# Dependencies

## Manager

- **Manager** — hatch 1.17 (at `~/.local/bin/hatch`).
- **Manifest** — `pyproject.toml` (PEP 621 project table + hatch env configuration).
- **Lockfile** — none. Hatch resolves dependencies on-the-fly when the environment is (re)created; there is no lockfile to maintain.

## Kinds

| Kind     | Manifest location                          |
| -------- | ------------------------------------------ |
| runtime  | `[project].dependencies`                   |
| dev      | `[tool.hatch.envs.default].dependencies`   |
| build    | `[build-system].requires`                  |

## Operations

### runtime

**Add**

```bash
hatch project add <pkg>
# Pinned to a specific version:
hatch project add <pkg>==<version>
```

Appends `<pkg>` (or `<pkg>==<version>`) to `[project].dependencies` in `pyproject.toml`.

**Remove**

```bash
hatch project remove <pkg>
```

Removes `<pkg>` from `[project].dependencies` in `pyproject.toml`.

**Update**

```bash
hatch project remove <pkg>
hatch project add <pkg>==<version>
```

There is no single hatch verb for updating a dependency. Remove the old entry, then add the new one.

### dev

**Add**

```bash
hatch project add --dev <pkg>
# Pinned:
hatch project add --dev <pkg>==<version>
```

Appends `<pkg>` (or `<pkg>==<version>`) to `[tool.hatch.envs.default].dependencies` in `pyproject.toml`.

**Remove**

```bash
hatch project remove --dev <pkg>
```

Removes `<pkg>` from `[tool.hatch.envs.default].dependencies` in `pyproject.toml`.

**Update**

```bash
hatch project remove --dev <pkg>
hatch project add --dev <pkg>==<version>
```

Remove the old entry, then add the new one.

### build

Build-system dependencies (`[build-system].requires`) are not managed by a hatch CLI command.
Edit `pyproject.toml` directly:

**Add** — In the `[build-system]` section, add the entry to the `requires` list:

```toml
[build-system]
requires = ["hatchling", "<pkg>==<version>"]
```

**Remove** — Remove the entry from the `requires` list:

```toml
[build-system]
requires = ["hatchling"]
```

**Update** — Change the version string in the `requires` list.

After editing, recreate the hatch environment to pick up the change:

```bash
hatch env create
```

## Conflict Resolution

Hatch resolves dependencies lazily when the environment is created. To inspect or force a resolution:

```bash
# Recreate the environment (forces fresh resolution)
hatch env remove default
hatch env create

# List installed packages in the hatch environment
hatch run pip list
```

There is no lockfile, so transitive dependencies cannot be pinned directly. If a transitive
dependency version is problematic, add a constraint to the appropriate dependency list
(`[project].dependencies` for runtime, `[tool.hatch.envs.default].dependencies` for dev)
and recreate the environment.

## Verify

After any dependency change, recreate the hatch environment and run the static-analysis + test
steps to confirm nothing is broken:

```bash
hatch env remove default
hatch env create
hatch run lint
hatch run typecheck
hatch run test
```
