# Dependency Contract — `DEPENDENCIES.md`

A project's dependencies are changed through **one** machine-followable document:
**`DEPENDENCIES.md`** at the project root. A toolchain-setup agent **writes** it;
the dependency-management agent (`toolchain_depsmgr`) **reads and executes** it.
Neither edits manifests or lockfiles by guesswork — `DEPENDENCIES.md` is the
single source of truth, and it is **language- and manager-agnostic**: the same
structure describes a `uv`/`pyproject.toml` project, an `npm`/`package.json`
project, a `cargo`, `go mod`, `maven`, or any other toolchain. Only the concrete
commands and manifest sections differ.

To stay executable by an agent that does not know the language in advance, every
`DEPENDENCIES.md` **must** follow the structure below exactly — the same headings,
in this order, with commands written as literal shell lines using the reserved
placeholders. A reader locates a block by its heading, substitutes the
placeholders, and runs the commands verbatim.

## Reserved Placeholders

Commands are written with these literal tokens; the executing agent substitutes
them before running:

- `<pkg>` — the dependency's package/module name.
- `<version>` — a version or version constraint (may be empty for "latest").
- `<extra>` — the optional-feature / extras group name (only in the `optional` kind).

## Canonical Dependency Kinds

Every dependency belongs to exactly one **kind**, drawn from this fixed
vocabulary (a toolchain maps its native categories onto these names):

- `runtime` — needed when the software runs in production (libraries it imports).
- `dev` — needed only while developing (formatters, linters, type checkers, build helpers).
- `test` — needed only to run the test suite.
- `optional` — pulled in only for an opt-in feature / extra (grouped under `<extra>`).
- `build` — needed by the build/packaging backend itself (release/build-system requirements).

A project documents **only the kinds it actually uses**; an absent kind means the
project has no such category and the reader must report the kind as unsupported
rather than improvising.

## Required Structure

`DEPENDENCIES.md` MUST contain these sections, with these exact `##` headings,
in this order:

### `## Manager`

Identify the toolchain's dependency machinery, one bullet each:

- **Manager** — the package/dependency manager and the detected version (`uv 0.5`, `npm 10`, `cargo`, …).
- **Manifest** — the manifest file(s) dependencies are declared in (`pyproject.toml`, `package.json`, `Cargo.toml`, …).
- **Lockfile** — the lockfile(s) kept in sync, or `none` if the toolchain has none.

### `## Kinds`

A table mapping each kind **this project uses** to where it lives in the manifest:

```
| Kind     | Manifest location                  |
| -------- | ---------------------------------- |
| runtime  | [project].dependencies             |
| test     | [dependency-groups].test           |
```

### `## Operations`

One `###` subsection **per kind in `## Kinds`**, heading = the exact kind name
(`### runtime`, `### test`, …). Each subsection gives the literal command(s) for
**every** operation, under bold labels, in this order:

- **Add** — install/declare `<pkg>` (at `<version>` when given) as this kind, updating the manifest and lockfile.
- **Remove** — uninstall/undeclare `<pkg>` of this kind.
- **Update** — change `<pkg>` to `<version>` (or to latest when `<version>` is empty) for this kind.

Each label is followed by a fenced command block — one shell command per line,
run in order from the project root. Use the reserved placeholders.

Some managers have no CLI verb for a given operation (e.g. `vcpkg`'s manifest
mode ships `add` but no `remove` or version-pin command) and the operation is
only reachable by editing the manifest directly. For that case, write **a
direct manifest edit** instead of a command block: state the exact file, the
JSON/TOML/etc. path or array within it, and the precise before/after content
(substituting the reserved placeholders) — precise enough that an agent with
only `edit_file` can perform it mechanically, the same way a command block is
precise enough to run verbatim. Only fall back to this when the manager
genuinely has no command; prefer a real command wherever one exists. If an
operation cannot be expressed *either* way for a kind, write a single line
stating so (the reader surfaces it as a failure, never guesses).

### `## Conflict Resolution`

Command-level steps to inspect dependency resolution, pin or constrain a
transitive dependency, relax an over-tight constraint, and regenerate the
lockfile — what the agent does when an `Add`/`Update` fails to resolve.

### `## Verify`

A fenced block with the command(s) that confirm the dependency graph resolves
and the lockfile is current (e.g. a lock/sync command). The dependency-management
agent runs this after every successful change and treats a non-zero exit as a
failed operation.
