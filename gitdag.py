#!/usr/bin/env python3
"""
Discover a dependency DAG between Git repositories organized as follows:

workspace/
├── faust.git/
│   └── compiler/
├── foo.git/
│   └── foo/
└── fii.git/
    └── fii/

Discovery rule:
- for each direct subdirectory of a consumer repository, such as foo.git/fii,
- if workspace/fii.git/fii exists,
- then foo depends on fii.

Discovery continues even if:
- a Git repository is dirty;
- a dependency copy diverges from its canonical directory;
- an expected repository is not a valid Git repository.

The script is non-destructive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class GitState:
    is_git_repository: bool
    clean: bool | None
    commit: str | None
    branch: str | None
    status_lines: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class TreeComparison:
    equal: bool | None
    source_hash: str | None
    canonical_hash: str | None
    differences: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ComponentState:
    state: str
    commit: str | None
    commit_tree: str | None
    committed_hash: str | None
    working_hash: str | None
    error: str | None = None


@dataclass
class Node:
    id: str
    name: str
    repository: str
    canonical_path: str
    git: GitState
    component: ComponentState


@dataclass
class CopyState:
    state: str
    base_commit: str | None
    materialized_hash: str | None
    canonical_working_hash: str | None
    canonical_committed_hash: str | None
    differences: list[str] = field(default_factory=list)
    error: str | None = None
    modified_in_consumer: bool | None = None
    matched_canonical_commit: str | None = None


@dataclass
class Edge:
    dependency: str
    consumer: str
    materialized_path: str
    canonical_path: str
    copy: CopyState


@dataclass
class Issue:
    severity: str
    kind: str
    message: str
    path: str | None = None


@dataclass
class GraphResult:
    workspace: str
    root_component: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


class DiscoveryError(RuntimeError):
    pass


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def inspect_git_repository(repo: Path) -> GitState:
    probe = run_git(repo, "rev-parse", "--show-toplevel")
    if probe.returncode != 0:
        return GitState(
            is_git_repository=False,
            clean=None,
            commit=None,
            branch=None,
            error=probe.stderr.strip() or "This directory is not a Git repository.",
        )

    status = run_git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    commit = run_git(repo, "rev-parse", "HEAD")
    branch = run_git(repo, "branch", "--show-current")

    status_lines = [
        line for line in status.stdout.splitlines()
        if line.strip()
    ]

    return GitState(
        is_git_repository=True,
        clean=(status.returncode == 0 and not status_lines),
        commit=commit.stdout.strip() if commit.returncode == 0 else None,
        branch=branch.stdout.strip() if branch.returncode == 0 else None,
        status_lines=status_lines,
        error=status.stderr.strip() if status.returncode != 0 else None,
    )


def find_git_root(path: Path) -> Path:
    result = run_git(path, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise DiscoveryError(
            f"{path} is not inside a Git repository: "
            f"{result.stderr.strip() or 'unknown error'}"
        )
    return Path(result.stdout.strip()).resolve()


def file_mode_signature(path: Path) -> str:
    mode = path.lstat().st_mode
    executable = bool(mode & stat.S_IXUSR)
    return "x" if executable else "-"


def iter_tree_entries(root: Path) -> Iterable[tuple[str, str, str]]:
    """
    Yield tuples containing:
        (POSIX relative path, entry type, fingerprint)

    Timestamps, owners, and irrelevant permissions are ignored.
    The executable bit is preserved.
    """
    if not root.exists():
        raise FileNotFoundError(root)

    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)

        # Git does not track ordinary directories, only files and their paths.
        # Including them would make every component with a subdirectory dirty.
        directory_symlinks = sorted(
            name
            for name in dirnames
            if name != ".git" and (current_path / name).is_symlink()
        )
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name != ".git" and name not in directory_symlinks
        )
        filenames.sort()

        # os.walk puts directory symlinks in dirnames but does not traverse them
        # with followlinks=False, so emit them explicitly here.
        for name in directory_symlinks:
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            yield rel, "symlink", os.readlink(path)

        for name in filenames:
            path = current_path / name
            rel = path.relative_to(root).as_posix()

            if path.is_symlink():
                yield rel, "symlink", os.readlink(path)
                continue

            if path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                signature = f"{file_mode_signature(path)}:{digest.hexdigest()}"
                yield rel, "file", signature
                continue

            yield rel, "other", str(path.lstat().st_mode)


def git_visible_paths(repository: Path, root: Path) -> list[Path]:
    """Return tracked or non-ignored files located below ``root``."""
    repository = repository.resolve()
    root = root.resolve()
    try:
        relative_root = root.relative_to(repository)
    except ValueError as exc:
        raise OSError(f"{root} is not located inside {repository}") from exc

    arguments = [
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
    ]
    if relative_root != Path("."):
        arguments.append(relative_root.as_posix())

    listing = run_git(repository, *arguments)
    if listing.returncode != 0:
        raise OSError(
            listing.stderr.strip()
            or "Unable to apply Git exclusion rules."
        )

    visible: list[Path] = []
    for relative in listing.stdout.split("\0"):
        if not relative:
            continue
        path = repository / relative
        # A tracked but deleted file must remain absent from the working-tree
        # manifest so that comparison with HEAD detects its deletion.
        if path.is_symlink() or path.exists():
            visible.append(path)
    return visible


def tree_manifest(
    root: Path,
    repository: Path | None = None,
) -> dict[str, tuple[str, str]]:
    if repository is None:
        return {
            rel: (entry_type, fingerprint)
            for rel, entry_type, fingerprint in iter_tree_entries(root)
        }

    manifest: dict[str, tuple[str, str]] = {}
    for path in git_visible_paths(repository, root):
        rel = path.relative_to(root.resolve()).as_posix()
        if path.is_symlink():
            manifest[rel] = ("symlink", os.readlink(path))
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            manifest[rel] = (
                "file",
                f"{file_mode_signature(path)}:{digest.hexdigest()}",
            )
        else:
            manifest[rel] = ("other", str(path.lstat().st_mode))
    return {
        rel: entry
        for rel, entry in sorted(manifest.items())
    }


def working_tree_oid(
    root: Path,
    repository: Path,
    object_format: str,
) -> str | None:
    """Compute the Git tree object ID for a materialized working tree."""
    hash_constructor = getattr(hashlib, object_format, None)
    if hash_constructor is None:
        return None

    tree: dict[bytes, object] = {}

    def object_oid(object_type: bytes, content: bytes) -> bytes:
        digest = hash_constructor()
        digest.update(object_type)
        digest.update(b" ")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
        return digest.digest()

    for path in git_visible_paths(repository, root):
        relative_parts = [
            os.fsencode(part)
            for part in path.relative_to(root.resolve()).parts
        ]
        if not relative_parts:
            continue

        current = tree
        for part in relative_parts[:-1]:
            child = current.setdefault(part, {})
            if not isinstance(child, dict):
                return None
            current = child

        name = relative_parts[-1]
        if path.is_symlink():
            mode = b"120000"
            content = os.fsencode(os.readlink(path))
        elif path.is_file():
            mode = b"100755" if file_mode_signature(path) == "x" else b"100644"
            content = path.read_bytes()
        else:
            return None
        current[name] = (mode, object_oid(b"blob", content))

    def tree_oid(entries: dict[bytes, object]) -> bytes:
        serialized = bytearray()
        ordered = sorted(
            entries.items(),
            key=lambda item: item[0] + (b"/" if isinstance(item[1], dict) else b""),
        )
        for name, entry in ordered:
            if isinstance(entry, dict):
                mode = b"40000"
                oid = tree_oid(entry)
            else:
                mode, oid = entry
            serialized.extend(mode)
            serialized.extend(b" ")
            serialized.extend(name)
            serialized.extend(b"\0")
            serialized.extend(oid)
        return object_oid(b"tree", bytes(serialized))

    return tree_oid(tree).hex()


def manifest_hash(manifest: dict[str, tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(manifest):
        entry_type, fingerprint = manifest[rel]
        digest.update(rel.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(entry_type.encode("ascii"))
        digest.update(b"\0")
        digest.update(fingerprint.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
    return digest.hexdigest()


def committed_tree_manifest(
    repo: Path,
    component: Path,
) -> tuple[dict[str, tuple[str, str]] | None, str | None, str | None]:
    """Return the component manifest as it exists in HEAD.

    The returned tuple contains:
      manifest, Git tree object SHA, error.
    """
    try:
        relative = component.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None, None, f"{component} is not located inside {repo}"

    tree_spec = "HEAD^{tree}" if relative == "." else f"HEAD:{relative}"
    tree_result = run_git(repo, "rev-parse", tree_spec)
    if tree_result.returncode != 0:
        return (
            None,
            None,
            tree_result.stderr.strip()
            or f"Path {relative!r} does not exist in HEAD.",
        )
    tree_sha = tree_result.stdout.strip()

    listing = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "HEAD", "--", relative],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if listing.returncode != 0:
        return None, tree_sha, listing.stderr.decode(errors="replace").strip()

    manifest: dict[str, tuple[str, str]] = {}
    prefix = relative.rstrip("/") + "/"

    for record in listing.stdout.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode_b, object_type_b, object_sha_b = metadata.split(b" ", 2)
        mode = mode_b.decode("ascii")
        object_type = object_type_b.decode("ascii")
        object_sha = object_sha_b.decode("ascii")
        full_path = raw_path.decode("utf-8", errors="surrogateescape")
        rel = full_path[len(prefix):] if full_path.startswith(prefix) else full_path

        if object_type != "blob":
            continue

        blob = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", object_sha],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if blob.returncode != 0:
            return None, tree_sha, blob.stderr.decode(errors="replace").strip()

        if mode == "120000":
            target = blob.stdout.decode("utf-8", errors="surrogateescape")
            manifest[rel] = ("symlink", target)
        else:
            executable = "x" if mode == "100755" else "-"
            digest = hashlib.sha256(blob.stdout).hexdigest()
            manifest[rel] = ("file", f"{executable}:{digest}")

    return manifest, tree_sha, None


def compare_manifests(
    left: dict[str, tuple[str, str]],
    right: dict[str, tuple[str, str]],
    left_label: str,
    right_label: str,
    max_differences: int,
) -> list[str]:
    differences: list[str] = []
    for rel in sorted(set(left) | set(right)):
        if len(differences) >= max_differences:
            differences.append(
                f"... additional differences omitted "
                f"(limit: {max_differences})"
            )
            break
        left_entry = left.get(rel)
        right_entry = right.get(rel)
        if left_entry is None:
            differences.append(f"missing from {left_label}: {rel}")
        elif right_entry is None:
            differences.append(f"missing from {right_label}: {rel}")
        elif left_entry != right_entry:
            differences.append(f"different: {rel}")
    return differences


def inspect_component_state(repo: Path, canonical: Path) -> ComponentState:
    git_state = inspect_git_repository(repo)
    if not git_state.is_git_repository or not git_state.commit:
        return ComponentState(
            state="unknown",
            commit=git_state.commit,
            commit_tree=None,
            committed_hash=None,
            working_hash=None,
            error=git_state.error or "Invalid Git repository.",
        )

    committed, tree_sha, error = committed_tree_manifest(repo, canonical)
    try:
        working = tree_manifest(canonical, repo)
    except OSError as exc:
        working = None
        error = str(exc)

    if committed is None or working is None:
        return ComponentState(
            state="unknown",
            commit=git_state.commit,
            commit_tree=tree_sha,
            committed_hash=manifest_hash(committed) if committed is not None else None,
            working_hash=manifest_hash(working) if working is not None else None,
            error=error,
        )

    committed_hash = manifest_hash(committed)
    working_hash = manifest_hash(working)
    return ComponentState(
        state="clean" if committed == working else "dirty",
        commit=git_state.commit,
        commit_tree=tree_sha,
        committed_hash=committed_hash,
        working_hash=working_hash,
    )


def inspect_copy_state(
    materialized: Path,
    dependency_node: Node,
    consumer_repository: Path,
    max_differences: int,
) -> CopyState:
    try:
        materialized_manifest = tree_manifest(materialized, consumer_repository)
        canonical_manifest = tree_manifest(
            Path(dependency_node.canonical_path),
            Path(dependency_node.repository),
        )
    except OSError as exc:
        return CopyState(
            state="unknown",
            base_commit=dependency_node.component.commit,
            materialized_hash=None,
            canonical_working_hash=dependency_node.component.working_hash,
            canonical_committed_hash=dependency_node.component.committed_hash,
            error=str(exc),
        )

    committed_manifest, _, committed_error = committed_tree_manifest(
        Path(dependency_node.repository),
        Path(dependency_node.canonical_path),
    )
    materialized_hash = manifest_hash(materialized_manifest)
    canonical_hash = manifest_hash(canonical_manifest)

    if committed_manifest is None:
        return CopyState(
            state="unknown",
            base_commit=dependency_node.component.commit,
            materialized_hash=materialized_hash,
            canonical_working_hash=canonical_hash,
            canonical_committed_hash=None,
            error=committed_error,
        )

    committed_hash = manifest_hash(committed_manifest)
    materialized_equals_canonical = materialized_manifest == canonical_manifest
    canonical_equals_committed = canonical_manifest == committed_manifest
    materialized_equals_committed = materialized_manifest == committed_manifest

    if materialized_equals_canonical and canonical_equals_committed:
        state = "synced"
        differences: list[str] = []
        matched_canonical_commit = dependency_node.component.commit
    elif materialized_equals_canonical:
        state = "synced-working-tree"
        differences = []
        matched_canonical_commit = None
    elif materialized_equals_committed and not canonical_equals_committed:
        state = "outdated"
        matched_canonical_commit = dependency_node.component.commit
        differences = compare_manifests(
            materialized_manifest,
            canonical_manifest,
            "the copy",
            "the current canonical module",
            max_differences,
        )
    elif canonical_equals_committed:
        state = "divergent"
        matched_canonical_commit = None
        differences = compare_manifests(
            materialized_manifest,
            canonical_manifest,
            "the copy",
            "the canonical module",
            max_differences,
        )
    else:
        state = "divergent-working-tree"
        matched_canonical_commit = None
        differences = compare_manifests(
            materialized_manifest,
            canonical_manifest,
            "the copy",
            "the current canonical module",
            max_differences,
        )

    return CopyState(
        state=state,
        base_commit=dependency_node.component.commit,
        materialized_hash=materialized_hash,
        canonical_working_hash=canonical_hash,
        canonical_committed_hash=committed_hash,
        differences=differences,
        matched_canonical_commit=matched_canonical_commit,
    )


def compare_trees(
    materialized: Path,
    canonical: Path,
    max_differences: int = 50,
) -> TreeComparison:
    try:
        left = tree_manifest(materialized)
        right = tree_manifest(canonical)
    except OSError as exc:
        return TreeComparison(
            equal=None,
            source_hash=None,
            canonical_hash=None,
            error=str(exc),
        )

    left_hash = manifest_hash(left)
    right_hash = manifest_hash(right)

    if left_hash == right_hash and left == right:
        return TreeComparison(
            equal=True,
            source_hash=left_hash,
            canonical_hash=right_hash,
        )

    differences: list[str] = []
    all_paths = sorted(set(left) | set(right))

    for rel in all_paths:
        if len(differences) >= max_differences:
            differences.append(
                f"... additional differences omitted "
                f"(limit: {max_differences})"
            )
            break

        left_entry = left.get(rel)
        right_entry = right.get(rel)

        if left_entry is None:
            differences.append(f"missing from the copy: {rel}")
        elif right_entry is None:
            differences.append(f"missing from the canonical module: {rel}")
        elif left_entry != right_entry:
            differences.append(f"different: {rel}")

    return TreeComparison(
        equal=False,
        source_hash=left_hash,
        canonical_hash=right_hash,
        differences=differences,
    )


def node_id(repo: Path, canonical: Path) -> str:
    return f"{repo.resolve()}::{canonical.resolve().relative_to(repo.resolve()).as_posix()}"


class GraphDiscoverer:
    def __init__(
        self,
        workspace: Path,
        repository_suffix: str = "auto",
        max_differences: int = 50,
        debug: bool = False,
    ) -> None:
        self.workspace = workspace.resolve()
        self.repository_suffix = repository_suffix
        self.max_differences = max_differences
        self.debug = debug
        self.result: GraphResult | None = None
        self._visited: set[str] = set()
        self._visiting: list[str] = []
        self._edge_keys: set[tuple[str, str, str]] = set()

    def discover(self, root_component: Path) -> GraphResult:
        root_component = root_component.resolve()
        root_repo = find_git_root(root_component)

        scan_root = root_component
        if root_component == root_repo:
            repository_name = root_repo.name.removesuffix(".git")
            canonical_candidate = root_repo / repository_name
            if canonical_candidate.is_dir():
                root_component = canonical_candidate
                scan_root = root_repo
                if self.debug:
                    print(
                        f"[debug] canonical root component: {root_component}",
                        file=sys.stderr,
                    )

        if root_repo.parent != self.workspace:
            self._issue(
                "warning",
                "workspace-mismatch",
                (
                    f"Root repository {root_repo} is not a direct child of "
                    f"workspace {self.workspace}."
                ),
                root_repo,
            )

        root_name = root_component.name
        root_id = node_id(root_repo, root_component)

        self.result = GraphResult(
            workspace=str(self.workspace),
            root_component=root_id,
        )

        self._visit(
            name=root_name,
            repo=root_repo,
            canonical=root_component,
            scan_root=scan_root,
        )

        self._mark_historical_copies_outdated()
        self._mark_contextual_working_copies()

        return self.result

    def _mark_historical_copies_outdated(self) -> None:
        """Mark copies matching an earlier canonical commit as outdated."""
        assert self.result is not None

        history_cache: dict[str, dict[str, str]] = {}
        format_cache: dict[str, str | None] = {}

        for edge in self.result.edges:
            dependency = self.result.nodes[edge.dependency]
            if (
                edge.copy.state != "divergent"
                or dependency.component.state != "clean"
            ):
                continue

            repository = Path(dependency.repository)
            repository_key = str(repository)
            if repository_key not in format_cache:
                object_format = run_git(
                    repository,
                    "rev-parse",
                    "--show-object-format",
                )
                format_cache[repository_key] = (
                    object_format.stdout.strip()
                    if object_format.returncode == 0
                    else None
                )
            object_format_name = format_cache[repository_key]
            if object_format_name is None:
                continue

            consumer = self.result.nodes[edge.consumer]
            materialized_oid = working_tree_oid(
                Path(edge.materialized_path),
                Path(consumer.repository),
                object_format_name,
            )
            if materialized_oid is None:
                continue

            if edge.dependency not in history_cache:
                history_cache[edge.dependency] = self._canonical_tree_history(
                    dependency
                )
            matching_commit = history_cache[edge.dependency].get(materialized_oid)
            if matching_commit is None:
                continue

            edge.copy.state = "outdated"
            edge.copy.matched_canonical_commit = matching_commit
            for issue in self.result.issues:
                if (
                    issue.kind == "divergent-copy"
                    and issue.path == edge.materialized_path
                ):
                    issue.kind = "outdated-copy"
                    issue.message = (
                        f"{edge.materialized_path} matches canonical commit "
                        f"{matching_commit[:8]} and is older than "
                        f"{short_sha(dependency.component.commit)}."
                    )

    @staticmethod
    def _canonical_tree_history(node: Node) -> dict[str, str]:
        """Map historical canonical tree IDs to their newest matching commit."""
        repository = Path(node.repository)
        canonical = Path(node.canonical_path)
        try:
            relative = canonical.relative_to(repository).as_posix()
        except ValueError:
            return {}

        arguments = ["log", "--format=%H", "HEAD"]
        if relative != ".":
            arguments.extend(["--", relative])
        history = run_git(repository, *arguments)
        if history.returncode != 0:
            return {}

        trees: dict[str, str] = {}
        for commit in history.stdout.splitlines():
            tree_spec = f"{commit}^{{tree}}" if relative == "." else f"{commit}:{relative}"
            tree = run_git(repository, "rev-parse", tree_spec)
            if tree.returncode == 0:
                trees.setdefault(tree.stdout.strip(), commit)
        return trees

    def _mark_contextual_working_copies(self) -> None:
        """Identify a copy as the unique location of work on a module.

        This classification is an inference limited to the discovered graph:
        the canonical module must be clean, exactly one copy must diverge, that
        copy must be modified in the consumer working tree, and every other
        known copy must be synchronized with the canonical module.
        """
        assert self.result is not None

        copies_by_dependency: dict[str, list[Edge]] = {
            identifier: [] for identifier in self.result.nodes
        }
        for edge in self.result.edges:
            consumer = self.result.nodes[edge.consumer]
            edge.copy.modified_in_consumer = self._copy_is_modified_in_consumer(
                edge,
                consumer,
            )
            copies_by_dependency[edge.dependency].append(edge)

        for dependency_id, edges in copies_by_dependency.items():
            dependency = self.result.nodes[dependency_id]
            if dependency.component.state != "clean":
                continue

            divergent = [edge for edge in edges if edge.copy.state == "divergent"]
            if len(divergent) != 1:
                continue
            if divergent[0].copy.modified_in_consumer is not True:
                continue
            if any(
                edge.copy.state not in {"synced", "divergent"}
                for edge in edges
            ):
                continue

            working_edge = divergent[0]
            working_edge.copy.state = "working"

            for issue in self.result.issues:
                if (
                    issue.kind == "divergent-copy"
                    and issue.path == working_edge.materialized_path
                ):
                    issue.kind = "working-copy"
                    issue.message = (
                        f"{working_edge.materialized_path} is the only modified "
                        f"copy of {dependency.name}; these changes should be "
                        "transferred to the canonical module."
                    )

    @staticmethod
    def _copy_is_modified_in_consumer(edge: Edge, consumer: Node) -> bool | None:
        """Return whether the copy differs from its consumer repository HEAD."""
        repository = Path(consumer.repository)
        materialized = Path(edge.materialized_path)
        try:
            relative = materialized.relative_to(repository).as_posix()
        except ValueError:
            return None

        status = run_git(
            repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            relative,
        )
        if status.returncode != 0:
            return None
        return any(line.strip() for line in status.stdout.splitlines())

    def _issue(
        self,
        severity: str,
        kind: str,
        message: str,
        path: Path | None = None,
    ) -> None:
        if self.result is None:
            return
        self.result.issues.append(
            Issue(
                severity=severity,
                kind=kind,
                message=message,
                path=str(path) if path else None,
            )
        )

    def _register_node(self, name: str, repo: Path, canonical: Path) -> str:
        assert self.result is not None

        identifier = node_id(repo, canonical)
        if identifier not in self.result.nodes:
            git_state = inspect_git_repository(repo)
            component_state = inspect_component_state(repo, canonical)
            self.result.nodes[identifier] = Node(
                id=identifier,
                name=name,
                repository=str(repo),
                canonical_path=str(canonical),
                git=git_state,
                component=component_state,
            )

            if not git_state.is_git_repository:
                self._issue(
                    "error",
                    "not-a-git-repository",
                    f"{repo} is not a valid Git repository.",
                    repo,
                )
            elif git_state.clean is False:
                self._issue(
                    "warning",
                    "dirty-repository",
                    f"Repository {repo.name} contains local changes.",
                    repo,
                )

        return identifier

    def _find_dependency_repository(self, dependency_name: str) -> Path | None:
        """Find the sibling repository for a module.

        In ``auto`` mode, try ``<name>`` first and then ``<name>.git``.
        With ``--repository-suffix``, use only the requested suffix.
        """
        if self.repository_suffix == "auto":
            candidates = [
                self.workspace / dependency_name,
                self.workspace / f"{dependency_name}.git",
            ]
        else:
            candidates = [
                self.workspace / f"{dependency_name}{self.repository_suffix}"
            ]

        if self.debug:
            print(
                f"[debug] potential module {dependency_name!r}; candidates: "
                + ", ".join(str(repo / dependency_name) for repo in candidates),
                file=sys.stderr,
            )

        matches = [
            repo for repo in candidates
            if (repo / dependency_name).is_dir()
        ]
        if not matches:
            return None

        if len(matches) > 1:
            self._issue(
                "warning",
                "ambiguous-repository",
                (
                    f"Multiple repositories match module {dependency_name!r}: "
                    + ", ".join(str(path) for path in matches)
                    + f". Using {matches[0]}."
                ),
                matches[0],
            )
        return matches[0]

    def _visit(
        self,
        name: str,
        repo: Path,
        canonical: Path,
        scan_root: Path | None = None,
    ) -> str:
        assert self.result is not None

        identifier = self._register_node(name, repo, canonical)

        if identifier in self._visiting:
            start = self._visiting.index(identifier)
            cycle = self._visiting[start:] + [identifier]
            if cycle not in self.result.cycles:
                self.result.cycles.append(cycle)
                self._issue(
                    "error",
                    "dependency-cycle",
                    "Cycle detected: " + " -> ".join(
                        self.result.nodes[item].name
                        for item in cycle
                    ),
                )
            return identifier

        if identifier in self._visited:
            return identifier

        self._visiting.append(identifier)

        if not repo.is_dir():
            self._issue(
                "error",
                "missing-repository",
                f"Repository {repo} does not exist.",
                repo,
            )
        else:
            # The entry point explicitly scans the requested directory (for
            # example, faust/compiler). During recursion, dependencies are
            # searched for at the root of each module repository.
            effective_scan_root = scan_root if scan_root is not None else repo

            if self.debug:
                print(f"[debug] scanning: {effective_scan_root}", file=sys.stderr)

            try:
                children = sorted(
                    entry for entry in effective_scan_root.iterdir()
                    if entry.is_dir() and entry.name != ".git"
                )
            except OSError as exc:
                self._issue(
                    "error",
                    "repository-read-error",
                    str(exc),
                    repo,
                )
                children = []

            for materialized in children:
                dependency_name = materialized.name

                # The repository's own canonical directory is not a dependency.
                if materialized.resolve() == canonical.resolve():
                    continue

                dependency_repo = self._find_dependency_repository(dependency_name)
                if dependency_repo is None:
                    continue
                dependency_canonical = dependency_repo / dependency_name

                dependency_id = self._register_node(
                    dependency_name,
                    dependency_repo,
                    dependency_canonical,
                )

                dependency_node = self.result.nodes[dependency_id]
                copy_state = inspect_copy_state(
                    materialized,
                    dependency_node,
                    consumer_repository=repo,
                    max_differences=self.max_differences,
                )

                edge_key = (
                    dependency_id,
                    identifier,
                    str(materialized.resolve()),
                )
                if edge_key not in self._edge_keys:
                    self._edge_keys.add(edge_key)
                    self.result.edges.append(
                        Edge(
                            dependency=dependency_id,
                            consumer=identifier,
                            materialized_path=str(materialized.resolve()),
                            canonical_path=str(dependency_canonical.resolve()),
                            copy=copy_state,
                        )
                    )

                if copy_state.state in {"divergent", "divergent-working-tree"}:
                    self._issue(
                        "warning",
                        "divergent-copy",
                        (
                            f"{materialized} diverge de "
                            f"{dependency_canonical}."
                        ),
                        materialized,
                    )
                elif copy_state.error:
                    self._issue(
                        "error",
                        "comparison-error",
                        (
                            f"Unable to compare {materialized} and "
                            f"{dependency_canonical}: {copy_state.error}"
                        ),
                        materialized,
                    )

                self._visit(
                    dependency_name,
                    dependency_repo,
                    dependency_canonical,
                    scan_root=dependency_repo,
                )

        self._visiting.pop()
        self._visited.add(identifier)
        return identifier


def topological_order(result: GraphResult) -> list[str] | None:
    if result.cycles:
        return None

    indegree = {node_id: 0 for node_id in result.nodes}
    consumers: dict[str, list[str]] = {
        node_id: [] for node_id in result.nodes
    }

    for edge in result.edges:
        indegree[edge.consumer] += 1
        consumers[edge.dependency].append(edge.consumer)

    def sort_key(identifier: str) -> tuple[str, str]:
        return result.nodes[identifier].name.casefold(), identifier

    current_layer = sorted(
        (
            node_id
            for node_id, degree in indegree.items()
            if degree == 0
        ),
        key=sort_key,
    )
    order: list[str] = []

    while current_layer:
        # Process the complete layer before considering nodes unlocked by it.
        # This keeps all dependency-free modules together, followed by modules
        # whose dependencies belong exclusively to earlier layers.
        order.extend(current_layer)
        next_layer: list[str] = []

        for current in current_layer:
            for consumer in consumers[current]:
                indegree[consumer] -= 1
                if indegree[consumer] == 0:
                    next_layer.append(consumer)

        current_layer = sorted(next_layer, key=sort_key)

    return order if len(order) == len(result.nodes) else None


def display_path(node: Node, result: GraphResult) -> str:
    """Return a readable path, relative to the workspace when possible."""
    path = Path(node.canonical_path)
    workspace = Path(result.workspace)
    try:
        return path.relative_to(workspace).as_posix() + "/"
    except ValueError:
        return path.as_posix() + "/"


def short_sha(value: str | None, length: int = 8) -> str:
    return value[:length] if value else "unknown-sha"


def compact_node_name(node: Node, result: GraphResult) -> str:
    """Return a compact but unambiguous module name.

    The root component keeps its repository/component path (for example,
    ``faust/compiler``). Module repositories shaped as ``xxx/xxx`` are
    shortened to ``xxx``.
    """
    path = Path(node.canonical_path)
    repo = Path(node.repository)

    if node.id == result.root_component:
        try:
            return path.relative_to(Path(result.workspace)).as_posix()
        except ValueError:
            return path.as_posix()

    if path.name == repo.name.removesuffix(".git"):
        return path.name

    try:
        return path.relative_to(Path(result.workspace)).as_posix()
    except ValueError:
        return path.as_posix()


def compact_copy_label(copy: CopyState) -> str:
    base = short_sha(copy.base_commit)
    labels = {
        "synced": f"synced:{base}",
        "working": f"WORKING:{base}",
        "synced-working-tree": f"synced+:{base}",
        "outdated": f"outdated:{base}",
        "divergent": f"divergent:{base}",
        "divergent-working-tree": f"divergent+:{base}",
        "unknown": f"unknown:{base}",
    }
    return labels.get(copy.state, f"{copy.state}:{base}")


def print_human_report(result: GraphResult) -> None:
    """Print exactly one line per module and one per dependency."""
    order = topological_order(result)
    if order is None:
        ordered_ids = sorted(result.nodes, key=lambda item: result.nodes[item].name)
    else:
        ordered_ids = order

    dependencies_by_consumer: dict[str, list[Edge]] = {
        identifier: [] for identifier in result.nodes
    }
    for edge in result.edges:
        dependencies_by_consumer[edge.consumer].append(edge)

    for index, identifier in enumerate(ordered_ids):
        node = result.nodes[identifier]
        print(
            f"{compact_node_name(node, result)} "
            f"[{node.component.state}:{short_sha(node.component.commit)}]"
        )

        edges = sorted(
            dependencies_by_consumer[identifier],
            key=lambda edge: compact_node_name(
                result.nodes[edge.dependency], result
            ),
        )
        for edge in edges:
            dependency = result.nodes[edge.dependency]
            print(
                f"  {compact_node_name(dependency, result)} "
                f"[{compact_copy_label(edge.copy)}]"
            )

        if index < len(ordered_ids) - 1:
            print()


def serialize_result(result: GraphResult) -> dict:
    payload = asdict(result)
    payload["topological_order"] = topological_order(result)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Discover the dependency graph between Git repositories "
            "containing copies of canonical subdirectories."
        )
    )
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path("."),
        help=(
            "Root component or repository. A /work/interval repository "
            "containing /work/interval/interval is normalized automatically."
            " Defaults to the current directory."
        ),
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        help=(
            "Directory containing sibling repositories. Defaults to the "
            "parent of the Git repository containing ROOT."
        ),
    )
    parser.add_argument(
        "--repository-suffix",
        default="auto",
        help=(
            "Repository directory suffix. The default 'auto' tries <name> "
            "first and then <name>.git. Use '.git' or '' to enforce a "
            "specific convention."
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        type=Path,
        help="Also write the complete result to a JSON file.",
    )
    parser.add_argument(
        "--max-differences",
        type=int,
        default=50,
        help="Maximum number of differences retained in the JSON output.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show scanned directories and candidate repositories.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Return a non-zero exit code if a repository is dirty, "
            "a copy diverges, or a cycle is detected."
        ),
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    root = args.root.resolve()

    try:
        root_repo = find_git_root(root)
    except DiscoveryError as exc:
        parser.error(str(exc))

    workspace = (
        args.workspace.resolve()
        if args.workspace
        else root_repo.parent.resolve()
    )

    discoverer = GraphDiscoverer(
        workspace=workspace,
        repository_suffix=args.repository_suffix,
        max_differences=max(1, args.max_differences),
        debug=args.debug,
    )

    try:
        result = discoverer.discover(root)
    except DiscoveryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print_human_report(result)

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(
                serialize_result(result),
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    if not args.strict:
        return 0

    has_strict_failure = (
        bool(result.cycles)
        or any(node.component.state != "clean" for node in result.nodes.values())
        or any(
            edge.copy.state not in {"synced"}
            for edge in result.edges
        )
        or any(
            issue.kind in {
                "comparison-error",
                "not-a-git-repository",
                "missing-repository",
            }
            for issue in result.issues
        )
    )
    return 1 if has_strict_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())
