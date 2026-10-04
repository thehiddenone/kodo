# Qwen36-35B-A3B — catalog notes

Verified to carry Multi-Token Prediction layers: the GGUF header declares
`qwen35moe.nextn_predict_layers = 1` (checked directly on the UD-Q8_K_XL
quant), so every entry here lists
the `spec-decoding-mtp` knob and sets `"mtp_supported": true`.
