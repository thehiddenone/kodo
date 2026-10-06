# MuseGlimmer-30B — catalog notes

Meta's dense 29.6B-param open-weight distillation of their cloud "Muse Spark"
model (https://huggingface.co/unsloth/Muse-Glimmer-30B-GGUF), released
2026-08. Architecturally a Gemma/Nemotron-style local/global mix rather than
plain dense attention: 52 layers, 1-in-4 full attention with the rest capped
to a 2048-token sliding window, plus 2 KV heads (GQA) — which is what keeps
the KV cache small relative to file size even at the full 131K context (see
the per-entry `gpu_tip`/`mac_tip` notes).

**Thinking tiers: `muse_glimmer_reasoning_strength`** (`_thinking.py`) —
`low`/`medium`/`high`/`xhigh`, default `high`. Each request sends
`chat_template_kwargs: {"reasoning_strength": "<tier>"}`; the Jinja template
embedded in the GGUF renders that as the `Reasoning strength: <value>.`
system-prompt line the model card documents (falling back to `high` when the
field is absent). The field is `reasoning_strength`, not `reasoning_effort` —
llama.cpp does not translate the OpenAI spelling for this template, so the
wrong name is silently ignored. Requires llama.cpp ≥ b10353 (Meta's llama.cpp
guide); the entries pin b10549.

**Update 2026-10-05:** these notes previously kept Muse Glimmer out of every
thinking-tier family, reasoning that the base model ships no chat template,
so the system-prompt line would need a new injection mechanism, and that
upstream support waited on ggml-org/llama.cpp#27475. Both were wrong for this
GGUF: its header carries a `tokenizer.chat_template` with a `render_reasoning`
macro reading `reasoning_strength`, and #27475 (still open) only sets the
thinking tags a per-request *token* cap (`reasoning_budget_tokens`) needs — a
control this family does not use.
