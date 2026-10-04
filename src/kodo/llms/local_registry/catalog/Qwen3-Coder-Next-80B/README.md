# Qwen3-Coder-Next-80B — catalog notes

No MTP knob: unlike the general Qwen3-Next-80B-A3B-Instruct/Thinking
releases, this coder-specialized variant's base model
(`Qwen/Qwen3-Coder-Next`) has zero `mtp.*` tensors in its own source
weights — the architecture family carries MTP, but this particular release
doesn't (see `_knobs_mtp.py`). Every entry here keeps
`mtp_supported` at its default `False`.
