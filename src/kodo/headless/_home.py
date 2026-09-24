"""The isolated ``~/.kodo`` a headless run's server sees — built from an allowlist.

The server child runs with ``HOME`` pointed at a throwaway directory, so the
user's real ``~/.kodo`` is never written. Unlike ``kodo.validator``'s
``clone_kodo_home`` (a denylist: copy everything not excluded), this copies
nothing it does not name — a real ``~/.kodo`` holds GBs of checkpoint mirrors
and a venv, plus secrets, none of which a headless run needs:

- **copied** (small files the run itself changes): ``etc/settings.json`` with
  the model selection merged in, and ``etc/local-llm-registry.json`` — or the
  ``--registry-file`` (a Harbor read-only mount of the host's registry), copied
  rather than linked so nothing can write through to the original;
- **symlinked** (read-only use): ``bin`` (rg/fd/uv), ``agents``, ``skills``;
- **omitted**: everything else — including ``llama.cpp`` (models live with
  ``kodo-llama-server``; the headless server only ever attaches by URL),
  ``checkpoints``, ``venv``, ``sessions``, ``logs`` and every credential file.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import cast

__all__ = ["build_headless_home"]

_SYMLINKED = ("bin", "agents", "skills")
_SETTINGS = Path("etc") / "settings.json"
_REGISTRY = Path("etc") / "local-llm-registry.json"


def build_headless_home(
    home_dir: Path,
    *,
    model: str,
    template_kodo_dir: Path | None,
    registry_file: Path | None = None,
) -> Path:
    """Create ``home_dir/.kodo`` for a headless run's server.

    Args:
        home_dir (Path): The directory the server will see as ``HOME``.
        model (str): Local-registry entry to select (``models.local``).
        template_kodo_dir (Path | None): The user's real ``~/.kodo`` to take
            settings, registry and links from; ``None`` or missing gives an
            empty home (the server fills in defaults).
        registry_file (Path | None): Registry to use instead of the
            template's (e.g. a read-only mount of the host's).

    Returns:
        Path: The isolated ``.kodo`` directory.

    Raises:
        FileNotFoundError: *registry_file* is given but does not exist.
    """
    kodo_dir = home_dir / ".kodo"
    (kodo_dir / "etc").mkdir(parents=True, exist_ok=True)
    template = template_kodo_dir if template_kodo_dir is not None else None
    if template is not None and not template.is_dir():
        template = None

    if template is not None:
        for name in _SYMLINKED:
            source = template / name
            if source.exists() and not (kodo_dir / name).exists():
                os.symlink(source, kodo_dir / name, target_is_directory=source.is_dir())

    registry_source = registry_file
    if registry_source is not None and not registry_source.is_file():
        raise FileNotFoundError(f"Registry file does not exist: {registry_source}")
    if registry_source is None and template is not None and (template / _REGISTRY).is_file():
        registry_source = template / _REGISTRY
    if registry_source is not None:
        shutil.copyfile(registry_source, kodo_dir / _REGISTRY)

    settings: dict[str, object] = {}
    if template is not None and (template / _SETTINGS).is_file():
        loaded = json.loads((template / _SETTINGS).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            settings = cast(dict[str, object], loaded)
    models = settings.get("models")
    models_map = dict(cast(dict[str, object], models)) if isinstance(models, dict) else {}
    models_map["local"] = model
    settings["models"] = models_map
    settings["mode"] = "local"
    (kodo_dir / _SETTINGS).write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return kodo_dir
