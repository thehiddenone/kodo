# Qwen38-27B — catalog notes

Verified to carry Multi-Token Prediction layers despite the repo not being
branded `-MTP-GGUF` the way the Qwen3.5/3.6 unsloth repos are: the GGUF
header declares `qwen35.nextn_predict_layers = 1` on both the Q4_0 quant
and the most aggressive one in the ladder (UD-IQ2_XXS), so the layers are
baked in at every rung, not just some. Every entry here therefore lists
the `spec-decoding-mtp` knob and sets `"mtp_supported": true`.

The repo also ships one **standalone** MTP head,
`MTP/mtp-Qwen3.8-27B-Q4_0.gguf` (1.37 GB), listed in `mtp_sidecars.json`
(doc/LLM_REGISTRY.md §4.0a). It is self-contained — its header carries
`token_embd.weight` and `output.weight` next to the `blk.64.nextn.*` block —
so mainline llama.cpp loads it with `--model-draft`. With heads present, every
entry here offers the MTP head picker (*Off* / *Built-in layers* / *Q4_0
head*) in place of the `spec-decoding-mtp` checkbox; a checkbox that was on
reads as *Built-in layers*. The head needs llama.cpp b11330: before
ggml-org/llama.cpp#29761 the speculative init opened the target model's path
instead of the `-md` file.
