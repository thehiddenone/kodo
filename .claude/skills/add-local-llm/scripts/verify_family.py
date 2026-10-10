#!/usr/bin/env python3
"""Load kodo's real registry and print what it serves for one base_llm family.

Run from the kodo repo root with the project environment:
    hatch run python .claude/skills/add-local-llm/scripts/verify_family.py <base_llm>

Uses a throwaway kodo dir, so no user catalog file can shadow a shipped
entry. Importing the registry runs the shipped-catalog validators, so a
malformed file fails here the same way it would fail kodo's startup.

This script goes through kodo's public API (``kodo.llms.local_registry``). If
an import below fails, the API moved: read that package's ``__all__`` and fix
this script (and the skill) rather than reaching into ``_private`` modules.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

try:
    from kodo.llms.local_registry import (
        get_local_registry,
        local_thinking_default_tier,
        local_thinking_family,
        local_thinking_tiers,
    )
except ImportError as exc:
    sys.exit(
        f"kodo.llms.local_registry API changed ({exc}). Read its __init__.py __all__ "
        "and update this script and the add-local-llm skill."
    )


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    base_llm = sys.argv[1]
    registry = get_local_registry(Path(tempfile.mkdtemp()))
    entries = list(registry.values()) if isinstance(registry, dict) else list(registry)
    family = [e for e in entries if getattr(e, "base_llm", "") == base_llm]
    if not family:
        sys.exit(f"no served entry has base_llm={base_llm!r} — check the catalog directory name")
    for e in family:
        knobs = [k.id for k in e.knobs]
        print(
            f"{e.name}\n  {e.quant_type} {e.size_hint} ctx={e.context_window} llama.cpp>=b{e.llamacpp_version} "
            f"mem={e.min_memory}/{e.memory} mtp={e.mtp_supported}\n  knobs={knobs} defaults={e.knob_defaults}"
        )
    print(f"\n{len(family)} entries for {base_llm}; {len(entries)} served in total")
    print(
        "thinking:",
        local_thinking_family(base_llm),
        local_thinking_tiers(base_llm),
        "default:",
        local_thinking_default_tier(base_llm) or "(none)",
    )


if __name__ == "__main__":
    main()
