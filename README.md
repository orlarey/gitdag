# gitdag

Analyze inclusion relationships between Git repositories, starting from a root repository.

# Installation

Install the `gitdag` command in `/usr/local/bin`:

```sh
make install
```

Use `PREFIX` to select another installation prefix:

```sh
make install PREFIX=/custom/prefix
```

# Usage

Run `gitdag` without arguments to analyze the current directory:

```sh
gitdag
```

Pass a component or repository to analyze another root:

```sh
gitdag ../faust/compiler
```

Run `gitdag --help` to see all available options.

# Dependency Status Format

This document describes the format produced by `gitdag`.

Its purpose is to provide a **compact**, **human-readable**, and **easy-to-parse** representation of the state of the modules in a workspace.

The text format is intentionally much simpler than JSON, which remains available for automated tooling.

---

# Organization

Modules are displayed in **topological order**:

- all modules without dependencies appear together in the first level;
- each subsequent level contains modules whose dependencies all appear in
  earlier levels;
- modules within the same level are sorted by name;
- the root module (for example, `faust/compiler`) appears last.

Each module occupies:

- **one line** if it has no dependencies;
- **n + 1 lines** if it has `n` dependencies.

For example:

```text
DirectedGraph [clean:e5c6c27a]

tlib [clean:50215fb8]

FaustAlgebra [clean:7d0438bf]
  tlib [WORKING:50215fb8]

compiler [dirty:47196653]
  DirectedGraph [synced:e5c6c27a]
  FaustAlgebra [outdated:7d0438bf]
  interval [synced+:7826244d]
```

---

# Module States

The main line describes the state of the canonical module.

```text
module_name [state:commit]
```

The commit always corresponds to the **HEAD of the module's Git repository**.

## clean

```text
interval [clean:7826244d]
```

The current contents of the canonical directory are identical to those stored in the specified commit.

Untracked files excluded by `.gitignore`, `.git/info/exclude`, or the global Git configuration do not participate in the comparison. A tracked file is still compared even if it subsequently matches an exclusion rule.

In other words:

```text
working tree == HEAD
```

for this directory.

The repository may contain changes elsewhere; they are not taken into account.

---

## dirty

```text
interval [dirty:7826244d]
```

The current contents of the canonical directory differ from those stored in the specified commit.

In other words:

```text
working tree != HEAD
```

for this directory.

---

# Dependency States

Indented lines describe materialized copies of dependencies.

```text
module
  dependency [state:commit]
```

The commit is always the HEAD of the dependency's canonical repository.

The dependency states can be expressed using:

- **H**: the canonical repository's `HEAD`;
- **H_old**: an earlier commit in the canonical repository's history;
- **C**: the canonical working tree;
- **M**: the materialized copy in the consumer repository.

| Canonical state | Copy relationship | Displayed state |
|---|---|---|
| `C = H` | `M = C` | `synced` |
| `C = H` | `M = H_old` | `outdated` |
| `C = H` | `M ≠ C` with no historical match | `divergent` or `WORKING` |
| `C ≠ H` | `M = C` | `synced+` |
| `C ≠ H` | `M = H` | `outdated` |
| `C ≠ H` | `M ≠ C` and `M ≠ H` | `divergent+` |

---

## synced

```text
interval [synced:7826244d]
```

The copy is identical to the canonical module, which is itself identical to the specified commit.

In other words:

```text
M = C = H
```

---

## synced+

```text
interval [synced+:7826244d]
```

The copy is identical to the current canonical module, but the canonical module contains local changes.

In other words:

```text
M = C
C != H
```

The `+` suffix therefore means:

> synchronized with a canonical module that has not yet been committed.

---

## outdated

```text
interval [outdated:7826244d]
```

The copy is behind the current canonical module. This occurs in either of two situations:

- the copy matches `HEAD`, while the canonical working tree contains newer local changes;
- the canonical module is clean, but the copy matches an earlier commit from its history.

In other words:

```text
M = H
C != H

or

C = H
M = H_old
```

The copy has simply not been updated yet. The JSON field
`matched_canonical_commit` identifies the canonical commit matched by the copy.

---

## divergent

```text
interval [divergent:7826244d]
```

The canonical module is clean, but the copy differs from it.

In other words:

```text
C = H
M != H
```

The divergence comes exclusively from the copy.

---

## WORKING

```text
interval [WORKING:7826244d]
```

The copy is the only divergent copy of the module in the discovered graph and contains uncommitted changes in the consumer repository. The canonical module is clean, and all its other known copies are identical to `HEAD`.

In other words:

```text
C = H
M != H
M != consumer repository HEAD
all other materialized copies = H
```

This state indicates that work performed in the context of the consumer module probably needs to be transferred to the canonical module. It is an inference limited to the copies reachable from the analyzed root, not proof of the developer's intent.

`WORKING` remains a divergence and therefore produces a non-zero exit code with `--strict`.

---

## divergent+

```text
interval [divergent+:7826244d]
```

Both the copy and the canonical module have been modified, but in different ways.

In other words:

```text
C != H
M != C
M != H
```

Here, the `+` suffix indicates that the divergence is measured against a canonical module that has itself been modified.

---

# Reading an Example

```text
DirectedGraph [clean:e5c6c27a]

FaustAlgebra [clean:7d0438bf]
  tlib [WORKING:50215fb8]

interval [dirty:7826244d]
  FaustAlgebra [divergent:7d0438bf]

signals [dirty:71c2c33e]
  FaustAlgebra [outdated:7d0438bf]
  interval [synced+:7826244d]
  tlib [synced:50215fb8]

compiler [dirty:47196653]
  DirectedGraph [synced:e5c6c27a]
  FaustAlgebra [outdated:7d0438bf]
  interval [synced+:7826244d]
  signals [synced+:71c2c33e]
  tlib [synced:50215fb8]
```

This immediately shows that:

- `DirectedGraph` is synchronized with its repository;
- `FaustAlgebra` contains the only modified copy of `tlib`, which needs to be transferred to its canonical module;
- `interval` contains uncommitted local changes;
- `signals` is synchronized with the `interval` working tree, but not with its latest commit;
- `compiler` is synchronized with `DirectedGraph`, but contains an outdated copy of `FaustAlgebra`.

---

# Why This Format?

JSON is intended for automated processing.

The text format is intended for developers. It is:

- compact;
- stable;
- diff-friendly;
- easy to read in a terminal;
- easy to parse from a script when necessary.
