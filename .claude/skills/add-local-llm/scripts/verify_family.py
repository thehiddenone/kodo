#!/usr/bin/env python3
"""Load kodo's real registry and print what it serves for one base_llm family.

Run from the kodo repo root with the project environment:
    hatch run python .claude/skills/add-local-llm/scripts/verify_family.py <base_llm>

Prints each served entry, the family's standalone MTP heads
(``mtp_sidecars.json``) and, per entry, which MTP control it is served with:
the built-in checkbox, the head picker (with its options), or none. Uses a
throwaway kodo dir, so no user catalog file can shadow a shipped entry.
Importing the registry runs the shipped-catalog validators, so a malformed
file fails here the same way it would fail kodo's startup.

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
        MTP_SPEC_DECODE_KNOB,
        get_local_registry,
        get_mtp_sidecars,
        local_thinking_default_tier,
        local_thinking_family,
        local_thinking_tiers,
        mtp_head_knob_id,
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
    kodo_dir = Path(tempfile.mkdtemp())
    registry = get_local_registry(kodo_dir)
    entries = list(registry.values()) if isinstance(registry, dict) else list(registry)
    family = [e for e in entries if getattr(e, "base_llm", "") == base_llm]
    if not family:
        sys.exit(f"no served entry has base_llm={base_llm!r} — check the catalog directory name")
    picker_ids = {mtp_head_knob_id(base_llm, builtin=b) for b in (True, False)}
    for e in family:
        knobs = [k.id for k in e.knobs]
        picker = next((k for k in e.knobs if k.id in picker_ids), None)
        if picker is not None:
            mtp_control = f"head picker {picker.id} options={[o.id for o in picker.options]}"
        elif MTP_SPEC_DECODE_KNOB.id in knobs:
            mtp_control = "built-in checkbox"
        else:
            mtp_control = "none"
        print(
            f"{e.name}\n  {e.quant_type} {e.size_hint} ctx={e.context_window} "
            f"llama.cpp>=b{e.llamacpp_version} mem={e.min_memory}/{e.memory} "
            f"mtp_supported={e.mtp_supported}\n  knobs={knobs} defaults={e.knob_defaults}"
            f"\n  MTP control: {mtp_control}"
        )
    print(f"\n{len(family)} entries for {base_llm}; {len(entries)} served in total")
    heads = get_mtp_sidecars(kodo_dir, base_llm)
    print(f"MTP heads ({len(heads)}):" if heads else "MTP heads: none")
    for h in heads:
        print(
            f"  {h.id}: {h.repo_id}/{h.filename} {h.quant_type} {h.size_hint} "
            f"llama.cpp>=b{h.llamacpp_version}"
        )
    print(
        "thinking:",
        local_thinking_family(base_llm),
        local_thinking_tiers(base_llm),
        "default:",
        local_thinking_default_tier(base_llm) or "(none)",
    )


if __name__ == "__main__":
    main()
