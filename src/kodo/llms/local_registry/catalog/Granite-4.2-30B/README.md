# Granite-4.2-30B — catalog notes

IBM's dense reasoning model, released 2026-08
(https://huggingface.co/ibm-granite/granite-4.2-30b, Apache 2.0): about 30B
parameters (bartowski's card says 29B), post-trained from
`ibm-granite/granite-4.1-30b-base` with SFT and then GRPO, including agentic RL
phases for software engineering, terminal use and web search. Quants are
bartowski's (https://huggingface.co/bartowski/granite-4.2-30b-GGUF), made with
llama.cpp b10603. Only BF16 is split (2 shards); `filename` names the first.

**Architecture: `granite`**, read off the GGUF header of all eight shipped
quants (2026-10-11). It is the long-supported Granite dense architecture, not
4.0's hybrid Mamba (`granitehybrid`): 64 layers, every one full GQA attention
with 32 query / 8 KV heads of dimension 128, no sliding window, no recurrent
layers. That makes the KV cache the heaviest of any family of this size:
64 × 8 × (128 + 128) elements per token, so at 128K context about 18GB at the
default q8_0 cache and about 34GB at f16 (the BF16 entry's default). Every
entry's total is file size + ~20GB (BF16: + ~36GB), which is why the low-RAM
tail saves so little, and why the tips on the two smallest rungs point at
the KV cache knob's `q4_0` option. The card says RoPE θ = 10,000,000; the
header and `config.json` both say 50,000,000. The header is what llama.cpp
uses.

**Context: native 131072** (`granite.context_length`). The card advertises
a "long-context extension to 512K", but `config.json` has
`rope_scaling: null` and gives no YaRN factor or original length, so nothing
says how to extend it. No shipped context knob targets `granite`, and none is
offered.

**MTP: none.** No quant has `granite.nextn_predict_layers`, and the repo has
no standalone heads (`hf_quants.py` lists only the imatrix besides the
quants). bartowski's card says "Speculative decoding: no". All entries set
`mtp_supported: false`.

**Thinking tiers: `qwen_reasoning_budget`** (`_thinking.py`). The embedded
template thinks by default (`enable_thinking` defaults to `True`, and the
generation prompt ends `<|im_start|>assistant\n<think>\n`), so no
`chat_template_kwargs` are needed. Its tool calls are the Qwen3-Coder XML
format (`<tool_call>\n<function=…>\n<parameter=…>`), and llama.cpp's
template detection (`common/chat.cpp`) routes it to the dedicated
Qwen3-Coder parser (`common/parsers/qwen3-coder.cpp`). Because the template
contains `<think>`, that parser takes its reasoning branch and declares
`<think>` as the start tag and `</think>` / `<tool_call>` as end tags, which
is what `--reasoning-budget` keys off. A tool call opened before `</think>`
still ends the reasoning. The budget scale copies Qwen38-27B's
(512..16384), and the default `high` tier (8192) matches the card's
`max_new_tokens = 8192` for thinking mode.
The template also takes `reasoning_effort` / `low_effort`, but that is only
binary (it appends `{reasoning effort: low}` to the last user turn when the
value is `"low"`), so it is not wired. The budget tiers already cover it.

**`llamacpp_version` is 11063, not bartowski's 10603.** The architecture and
the Qwen3-Coder parser's thinking branch both predate b10603, so that build
loads the model and parses its output. These later fixes on the parser path
this template uses matter to an agent:
- b10625, ggml-org/llama.cpp#27679: scopes the Qwen3-Coder "missing
  `<tool_call>`" workaround to non-thinking templates, so a bare
  `<function=name>` in a thinking model's content is no longer taken as a
  tool call;
- b10936, #28742: typed parsing of union-with-string and nullable tool
  arguments in this parser;
- b10996, #28869: when the reasoning budget runs out, the forced end tag is
  `\n</think>`, which kodo's budget tiers trigger directly;
- b11063, #29161: an invalid-UTF-8 byte piece in the output no longer fails
  the whole PEG parse with HTTP 500. Granite's byte-level BPE tokenizer can
  sample such pieces, as Ling-3.0-Flash's did.

The pin is the newest of these. No open upstream issue names Granite 4.2
(searched 2026-10-11).

**Quant ladder** (maintainer's choice, 2026-10-11) mirrors Ornith 1.5
35B-A3B's and Ling-3.0-Flash's (also bartowski):
BF16/Q8_0/Q6_K/Q5_K_M/Q4_K_M plus the large-model low-RAM tail
Q3_K_XL/IQ3_M/Q2_K_L. At 30B the `_L`/`_XL` variants are only ~0.4GB larger
than Q3_K_L/Q2_K, so no size inversion.

**Sampling: IBM's recommendation ships as defaults** (maintainer's choice,
2026-10-11). This is the first family to put sampling knobs in
`knob_defaults`. The card says "Use `temperature=1.0` and `top_p=0.95`
across **all tasks and serving backends**, including general chat,
reasoning, and tool calling", and the GGUF header carries the same
`general.sampling.temp = 1.0` / `general.sampling.top_p = 0.95`. Every
entry therefore defaults `temperature` to `t10` (1.0) and
`nucleus-sampling` to `p095` (top-p 0.95, top-k disabled, matching vLLM's
behavior). Tail culling stays at the shared default. Users can move either
knob back.

**Not wired:** vision (the model is text-only anyway), the 512K extension
(above), and the binary `low_effort` switch (above).

**Update 2026-10-11 (later the same day): min-p is now 0 as well**
(maintainer's choice, made while adding `Granite-4.2-8B`). The sampling
paragraph above left tail culling at the shared default. That keeps
llama.cpp's own min-p 0.05 in force, which IBM's recipe does not include:
the card names no min-p, and its vLLM and SGLang serving sections default
min-p to 0. The `tail-culling: none` option (`--min-p 0.0`) did not exist when
this family shipped. It was added for Ling-3.0-Flash later the same day.
Every entry now also defaults `tail-culling` to `none`, so a launch
reproduces the card's recipe exactly. Users who never moved the knob pick
up the new default, and an explicit choice is kept.
