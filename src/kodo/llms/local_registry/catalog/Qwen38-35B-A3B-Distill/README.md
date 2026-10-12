# Qwen38-35B-A3B-Distill — catalog notes

empero-ai's distillation of the **Qwen3.8** models (teachers: Qwen3.8 2.4T
A95B and Qwen3.8 Flash Next) into the **Qwen3.6-35B-A3B** sparse-MoE
architecture. It is an SFT of `Qwen/Qwen3.6-35B-A3B` on curated teacher
traces (`empero-ai/Qwen3.8-35B-A3B-Distill`), and empero-ai quantized it
themselves (`empero-ai/Qwen3.8-35B-A3B-Distill-GGUF`). The repo's model name
is just "Qwen3.8-35B-A3B", and its GGUF headers say `general.name = Ours`.
It is the MoE sibling of `Qwen38-9B-Distill`, by the same team.

## Why a family of its own

As with `Qwen38-9B-Distill`, the maintainer chose to keep this model apart
from the Alibaba Qwen families (`Qwen36-35B-A3B`, `Qwen38-27B`). It is not an
Alibaba Qwen 3.8 model. It is a third-party Qwen 3.6 35B-A3B whose weights
were trained on Qwen 3.8 outputs. Three choices keep that visible in the UI:

- the slug `Qwen38-35B-A3B-Distill` keeps "Distill" in the group header;
- every `description` reads `Qwen 3.8 35B-A3B Distill <QUANT> by empero-ai`;
- `llm_author` is `empero-ai`, the fine-tuning team, not Alibaba Cloud.
  `quant_author` is `empero-ai` too, since they published the GGUFs.

The license is Apache 2.0, inherited from the Qwen base per empero-ai's card.
Neither empero-ai repo has a LICENSE file, so `license_url` points at
`Qwen/Qwen3.6-35B-A3B`'s, like the `Qwen36-35B-A3B` entries.

## Architecture (verified from the GGUF headers)

All ten quants were read directly (2026-10-11):

- `general.architecture = qwen35moe` on every quant, so the family takes
  `context-qwen35moe`, as `Qwen36-35B-A3B` and `Ornith15-35B-A3B` do. 41
  blocks: 40 main layers plus one MTP layer, with every 4th main layer full
  attention (`full_attention_interval = 4`) and the rest Gated DeltaNet. 256
  experts, 8 routed per token, plus a shared expert.
- `qwen35moe.context_length = 262144`.
- `qwen35moe.nextn_predict_layers = 1` on **every** quant, with no
  `nextn_shared_target_tensors`. So every entry sets `mtp_supported: true`
  and lists `spec-decoding-mtp`. Neither card mentions MTP; the SFT kept the
  base's MTP layer (`mtp_num_hidden_layers: 1` in the base `config.json`)
  through conversion. Whether its acceptance rate survived the fine-tune was
  not measured. The repo has no standalone MTP heads, so there is no
  `mtp_sidecars.json`.
- KV cache: only the 10 full-attention layers (plus the MTP layer when
  drafting) hold KV, 2 KV heads × (256 + 256). At 128K that is about 1.6 GB
  in q8_0 and 3.0 GB in f16; the Gated DeltaNet state is tens of MB. So each
  total is roughly file + 3.5 GB (BF16: + 5 GB). That is well below the
  totals the `Ornith15-35B-A3B` and `Qwen36-35B-A3B` entries quote for the
  same attention shape; those were not re-derived here.
- **Vision is not wired.** The repo ships `mmproj-Qwen3.8-35B-A3B-F16.gguf`
  (0.90 GB), the base's vision tower, which empero-ai says was not touched or
  evaluated by the text-only fine-tune. `LocalLLMEntry` has no projector
  field, so every entry is text-only and the projector is never downloaded.

## Thinking

The embedded chat template (identical across all ten quants) is the stock
Qwen 3.6 template: it opens every generation with `<think>\n` unless
`enable_thinking` is explicitly `false`, so it thinks by default, it honors
`preserve_thinking`, and it emits Qwen3-Coder-style XML tool calls
(`<tool_call><function=…>`). The family joins
`QWEN_REASONING_BUDGET_FAMILY` with no `enable_thinking` kwarg. Its budgets
copy its base `Qwen36-35B-A3B`'s mid-size MoE ladder (512..16384), not the
9B Distill's small-model ladder: empero-ai's card says the student "was
trained on 8,192-token examples and produces noticeably shorter outputs than
the base", and the default `high` tier is exactly 8192. The card also warns
of "occasional over-long deliberation on easy questions", inherited from the
teachers.

## llama.cpp pin: 11063

empero-ai's card names no build, only "a recent llama.cpp build with Qwen3.6
/ Gated DeltaNet MoE support". `qwen35moe` loads since b7990 (#19468), and
its MTP graph since b9180 (#22673; post-norm fix #24025, b9495; nextn hparam
load order #28159, b10741). The template selects llama.cpp's dedicated
Qwen3-Coder parser (`common/chat.cpp` detection: `<tool_call>` +
`<function=`), the same path as `Qwen38-9B-Distill`, so the same three fixes
set the pin:

- b10936, #28742: parsing of complex (array/object) tool arguments.
- b10996, #28869: a reasoning-budget cutoff now forces `\n</think>`.
- b11063, #29161: invalid UTF-8 byte pieces no longer 500 the PEG parse.

Later changes up to b11559 were checked for the 9B Distill and still apply.
#30257 (b11548, MTP with multimodal input) only matters with the projector,
which isn't wired. Open upstream issues about this model were not searched.

## Quant ladder

empero-ai publishes ten quants, and the maintainer chose to ship **all ten**:
BF16, Q8_0, Q6_K, Q5_K_M, Q4_K_M, IQ4_XS, Q3_K_M, IQ3_M, Q2_K and IQ2_M. That
is the standard five rungs plus a five-rung low-RAM tail, down to IQ2_M
(12.6 GB, about 16 GB total), which empero-ai calls the "smallest usable".
empero-ai has no `_L`/`_XL` builds, so the tail uses plain K-quants and
i-quants. Per empero-ai, the i-quants and the 2/3-bit K-quants are
imatrix-calibrated. Q4_K_M is the rung empero-ai recommends.

## Sampling

empero-ai's card: "**Sampling**: `temperature=0.6, top_p=0.95, top_k=20`.
Greedy decoding on long generations is a known repetition-loop failure mode
for reasoning models in this class." The GGUF headers disagree on one value:
they carry `general.sampling.temp = 1.0`, `top_p = 0.95`, `top_k = 20`, most
likely inherited from the Qwen 3.6 base's metadata. The maintainer chose the
card's recipe, the same as `Qwen38-9B-Distill`: `{"tail-culling": "none",
"temperature": "t06", "nucleus-sampling": "k20-p095"}`, plus `kv-cache: f16`
on BF16. `tail-culling: none` sets min-p to 0, which the card's transformers
setup assumes. llama.cpp would otherwise apply its own min-p of 0.05.

## Not verified

Running the model. The catalog checks, header reads and HEAD requests prove
that the files exist and are described correctly, not that the model behaves
well in kodo's agent loop.
