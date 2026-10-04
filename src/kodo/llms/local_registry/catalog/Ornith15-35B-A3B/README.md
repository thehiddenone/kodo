# Ornith15-35B-A3B — catalog notes

The sparse-MoE member of the Ornith 1.5 family, and architecturally the
direct successor to Ornith 1.0 35B-A3B: the same `qwen35moe` GGUF
architecture key, the same 262144-token native context, and the same
attention shape (2 KV heads, 256-wide keys and values, full attention on
every fourth layer), so it reuses
the `context-qwen35moe` knob unchanged and its KV-cache
footprint at 128K context matches the 1.0 entries' (~17GB at the default
q8_0 cache, ~34GB at f16).

Two things separate it from the 1.0 family:

- **MTP.** These quants carry Multi-Token Prediction layers (the GGUF's
  `qwen35moe.nextn_predict_layers`), which llama.cpp can drive as a
  built-in draft model for speculative decoding — see
  `_knobs_mtp.py` for the `spec-decoding-mtp` knob, which
  every entry here lists, and `mtp_supported`,
  which every entry here sets `True`.
- **Vision.** The upstream model is multimodal and bartowski's repo ships
  `mmproj-Ornith-1.5-35B-A3B-{f16,bf16}.gguf` companions.
  `LocalLLMEntry` has no field to declare an mmproj
  companion, so every entry here is registered text-only and the projector
  files are simply not downloaded. Wiring mmproj through the catalog,
  the download manager and the `--mmproj` launch arg is a separate piece
  of work.

The quant ladder mirrors the five rungs the Ornith 1.0 families ship
(BF16/Q8_0/Q6_K/Q5_K_M/Q4_K_M), extended downward with three smaller builds
so a 32-36GB machine can run the model at all.
