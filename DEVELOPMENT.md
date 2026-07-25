# Development

## Prerequisites

| Tool     | Version       | Notes                                    |
| -------- | ------------- | ---------------------------------------- |
| Python   | >= 3.12       | Runtime interpreter.                     |
| hatch    | >= 1.17       | Build manager. Installed at `~/.local/bin/hatch` or on `PATH`. |

No additional installation is required — the five scripts below drive hatch directly.
The hatch environment (defined in `pyproject.toml`) is created automatically on first use.

## Build Scripts

All scripts are run from the **project root** (`kodo/`). Each script resolves its own path
relative to its location in `scripts/`, so `cwd` does not matter for the caller.

| Script                      | What it does                                             | Hatch command(s)                           |
| --------------------------- | -------------------------------------------------------- | ------------------------------------------ |
| `scripts/build.{sh,ps1}`    | Build wheel + sdist (quick dev build, no version stamp). | `hatch build`                              |
| `scripts/format.{sh,ps1}`   | Auto-format source with ruff.                            | `hatch run fmt`                            |
| `scripts/static_analysis.{sh,ps1}` | Lint with ruff, then type-check with mypy.         | `hatch run lint` && `hatch run typecheck`  |
| `scripts/test.{sh,ps1}`     | Run the pytest test suite.                               | `hatch run test`                           |
| `scripts/full_build.{sh,ps1}` | format → build → static_analysis → test, fail fast.   | Delegates to the four scripts above.       |

### Invocation

**Linux / macOS:**

```bash
./scripts/build.sh
./scripts/format.sh
./scripts/static_analysis.sh
./scripts/test.sh
./scripts/full_build.sh
```

**Windows (PowerShell):**

```powershell
.\scripts\build.ps1
.\scripts\format.ps1
.\scripts\static_analysis.ps1
.\scripts\test.ps1
.\scripts\full_build.ps1
```

### Test Selector

`scripts/test` accepts an optional first argument that is passed directly to pytest.

```bash
# Run the full suite
./scripts/test.sh

# Run a single test file
./scripts/test.sh test/test_orders.py

# Run a single test function
./scripts/test.sh test/test_orders.py::test_refund

# Run by keyword filter
./scripts/test.sh -k refund
```

Because hatch passes `{args}` through to the underlying `pytest` command, any valid
pytest node-id, path, or flag works as the selector.

## Scripts vs. Hatch Direct Commands

The five Kodo scripts cover the standard build model. Additional hatch commands are
available for day-to-day work:

| Command                  | Purpose                                              |
| ------------------------ | ---------------------------------------------------- |
| `hatch run check`        | Run fmt, lint, typecheck, and tests — no build.      |
| `hatch run build`        | Full release pipeline (version stamp, checks, build, post-increment). |
| `hatch run check-version` | Sync `__version__` in `__init__.py` from `pyproject.toml`. |

See [`README.md`](README.md) for the full development cycle and release workflow.

## Cross-Platform

The project is pure Python with no native extensions. The `.sh` and `.ps1` pairs
invoke identical hatch commands; there are no host/target differences.
