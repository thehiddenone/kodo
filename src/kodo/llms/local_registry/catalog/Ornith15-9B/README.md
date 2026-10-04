# Ornith15-9B — catalog notes

The small member of the Ornith 1.5 family. Unlike its 35B-A3B sibling this
is a **dense** build: its GGUF records `general.architecture = qwen35`, not
`qwen35moe`, so it takes the `context-qwen35` knob — the
dense Qwen YaRN knob, whose `--override-kv` targets
`qwen35.context_length`. Pointing the MoE knob at it would write an
override for a metadata key this GGUF does not have, llama.cpp would ignore
it silently, and the extended 512K/1M options would appear to work while the
KV cache stayed capped at the trained 262144 tokens.

The upstream model is multimodal and bartowski's repo ships
`mmproj-Ornith-1.5-9B-{f16,bf16}.gguf` companions, but
`LocalLLMEntry` has no field to declare one, so every entry
here is registered text-only. Unlike the 35B-A3B build there are no MTP
layers in these quants (bartowski's model card lists "Speculative decoding:
no"), so this family declares no MTP knob and every entry keeps
`mtp_supported` at its default `False` — see
`_knobs_mtp.py`.

The quant ladder mirrors the five rungs Ornith 1.0 9B ships
(BF16/Q8_0/Q6_K/Q5_K_M/Q4_K_M) plus three smaller builds. The tail is
Q3_K_L/IQ3_M/Q2_K rather than the 35B family's Q3_K_XL/IQ3_M/Q2_K_L: at 9B
the embedding and output tensors are a much larger share of the file, so
bartowski's `_L`/`_XL` variants — which hold those at Q8_0 — do not
shrink here. Q3_K_XL is 6.00 GB and Q2_K_L is 5.06 GB, both *larger* than
this family's own 5.91 GB Q4_K_M and 5.11 GB Q3_K_L, which would make them
pointless as low-memory rungs.
