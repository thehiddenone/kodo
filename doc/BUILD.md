# Build System

> Status: implemented (2026-08-29). `kodo` is a hybrid Python/Rust package —
> Python source under `src/kodo/`, a Rust crate under `rust/`, built together
> into one wheel by [maturin](https://www.maturin.rs/) with
> [PyO3](https://pyo3.rs/) bindings.

---

## 1. Why this exists

`kodo-vsix` installs `py-kodo` into the end user's `~/.kodo/venv` with a
plain `uv pip install py-kodo==<version>` (`src/uv-setup.ts`) — no
`--no-binary` flag, no source build. For that to keep working with **zero
Rust toolchain on the end user's machine**, every release must publish a
prebuilt wheel on PyPI matching each end user's platform, architecture, and
Python version; `uv`/`pip` then picks that wheel automatically instead of
falling back to the source distribution. Rust/`cargo` is only ever needed on
the machine *building* those wheels — a `kodo` contributor's dev machine, or
CI. This is the constraint every choice below is optimized for.

## 2. Layout

```
kodo/
  pyproject.toml       <- [build-system] backend = maturin; [tool.maturin] config
  rust/
    Cargo.toml          <- the one Rust crate, package name "kodo-rust-native"
    src/
      lib.rs             <- #[pymodule] fn rust_native — add new Rust-backed
                             functions here
  src/
    kodo/
      __init__.py
      __main__.py
      rust_native.pyi     <- type stub for the compiled extension (mypy)
      rust_native*.so      <- compiled extension, built in place by
                             `maturin develop` / `pip install -e .`; not
                             committed (gitignored *.so / *.pyd)
      ... (unchanged pure-Python package)
```

The Rust crate lives in its own top-level `rust/` directory rather than
inside `src/` — maturin's default crate location is a `src/` folder next to
`Cargo.toml`, which would collide with `src/kodo/`'s existing Python
src-layout. `pyproject.toml`'s `[tool.maturin]` table points at it
explicitly:

```toml
[tool.maturin]
manifest-path = "rust/Cargo.toml"
python-source = "src"
module-name = "kodo.rust_native"
```

`module-name = "kodo.rust_native"` is what makes the compiled extension land
*inside* the existing `kodo` package as a submodule — `from kodo.rust_native
import hello_world` — rather than as a second top-level package. There's
still only one PyPI distribution (`py-kodo`), one wheel, one version.

## 3. The stable ABI (`abi3`)

`rust/Cargo.toml` builds PyO3 with the `abi3-py312` feature:

```toml
pyo3 = { version = "0.23", features = ["extension-module", "abi3-py312"] }
```

This links against Python's [stable ABI](https://docs.python.org/3/c-api/stable.html)
floor at 3.12 (matching `requires-python = ">=3.12"`), so **one compiled
wheel per platform/arch works for every Python ≥ 3.12** — no separate wheel
per Python minor version. That's what keeps the release matrix at 6 wheels
(§5) instead of 6 × (number of supported Python versions). You'll see this in
a built wheel's filename: `py_kodo-X.Y.Z-cp312-abi3-<platform tag>.whl`.

## 4. Versioning — unchanged

The `major.minor.BUILDNUM` scheme (the `build_number` file, stamped into
`pyproject.toml` + `__init__.py` by `scripts/pre_build.py`, incremented by
`scripts/post_build.py` after a successful `hatch run build`) is **untouched**
by this change — see `README.md`'s release-workflow section for the full
mechanics. It works unchanged because it only ever text-stamps
`pyproject.toml`'s `[project] version` before the build backend runs; it was
never a hatchling-specific plugin hook.

`[project] version` stays a plain **static** string (not `dynamic`) in
`pyproject.toml`. This matters: maturin only reads a version out of
`Cargo.toml` when `pyproject.toml` marks `version` as `dynamic` — since it
isn't, `pyproject.toml` stays the single source of truth and
`rust/Cargo.toml`'s own `version = "0.1.0"` field is unused for packaging
purposes (Cargo still requires *some* value there; it's never published to
crates.io independently).

There used to be a second, more elaborate `bN`-prerelease build-session
scheme implemented as a hatchling `BuildHookInterface` plugin
(`hatch_build.py`). It was dead code — never wired into any
`[tool.hatch.build.hooks]` table — and has been deleted; it was not the live
versioning mechanism and had nothing to migrate.

## 5. CI wheel matrix

`.github/workflows/ci.yml`'s `build-wheels` job builds all 6 targets in
parallel via [`PyO3/maturin-action`](https://github.com/PyO3/maturin-action),
gated the same way the old single-wheel build was — only on a push to `main`
that changed `build_number`:

| Target | Runner | How |
| --- | --- | --- |
| `x86_64-unknown-linux-gnu` | `ubuntu-latest` | `maturin build --zig --compatibility manylinux2014` |
| `aarch64-unknown-linux-gnu` | `ubuntu-latest` | same, cross-compiled |
| `x86_64-apple-darwin` | `macos-latest` | native/Xcode cross |
| `aarch64-apple-darwin` | `macos-latest` | Xcode cross (Apple Silicon) |
| `x86_64-pc-windows-msvc` | `windows-latest` | native |
| `aarch64-pc-windows-msvc` | `windows-latest` | `cargo-xwin` cross, auto-installed by maturin-action |

**Why `--zig` for Linux, tagged `manylinux2014` (not musl):** cross-linking
against an old glibc via [Zig](https://ziglang.org/)'s bundled headers avoids
needing a manylinux Docker container, while still producing a wheel that
installs on essentially every mainstream glibc-based Linux distro (Ubuntu,
Debian, Fedora, …) — this is the same approach ruff/pydantic-core/etc. use.
A musl-target build was deliberately **not** chosen: it produces a
`musllinux`-tagged wheel that pip/uv only select on musl-based systems (e.g.
Alpine) — not on a standard glibc Ubuntu/Debian machine — so it would leave
most Linux end users still needing a source build.

Both macOS targets build from a single `macos-latest` runner — Xcode's
toolchain cross-links `aarch64-apple-darwin` and `x86_64-apple-darwin` from
either host architecture without extra tooling. Windows ARM64
(`aarch64-pc-windows-msvc`) is cross-compiled from the same `windows-latest`
x86_64 runner via `cargo-xwin`, which `maturin-action` installs automatically
when it sees an `-msvc` target that doesn't match the runner's native arch.

A separate `build-sdist` job builds the one source distribution (needed for
platforms/versions with no matching prebuilt wheel — it still requires Rust
to build from, unlike the wheels). A final `combine` job downloads every
per-target wheel artifact plus the sdist into one `dist/` and re-uploads it
as the `kodo-latest` artifact — the same name/shape `publish-kodo.yml`
already expects, so that workflow needed no changes.

The `check` job (runs on every push/PR, not just releases) now installs a
Rust toolchain (`dtolnay/rust-toolchain@stable`) before `hatch run check` —
`hatch`'s environment creation does an editable install of the project,
which now invokes maturin's `build_editable` and needs `cargo` on `PATH`.

## 6. Local development

Prerequisites: Python ≥ 3.12, [hatch](https://hatch.pypa.io) ≥ 1.17, and a
Rust toolchain (`rustup` — see [rust-lang.org/tools/install](https://www.rust-lang.org/tools/install)).
`mise.toml` (local-only, gitignored, not shipped to other contributors or CI)
can pin `rust = "stable"` alongside the existing `node`/`python` pins if you
use mise.

- `hatch run test` / `hatch run lint` / `hatch run typecheck` — unchanged
  commands; `hatch`'s env creation now also compiles `rust/` the first time
  (and whenever `rust/` changes) as part of the editable install.
- `maturin develop --release` — rebuild just the Rust extension in place
  inside the current venv, without going through hatch's env machinery.
  Fastest loop while iterating on Rust code.
- `hatch run build` (`scripts/pre_build.py` → checks → `hatch build` →
  `scripts/post_build.py`) — the full local release pipeline; produces one
  native wheel for your own platform (`dist/py_kodo-*.whl`) and advances
  `build_number`. Unchanged from before this change, other than now
  containing the compiled `rust_native` extension.
- `scripts/build.sh` / `scripts/build.ps1` (bare `hatch build`, no version
  stamp) — unaffected, still the quick "does it build" check during
  development.

## 7. Adding a new Rust-backed function

1. Add the function to `rust/src/lib.rs`, `#[pyfunction]`-annotated.
2. Register it in the `rust_native` `#[pymodule]` function
   (`m.add_function(wrap_pyfunction!(your_fn, m)?)?;`).
3. Add its signature to `src/kodo/rust_native.pyi` so mypy sees it.
4. `maturin develop --release` to rebuild, then use it from Python like any
   other import: `from kodo.rust_native import your_fn`.

## 8. Demo: `kodo.rust_native.hello_world`

`rust/src/lib.rs` ships one function, `hello_world() -> str`, wired to
`python -m kodo --rust-hello` (see `src/kodo/__main__.py`) purely to prove
the whole pipeline — Rust source → compiled extension → packaged wheel →
importable from Python — works end to end. It has no other purpose; it's the
starting point for further Rust work, not a template to imitate feature-wise.
