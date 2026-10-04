# MuseGlimmer-30B — catalog notes

Meta's dense 29.6B-param open-weight distillation of their cloud "Muse Spark"
model (https://huggingface.co/unsloth/Muse-Glimmer-30B-GGUF), released
2026-08. Architecturally a Gemma/Nemotron-style local/global mix rather than
plain dense attention: 52 layers, 1-in-4 full attention with the rest capped
to a 2048-token sliding window, plus 2 KV heads (GQA) — which is what keeps
the KV cache small relative to file size even at the full 131K context (see
the per-entry `gpu_tip`/`mac_tip` notes).

**Deliberately not in either thinking-tier family** (`_thinking`).
Muse Glimmer's reasoning strength (low/medium/high/xhigh) is documented as
being set by a literal `Reasoning strength: <value>` line in the system
prompt — not a CLI token budget (the Qwen family) and not a
`chat_template_kwargs` field consumed by the GGUF's own Jinja template (the
GPT-OSS family); the base model's `tokenizer_config.json` has no
`chat_template` field at all. Wiring that would need a third thinking-tier
mechanism (system-prompt injection) touching code well beyond this package
(`_llama.py` and wherever the request's system prompt gets assembled), and
upstream llama.cpp's own support is still catching up — model support landed
in ggml-org/llama.cpp#26841 (2026-08-10) and a tool-call parsing fix in #26879
(2026-08-11), but the PR that actually sets Muse Glimmer's thinking tags
(#27475) is still open/unmerged as of 2026-08-21. Revisit once that lands and
kodo has a system-prompt-injection mechanism to hang it on.
