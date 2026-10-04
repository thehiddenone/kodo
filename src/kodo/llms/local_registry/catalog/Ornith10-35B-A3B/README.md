# Ornith10-35B-A3B — catalog notes

No MTP knob: `ornith-ai/Ornith-1.0-35B-GGUF`'s own source model is a gated
HuggingFace repo that could not be checked for `mtp.*` tensors the way
every other MTP-candidate family in this catalog was (see `_knobs_mtp.py`)
— its dense 9B sibling has none, but that isn't proof either way for this
size. Left unsupported rather than guessed; every entry here keeps
`mtp_supported` at its default `False`.
