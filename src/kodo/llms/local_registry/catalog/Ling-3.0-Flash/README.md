# Ling-3.0-Flash — catalog notes

inclusionAI's (Ant Group) hybrid-linear sparse MoE, released 2026-08
(https://huggingface.co/inclusionAI/Ling-3.0-flash, MIT): 124B total / 5.1B
active parameters, 512 routed experts (8 active) plus one shared expert.
Quants are bartowski's (https://huggingface.co/bartowski/Ling-3.0-flash-GGUF),
made with llama.cpp b10472. Every build above Q2_K_L is split into 2-7
shards; `filename` names the first one.

**Architecture: `bailingmoe3`**, read off the GGUF header rather than the
card. Of the 42 transformer layers, 35 are Kimi Delta Attention (KDA, a
linear-attention recurrence with a fixed-size state) and only 7 — every
sixth — are MLA. The header's `attention.head_count_kv` array is `1` on
exactly those 7 plus the MTP layer and `0` everywhere else, so the KV cache
holds 8 layers of a 576-wide MLA latent: about 1-2GB at 128K context
whatever the quant. That is why every entry's `gpu_tip`/`mac_tip` total is
just the file size plus ~3-4GB.

**Context: native 262144** (`bailingmoe3.context_length`, the end of the
8K → 32K → 256K training schedule). No context knob: no shipped knob targets
`bailingmoe3`, and nothing beyond the trained length is offered.

**MTP: every entry, built in.** All eight headers carry
`bailingmoe3.nextn_predict_layers = 1` and no
`nextn_shared_target_tensors`, so every entry sets `mtp_supported` and
lists `spec-decoding-mtp` (`--spec-type draft-mtp`, supported since the
architecture landed in ggml-org/llama.cpp#26608). bartowski stores the MTP
layer at Q4_0 in every imatrix quant except Q8_0, on purpose. A separate
DSpark draft model also exists (`inclusionAI/Ling-3.0-flash-dspark`,
ggml-org/llama.cpp#27508) but is not wired: it is a second download, not a
head.

**Thinking tiers: `qwen_reasoning_budget`** (`_thinking.py`). The embedded
"Bailing V3" template thinks by default (`enable_thinking` undefined →
`thinking_option = 'on'`) and opens `<think>` in the generation prompt, so no
`chat_template_kwargs` are needed; llama.cpp's dedicated Ling 3.0 parser
(`common/parsers/ling3.cpp`) declares `<think>` / `</think>` as the thinking
tags, which is what `--reasoning-budget` keys off. The budget scale is the
512..16384 one the other mid-size MoE families use.

**`llamacpp_version` is 11063, not bartowski's 10472.** b10472 loads the
model, but its generated parser treated a tool call emitted before
`</think>` as reasoning, so the client got empty content and no tool calls,
and a stray invalid-UTF-8 byte piece (which this tokenizer samples now and
then) failed the whole parse with an HTTP 500. Both break agentic use. The
fixes are ggml-org/llama.cpp#28682 (the dedicated parser, b11057) and #29161
(invalid UTF-8 in the PEG AST, merged as b11063 itself — it replaced the
closed #28724).

**Quant ladder** mirrors Ornith 1.5 35B-A3B's (also bartowski):
BF16/Q8_0/Q6_K/Q5_K_M/Q4_K_M plus the Q3_K_XL/IQ3_M/Q2_K_L low-RAM tail.
Sampling is left on the shared defaults like every other family, although
the card recommends temperature 0.6, top_p 0.95, top_k 20.
