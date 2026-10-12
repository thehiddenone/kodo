# Granite-4.2-8B — catalog notes

IBM's dense 8B reasoning model, released 2026-08-25
(https://huggingface.co/ibm-granite/granite-4.2-8b, Apache 2.0), post-trained
from `ibm-granite/granite-4.1-8b-base`. It shares one model card with the
3B and 30B sizes. Per the card, all three got the same SFT corpus, and only
the 30B had the second, agentic-upsampled SFT phase. Quants are bartowski's
(https://huggingface.co/bartowski/granite-4.2-8b-GGUF), made with llama.cpp
b10603, all single-file. The sibling family is `Granite-4.2-30B`, and most
of what its README says holds here. This file records what was re-checked
for the 8B and where the two differ.

**Architecture: `granite`**, read off the GGUF header of all eight shipped
quants (2026-10-11), the same dense architecture as the 30B: 40 layers, every
one full GQA attention with 32 query / 8 KV heads of dimension 128 (4096 / 32;
the header sets no explicit key/value length), no sliding window, no
recurrent layers. The KV cache is 40 × 8 × (128 + 128) elements per token,
so at 128K context about 11.4GB at the default q8_0 cache and about 21.5GB at
f16 (the BF16 entry's default). That is about twice the weights of the
Q4_K_M rung. Every entry's total is file size + ~12.5GB (BF16: + ~22.5GB), so
the low-RAM tail saves little and the tips point at the KV cache knob's
`q4_0` option (~6GB at 128K). RoPE θ is 10,000,000 in the header,
`config.json` and the card alike. The 30B's card/header disagreement does
not recur here.

**Context: native 131072** (`granite.context_length`). As with the 30B, the
card advertises a "long-context extension to 512K", but `config.json` has
`rope_scaling: null`, so nothing says how to extend it. No shipped context
knob targets `granite` (`context_knob_architectures()`), and none is offered.

**MTP: none.** No quant has `granite.nextn_predict_layers`, and the repo has
no standalone heads (`hf_quants.py` lists only the imatrix besides the
quants). bartowski's card says "Speculative decoding: no". All entries set
`mtp_supported: false`.

**Thinking tiers: `qwen_reasoning_budget`** (`_thinking.py`). The embedded
chat template is byte-identical to the 30B quants' (diffed 2026-10-11), so
the 30B README's analysis applies unchanged. The template thinks by default,
its tool calls are in the Qwen3-Coder XML format, and llama.cpp routes it to
the dedicated Qwen3-Coder parser, whose reasoning branch declares the
`<think>` tags that `--reasoning-budget` keys off. The binary `low_effort`
switch is not wired, for the same reason as on the 30B.

The budget scale is the **small-model scale** (2048/4096/8192/16384/32768,
default `high` = 16384), shared with `Ornith15-9B`, `Qwen35-9B` and
`Nanbeige4.2-3B`, rather than the 30B's 512..16384 (maintainer's choice,
2026-10-11). The card's `max_new_tokens = 8192` for thinking mode is the same
figure the 30B row's default was matched to, so here it sits at the `medium`
tier instead.

**`llamacpp_version` is 11063, not bartowski's 10603**, for the same reasons
as the 30B: an identical template on the same parser path, so the same
fixes apply. These are ggml-org/llama.cpp#27679 (b10625), then #28742
(b10936), #28869 (b10996) and #29161 (b11063). See
`catalog/Granite-4.2-30B/README.md` for what each one fixes. The 30B's pin was derived the same day. No open
upstream issue names Granite 4.2 (searched 2026-10-11).

**Quant ladder** (maintainer's choice, 2026-10-11): BF16/Q8_0/Q6_K/Q5_K_M/
Q4_K_M plus the **small-model** low-RAM tail Q3_K_L/IQ3_M/Q2_K, like
Ornith15-9B. It is deliberately not the 30B's Q3_K_XL/Q2_K_L tail. At 8B
the `_L`/`_XL` variants keep embedding and output at Q8_0, which inflates
them. Q3_K_XL is 5.11GB, within 0.1GB of Q4_K_S, and Q2_K_L is 3.98GB,
larger than IQ3_XS.

**Sampling: IBM's recommendation ships as defaults** (maintainer's choice,
2026-10-11). The card, under "Generation Parameters", says "Use
`temperature=1.0` and `top_p=0.95` across **all tasks and serving
backends**, including general chat, reasoning, and tool calling". The GGUF
header carries the same `general.sampling.temp = 1.0` /
`general.sampling.top_p = 0.95`. Every entry defaults `temperature` to `t10`
(1.0), `nucleus-sampling` to `p095` (top-p 0.95, top-k disabled) and
`tail-culling` to `none` (min-p 0). The card names no min-p, but its serving
sections (vLLM, SGLang) default min-p to 0, while llama.cpp's own default is
0.05. The maintainer chose to reproduce the recipe exactly and to bring the
30B in line at the same time (see the dated update in its README). Users can
move any of the three knobs back.

**Not wired:** vision (the model is text-only anyway), the 512K extension
and the binary `low_effort` switch (above).
