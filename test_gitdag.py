import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from gitdag import (
    ComponentState,
    CopyState,
    Edge,
    GitState,
    GraphDiscoverer,
    GraphResult,
    Node,
    build_parser,
    inspect_component_state,
    print_human_report,
    topological_order,
    tree_manifest,
)


class TreeManifestTests(unittest.TestCase):
    def test_ordinary_directories_are_not_manifest_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "src"
            source.mkdir()
            (source / "main.cpp").write_text("int main() {}\n", encoding="utf-8")

            manifest = tree_manifest(root)

            self.assertNotIn("src", manifest)
            self.assertIn("src/main.cpp", manifest)

    def test_directory_symlinks_remain_manifest_entries(self) -> None:
        if os.name == "nt":
            self.skipTest("Creating symbolic links requires special permissions.")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "target").mkdir()
            (root / "linked-directory").symlink_to("target", target_is_directory=True)

            manifest = tree_manifest(root)

            self.assertEqual(
                manifest["linked-directory"],
                ("symlink", "target"),
            )

    def test_clean_component_with_subdirectory_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            component = repository / "module"
            source = component / "src"
            source.mkdir(parents=True)
            (source / "main.cpp").write_text("int main() {}\n", encoding="utf-8")

            commands = [
                ["git", "init", "--quiet"],
                ["git", "config", "user.name", "Gitdag Tests"],
                ["git", "config", "user.email", "gitdag@example.invalid"],
                ["git", "add", "module/src/main.cpp"],
                ["git", "commit", "--quiet", "-m", "Initial commit"],
            ]
            for command in commands:
                subprocess.run(command, cwd=repository, check=True)

            state = inspect_component_state(repository, component)

            self.assertEqual(state.state, "clean")
            self.assertIsNone(state.error)

    def test_gitignored_file_does_not_dirty_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            component = repository / "module"
            component.mkdir()
            (repository / ".gitignore").write_text(
                "module/.vscode/\n",
                encoding="utf-8",
            )
            (component / "tracked.txt").write_text("tracked\n", encoding="utf-8")

            commands = [
                ["git", "init", "--quiet"],
                ["git", "config", "user.name", "Gitdag Tests"],
                ["git", "config", "user.email", "gitdag@example.invalid"],
                ["git", "add", "."],
                ["git", "commit", "--quiet", "-m", "Initial commit"],
            ]
            for command in commands:
                subprocess.run(command, cwd=repository, check=True)

            settings = component / ".vscode" / "settings.json"
            settings.parent.mkdir()
            settings.write_text("{}\n", encoding="utf-8")

            state = inspect_component_state(repository, component)

            self.assertEqual(state.state, "clean")
            self.assertIsNone(state.error)

    def test_repository_root_can_be_inspected_as_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory)
            (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            commands = [
                ["git", "init", "--quiet"],
                ["git", "config", "user.name", "Gitdag Tests"],
                ["git", "config", "user.email", "gitdag@example.invalid"],
                ["git", "add", "tracked.txt"],
                ["git", "commit", "--quiet", "-m", "Initial commit"],
            ]
            for command in commands:
                subprocess.run(command, cwd=repository, check=True)

            state = inspect_component_state(repository, repository)

            self.assertEqual(state.state, "clean")
            self.assertIsNone(state.error)


class ContextualWorkingTests(unittest.TestCase):
    @staticmethod
    def initialize_repository(repository: Path) -> None:
        commands = [
            ["git", "init", "--quiet"],
            ["git", "config", "user.name", "Gitdag Tests"],
            ["git", "config", "user.email", "gitdag@example.invalid"],
            ["git", "add", "."],
            ["git", "commit", "--quiet", "-m", "Initial commit"],
        ]
        for command in commands:
            subprocess.run(command, cwd=repository, check=True)

    def test_unique_divergent_copy_is_marked_working(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)

            interval_repository = workspace / "interval.git"
            interval = interval_repository / "interval"
            interval.mkdir(parents=True)
            (interval / "interval.cpp").write_text("original\n", encoding="utf-8")
            self.initialize_repository(interval_repository)

            signals_repository = workspace / "signals.git"
            materialized_interval = signals_repository / "signals" / "interval"
            materialized_interval.mkdir(parents=True)
            (materialized_interval / "interval.cpp").write_text(
                "original\n",
                encoding="utf-8",
            )
            self.initialize_repository(signals_repository)

            (materialized_interval / "interval.cpp").write_text(
                "work in signals\n",
                encoding="utf-8",
            )

            result = GraphDiscoverer(workspace).discover(
                signals_repository / "signals"
            )

            self.assertEqual(len(result.edges), 1)
            self.assertEqual(result.edges[0].copy.state, "working")
            self.assertTrue(
                any(issue.kind == "working-copy" for issue in result.issues)
            )

    def test_committed_divergent_copy_remains_divergent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)

            interval_repository = workspace / "interval.git"
            interval = interval_repository / "interval"
            interval.mkdir(parents=True)
            (interval / "interval.cpp").write_text("canonical\n", encoding="utf-8")
            self.initialize_repository(interval_repository)

            signals_repository = workspace / "signals.git"
            materialized_interval = signals_repository / "signals" / "interval"
            materialized_interval.mkdir(parents=True)
            (materialized_interval / "interval.cpp").write_text(
                "committed fork\n",
                encoding="utf-8",
            )
            self.initialize_repository(signals_repository)

            result = GraphDiscoverer(workspace).discover(
                signals_repository / "signals"
            )

            self.assertEqual(len(result.edges), 1)
            self.assertEqual(result.edges[0].copy.state, "divergent")
            self.assertFalse(result.edges[0].copy.modified_in_consumer)

    def test_copy_matching_older_canonical_commit_is_outdated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)

            interval_repository = workspace / "interval.git"
            interval = interval_repository / "interval"
            interval.mkdir(parents=True)
            source = interval / "interval.cpp"
            source.write_text("version one\n", encoding="utf-8")
            self.initialize_repository(interval_repository)
            first_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=interval_repository,
                text=True,
                stdout=subprocess.PIPE,
                check=True,
            ).stdout.strip()

            signals_repository = workspace / "signals.git"
            materialized_interval = signals_repository / "signals" / "interval"
            materialized_interval.mkdir(parents=True)
            (materialized_interval / "interval.cpp").write_text(
                "version one\n",
                encoding="utf-8",
            )
            self.initialize_repository(signals_repository)

            source.write_text("version two\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "interval/interval.cpp"],
                cwd=interval_repository,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "Version two"],
                cwd=interval_repository,
                check=True,
            )

            result = GraphDiscoverer(workspace).discover(
                signals_repository / "signals"
            )

            self.assertEqual(len(result.edges), 1)
            self.assertEqual(result.edges[0].copy.state, "outdated")
            self.assertEqual(
                result.edges[0].copy.matched_canonical_commit,
                first_commit,
            )


