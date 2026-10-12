# Qwen38-9B-Distill — catalog notes

empero-ai's full-parameter distillation of **Qwen3.8 2.4T A95B** into the
**Qwen3.5-9B** architecture: an SFT of `Qwen/Qwen3.5-9B` on ~70,000 teacher
traces (`empero-ai/Qwen3.8-9B-Distill`), quantized by empero-ai themselves
(`empero-ai/Qwen3.8-9B-Distill-GGUF`). The repo's model name is just
"Qwen3.8-9B", and its GGUF headers say `general.name = Qwen3.8 9B`.

## Why a family of its own

The maintainer chose to keep this model apart from `Qwen38-27B`, and not list
it there as a smaller size. It is not an Alibaba Qwen 3.8 model. It is a
third-party Qwen 3.5 9B whose weights were trained on Qwen 3.8 outputs, and
it has a different author, architecture size and template than the 27B.
Three choices keep that visible in the UI:

- the slug `Qwen38-9B-Distill` keeps "Distill" in the group header;
- every `description` reads `Qwen 3.8 9B Distill <QUANT> by empero-ai`;
- `llm_author` is `empero-ai`, the fine-tuning team, not Alibaba Cloud.
  `quant_author` is `empero-ai` too, since they published the GGUFs.

The license is Apache 2.0, inherited from the Qwen base per empero-ai's card.
Neither empero-ai repo has a LICENSE file, so `license_url` points at
`Qwen/Qwen3.5-9B`'s, like the `Qwen35-9B` entries.

## Architecture (verified from the GGUF headers)

All five quants were read directly (2026-10-11):

- `general.architecture = qwen35`, which is dense, so it takes
  `context-qwen35`, as `Qwen35-9B` and `Ornith15-9B` do. 33 blocks: 32 main
  layers plus one MTP layer, with every 4th main layer full attention
  (`full_attention_interval = 4`) and the rest Gated DeltaNet.
- `qwen35.context_length = 262144`.
- `qwen35.nextn_predict_layers = 1` on **every** quant, with no
  `nextn_shared_target_tensors`. The Q4_K_M tensor list has the
  `blk.32.nextn.*` tensors plus its own `token_embd`/`output`. So every entry
  sets `mtp_supported: true` and lists `spec-decoding-mtp`. The card never
  mentions MTP; the SFT kept the base's MTP layer through conversion. Whether
  its acceptance rate survived the fine-tune was not measured. The repo has no
  standalone MTP heads, so there is no `mtp_sidecars.json`.
- KV cache: only the 8 full-attention layers (plus the MTP layer when
  drafting) hold KV, 4 KV heads × (256 + 256). At 128K that is about 2.3 GB
  in q8_0 and 4.3 GB in f16, which is why each total is roughly
  file + 4 GB (BF16: + 6 GB).
- The upstream base is a vision-language model, but empero-ai's fine-tune is
  text-only and the GGUF repo ships no `mmproj-*` file. Nothing to wire.

## Thinking

The embedded chat template is **byte-identical** to `Ornith15-9B`'s, the
stock Qwen3.5 template. It opens every generation with `<think>\n` unless
`enable_thinking` is explicitly `false`, so it thinks by default, and it
emits Qwen3-Coder-style XML tool calls (`<tool_call><function=…>`). The
family joins `QWEN_REASONING_BUDGET_FAMILY` with no `enable_thinking` kwarg.
Its budgets copy the small-model ladder of the other 9B families
(2048..32768). empero-ai's card warns that the student "inherits the
teacher's reasoning style, including occasional over-long deliberation on
easy questions", which is the verbosity that ladder already assumes.

## llama.cpp pin: 11063

empero-ai's card names no build. qwen35 loads since b7990 (#19468), and its
MTP graph since b9180 (#22673; post-norm fix #24025, b9495). The template
selects llama.cpp's dedicated Qwen3-Coder parser (`common/chat.cpp`
detection: `<tool_call>` + `<function=`), and these fixes to that path matter
for an agent:

- b10936, #28742: parsing of complex (array/object) tool arguments in the
  qwen3-coder parser. kodo's tools take array arguments.
- b10996, #28869: a reasoning-budget cutoff now forces `\n</think>`. kodo
  launches this family with `--reasoning-budget`.
- b11063, #29161: invalid UTF-8 byte pieces no longer 500 the PEG parse. The
  same fix sets the `Ling-3.0-Flash` and Granite 4.2 pins.

So the pin is 11063, the same as Ling and Granite. Later chat-parser changes
up to b11559 were checked too. #29942 (b11393, a TOOL_ID use-after-free)
doesn't apply because the qwen3-coder format has no tool ids. The others are
refactors or other models' parsers.

## Quant ladder

empero-ai publishes exactly five quants: BF16, Q8_0, Q6_K, Q5_K_M and Q4_K_M.
All five ship, which is the standard five-rung ladder. There is no low-RAM
tail because no Q3/Q2 build of this model exists. The only other GGUFs on
the Hub are of derived merges and abliterations, not this model. Q4_K_M is
the rung empero-ai recommends.

## Sampling

empero-ai's card: "**Sampling**: `temperature=0.6, top_p=0.95, top_k=20`.
Greedy decoding on long generations is a known repetition-loop failure mode
for reasoning models in this class." The GGUF headers carry no
`general.sampling.*` keys. The maintainer chose to ship the card's recipe
exactly, as `Ling-3.0-Flash` does: `{"tail-culling": "none", "temperature":
"t06", "nucleus-sampling": "k20-p095"}`, plus `kv-cache: f16` on BF16.
`tail-culling: none` sets min-p to 0, which the card's transformers setup
assumes. llama.cpp would otherwise apply its own min-p of 0.05.

## Not verified

Running the model. The catalog checks, header reads and HEAD requests prove
that the files exist and are described correctly, not that the model behaves
well in kodo's agent loop.
