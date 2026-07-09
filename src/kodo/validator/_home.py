"""Isolated ``~/.kodo`` home preparation for validation runs.

A validation run must never touch the developer's real ``~/.kodo`` (nor race
the real singleton server's discovery file). The harness therefore launches
``python -m kodo.server`` with ``HOME``/``USERPROFILE`` pointed at a scratch
directory, and this module populates that scratch home from a **template**
kodo home:

* heavy, immutable entries (``bin/``, ``llama.cpp/`` — binaries and GGUF
  models) are **symlinked** so local inference works without copying
  gigabytes;
* runtime state that must start fresh (``sessions/``, ``logs/``, the
  ``kodo-server`` discovery file) is **skipped**;
* everything else (``etc/settings.json``, the local LLM registry, …) is
  **copied**, so the run can mutate its settings freely.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

__all__ = ["DEFAULT_SKIP_ENTRIES", "DEFAULT_SYMLINK_ENTRIES", "clone_kodo_home"]

_log = logging.getLogger(__name__)

# Template entries shared with the scratch home by symlink (large + read-mostly).
DEFAULT_SYMLINK_ENTRIES: tuple[str, ...] = ("bin", "llama.cpp")

# Template entries never carried into the scratch home (per-run state).
DEFAULT_SKIP_ENTRIES: tuple[str, ...] = ("sessions", "logs", "kodo-server")


def clone_kodo_home(
    home_dir: Path,
    template_kodo_dir: Path | None = None,
    *,
    symlink_entries: tuple[str, ...] = DEFAULT_SYMLINK_ENTRIES,
    skip_entries: tuple[str, ...] = DEFAULT_SKIP_ENTRIES,
    settings_overrides: dict[str, object] | None = None,
) -> Path:
    """Materialize an isolated kodo home under *home_dir*.

    Creates ``home_dir/.kodo`` from *template_kodo_dir* (typically the real
    ``~/.kodo``): entries named in *symlink_entries* become symlinks back into
    the template, entries named in *skip_entries* are omitted, and everything
    else is copied. With no template, a minimal empty skeleton is created and
    the server fills in defaults on first start.

    Args:
        home_dir (Path): Directory the server process will see as ``$HOME``.
        template_kodo_dir (Path | None): Existing ``.kodo`` directory to clone.
        symlink_entries (tuple[str, ...]): Top-level template entries to symlink.
        skip_entries (tuple[str, ...]): Top-level template entries to omit.
        settings_overrides (dict[str, object] | None): Keys deep-merged into the
            cloned ``etc/settings.json`` (e.g. force ``mode``/``models`` per run).

    Returns:
        Path: The scratch ``.kodo`` directory (``home_dir/.kodo``).

    Raises:
        FileNotFoundError: If *template_kodo_dir* is given but does not exist.
        OSError: If a symlink cannot be created (e.g. Windows without
            developer mode) or a copy fails.
    """
    kodo_dir = home_dir / ".kodo"
    kodo_dir.mkdir(parents=True, exist_ok=True)

    if template_kodo_dir is not None:
        template = template_kodo_dir.resolve()
        if not template.is_dir():
            raise FileNotFoundError(f"Template kodo home does not exist: {template}")
        for child in sorted(template.iterdir()):
            if child.name in skip_entries:
                continue
            dest = kodo_dir / child.name
            if child.name in symlink_entries:
                dest.symlink_to(child, target_is_directory=child.is_dir())
            elif child.is_dir():
                shutil.copytree(child, dest, symlinks=True)
            else:
                shutil.copy2(child, dest)
        _log.info("Cloned kodo home %s -> %s", template, kodo_dir)

    # Fresh per-run state directories the server expects.
    (kodo_dir / "sessions").mkdir(exist_ok=True)
    (kodo_dir / "logs").mkdir(exist_ok=True)
    (kodo_dir / "etc").mkdir(exist_ok=True)

    if settings_overrides:
        _merge_settings(kodo_dir / "etc" / "settings.json", settings_overrides)

    return kodo_dir


def _merge_settings(settings_path: Path, overrides: dict[str, object]) -> None:
    """Deep-merge *overrides* into the JSON file at *settings_path*.

    Args:
        settings_path (Path): The cloned ``etc/settings.json`` (may not exist).
        overrides (dict[str, object]): Keys to merge in; nested dicts merge
            recursively, everything else replaces.
    """
    current: dict[str, object] = {}
    if settings_path.exists():
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            current = loaded
    merged = _deep_merge(current, overrides)
    settings_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def _deep_merge(base: dict[str, object], overrides: dict[str, object]) -> dict[str, object]:
    """Return *base* with *overrides* merged in (nested dicts merge recursively).

    Args:
        base (dict[str, object]): Original mapping (not mutated).
        overrides (dict[str, object]): Values that win over *base*.

    Returns:
        dict[str, object]: The merged mapping.
    """
    merged = dict(base)
    for key, value in overrides.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(
                {str(k): v for k, v in existing.items()},
                {str(k): v for k, v in value.items()},
            )
        else:
            merged[key] = value
    return merged