class RootNormalizationTests(unittest.TestCase):
    @staticmethod
    def initialize_repository(repository: Path) -> None:
        commands = [
            ["git", "init", "--quiet"],
            ["git", "config", "user.name", "Gitdag Tests"],
            ["git", "config", "user.email", "gitdag@example.invalid"],
            ["git", "add", "."],
            ["git", "commit", "--quiet", "-m", "Initial commit"],
        ]
        for command in commands:
            subprocess.run(command, cwd=repository, check=True)

    def test_repository_root_uses_homonymous_canonical_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)

            dependency_repository = workspace / "dependency"
            dependency = dependency_repository / "dependency"
            dependency.mkdir(parents=True)
            (dependency / "value.txt").write_text("value\n", encoding="utf-8")
            self.initialize_repository(dependency_repository)

            root_repository = workspace / "module"
            canonical = root_repository / "module"
            materialized_dependency = root_repository / "dependency"
            canonical.mkdir(parents=True)
            materialized_dependency.mkdir()
            (canonical / "module.txt").write_text("module\n", encoding="utf-8")
            (materialized_dependency / "value.txt").write_text(
                "value\n",
                encoding="utf-8",
            )
            self.initialize_repository(root_repository)

            result = GraphDiscoverer(workspace).discover(root_repository)

            root = result.nodes[result.root_component]
            self.assertEqual(Path(root.canonical_path), canonical.resolve())
            self.assertEqual(len(result.nodes), 2)
            self.assertEqual(len(result.edges), 1)
            self.assertNotEqual(
                result.edges[0].dependency,
                result.edges[0].consumer,
            )
            self.assertEqual(result.cycles, [])


class TopologicalOrderTests(unittest.TestCase):
    @staticmethod
    def node(identifier: str, name: str) -> Node:
        return Node(
            id=identifier,
            name=name,
            repository="",
            canonical_path="",
            git=GitState(True, True, "commit", "main"),
            component=ComponentState(
                state="clean",
                commit="commit",
                commit_tree="tree",
                committed_hash="hash",
                working_hash="hash",
            ),
        )

    def test_complete_layer_is_emitted_before_next_layer(self) -> None:
        leaf_a = self.node("a", "alpha")
        leaf_z = self.node("z", "zeta")
        consumer = self.node("b", "beta")
        result = GraphResult(
            workspace="",
            root_component=consumer.id,
            nodes={node.id: node for node in (leaf_a, leaf_z, consumer)},
            edges=[
                Edge(
                    dependency=leaf_a.id,
                    consumer=consumer.id,
                    materialized_path="",
                    canonical_path="",
                    copy=CopyState(
                        state="synced",
                        base_commit="commit",
                        materialized_hash="hash",
                        canonical_working_hash="hash",
                        canonical_committed_hash="hash",
                    ),
                )
            ],
        )

        self.assertEqual(topological_order(result), ["a", "z", "b"])


class HumanReportTests(unittest.TestCase):
    @staticmethod
    def report_with_repository_state(repository_clean: bool | None) -> str:
        node = Node(
            id="module",
            name="module",
            repository="/workspace/module",
            canonical_path="/workspace/module/module",
            git=GitState(
                is_git_repository=repository_clean is not None,
                clean=repository_clean,
                commit="1234567890abcdef",
                branch="main",
            ),
            component=ComponentState(
                state="clean",
                commit="1234567890abcdef",
                commit_tree="tree",
                committed_hash="hash",
                working_hash="hash",
            ),
        )
        result = GraphResult(
            workspace="/workspace",
            root_component=node.id,
            nodes={node.id: node},
        )
        output = io.StringIO()
        with redirect_stdout(output):
            print_human_report(result)
        return output.getvalue()

    def test_clean_repository_is_displayed_systematically(self) -> None:
        self.assertEqual(
            self.report_with_repository_state(True),
            "module/module [clean:12345678, repo:clean]\n",
        )

    def test_dirty_repository_is_distinct_from_clean_component(self) -> None:
        self.assertEqual(
            self.report_with_repository_state(False),
            "module/module [clean:12345678, repo:dirty]\n",
        )

    def test_unknown_repository_state_is_displayed(self) -> None:
        self.assertEqual(
            self.report_with_repository_state(None),
            "module/module [clean:12345678, repo:unknown]\n",
        )


class CommandLineTests(unittest.TestCase):
    def test_root_defaults_to_current_directory(self) -> None:
        arguments = build_parser().parse_args([])

        self.assertEqual(arguments.root, Path("."))


if __name__ == "__main__":
    unittest.main()
