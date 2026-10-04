# Qwen38-27B — catalog notes

Verified to carry Multi-Token Prediction layers despite the repo not being
branded `-MTP-GGUF` the way the Qwen3.5/3.6 unsloth repos are: the GGUF
header declares `qwen35.nextn_predict_layers = 1` on both the Q4_0 quant
and the most aggressive one in the ladder (UD-IQ2_XXS), so the layers are
baked in at every rung, not just some. Every entry here therefore lists
the `spec-decoding-mtp` knob and sets `"mtp_supported": true`.
