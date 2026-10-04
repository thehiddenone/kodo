# Qwen35-9B — catalog notes

Verified to carry Multi-Token Prediction layers: the GGUF header declares
`qwen35.nextn_predict_layers = 1` (checked directly on the BF16 quant, not
inferred from the repo's own `-MTP-GGUF` name), so every entry here lists
the `spec-decoding-mtp` knob and sets `"mtp_supported": true`.
