# Qwen36-27B — catalog notes

Support for Multi-Token Prediction splits **within** this family, by quant
repo rather than by model — the first trap of its kind in this catalog, so
it's called out explicitly rather than left to be rediscovered. The four
`unsloth/Qwen3.6-27B-MTP-GGUF`-backed entries have it: their GGUF header
declares `qwen35.nextn_predict_layers = 1` (checked directly on the
UD-Q8_K_XL quant). The `atomicchat-qwen36-27b-q8` entry does not: it's a
*different* quant of the same base model, from `AlexAtomic/qwen36-27b-GGUF`,
whose own GGUF header has no `nextn_predict_layers` key at all — that
quantization pipeline simply didn't retain the MTP tensors. So only the
Unsloth-backed entries list the `spec-decoding-mtp` knob and set
`"mtp_supported": true`; the AtomicChat entry stays at the default
`"mtp_supported": false` with no MTP knob.
