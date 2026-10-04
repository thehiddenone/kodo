# Ornith10-9B — catalog notes

A **dense** build despite the MoE sibling in the same generation: its GGUF
records `general.architecture = qwen35`, so it takes
the `context-qwen35` knob, not the MoE one that
the `Ornith10-35B-A3B` entries use.

No MTP knob: the base model's own source weights (`ornith-ai/Ornith-1.0-9B`)
have zero `mtp.*` tensors, unlike the 1.5 generation's Qwen3.5 lineage —
see `_knobs_mtp.py`. Every entry here keeps
`mtp_supported` at its default `False`.
