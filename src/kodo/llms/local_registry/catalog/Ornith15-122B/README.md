# Ornith15-122B — catalog notes

The largest member of the Ornith 1.5 family (`ornith-ai/Ornith-1.5-122B`,
MIT), quantized by bartowski (`bartowski/Ornith-1.5-122B-GGUF`) with
llama.cpp b11716. Added 2026-11-07. Like its `Ornith15-35B-A3B` sibling it
is a sparse MoE on Qwen 3.5's hybrid architecture (`qwen3_5_moe` in the base
`config.json`), only wider and deeper.

**Update 2026-11-07 (later the same day): the family now ships only the ten
unsloth quants.** It was first added with eight bartowski quants
(`bartowski-ornith15-122b-*`, commit `55fb0c4`), and the unsloth ones joined
them (`9915d16`). Then it turned out that bartowski's GGUFs embed ornith-ai's
outdated chat template and unsloth's the revised one (see the update under
Thinking). kodo can't override an entry's template, so the maintainer
dropped the bartowski entries. They were never pushed, so no user had
downloads or profiles keyed to those names, and the startup purge has nothing
to delete. The sections below keep the original bartowski-based notes. Each
says, where it matters, what holds for the unsloth entries.

## Architecture (verified from the GGUF headers)

All eight shipped quants were read directly (2026-11-07):

- `general.architecture = qwen35moe` on every quant, so the family takes
  `context-qwen35moe`, the same knob as `Ornith15-35B-A3B`,
  `Qwen36-35B-A3B` and `Qwen38-35B-A3B-Distill`. The card's long-context
  recipe (YaRN factor 4.0 for about 1M tokens) matches that knob's 512K/1M
  options.
- `qwen35moe.context_length = 262144`, so entries launch at the full 256K.
  That is within the 256K default-launch ceiling, so no capping knob is
  needed.
- 49 blocks: 48 main layers, with every 4th one full attention
  (`full_attention_interval = 4`) and the rest Gated DeltaNet, plus one MTP
  layer. 256 experts, 8 routed per token, plus a shared expert;
  `embedding_length = 3072`.
- `qwen35moe.nextn_predict_layers = 1` on **every** quant, with no
  `nextn_shared_target_tensors`, and the tensor list includes
  `token_embd.weight` and `output.weight`. So every entry sets
  `mtp_supported: true` and lists `spec-decoding-mtp`. bartowski's card
  says the MTP layer is stored at Q4_0 in every quant except Q8_0, for draft
  speed. The repo has no standalone MTP heads, so there is no
  `mtp_sidecars.json`.
- KV cache: only the 12 full-attention layers (13 with the MTP layer while
  drafting) hold KV, 2 KV heads × (256 + 256). At 128K that is about 1.9 GB
  in q8_0 and 3.5 GB in f16; the Gated DeltaNet state is about 150 MB. With
  about 2 GB of compute buffers, each total is file + ~4 GB (BF16, with
  f16 KV: + ~5.5 GB). These are header estimates, not `kodo-memcheck`
  measurements. Note that `Ornith15-35B-A3B`'s totals were sized
  differently and are known to be overstated (~17 GB KV quoted for the
  same attention shape); they were not touched here.
- **Vision is not wired.** The repo ships
  `mmproj-Ornith-1.5-122B-{bf16,f16}.gguf` (0.91 GB each). `LocalLLMEntry`
  has no projector field, so every entry is text-only and the projector is
  never downloaded.

## Thinking

The embedded chat template (byte-identical across all eight quants) opens
every generation with `<think>\n` unless `enable_thinking` is explicitly
`false`, so it thinks by default. The family joins
`QWEN_REASONING_BUDGET_FAMILY` with no `enable_thinking` kwarg. Its budgets
copy `Ornith15-35B-A3B`'s Ornith ladder (256..6144), the same model line and
RL recipe.

The template differs from the 35B-A3B's in two lines only: it has no
`preserve_thinking` switch (past reasoning is kept only after the last user
turn), and it serializes tool-call arguments with `tojson` for objects and
arrays and `string` otherwise, where the 35B-A3B's does it the other way
round. Neither changes which parser llama.cpp picks.

**Update 2026-11-07 (same day):** the two-line difference is not a
bartowski choice. It is a stale copy of ornith-ai's own template. ornith-ai
revised `chat_template.jinja` on 2026-10-16 (commits `7b0a4adc` and
`38c106ad`), and the card says the template was adjusted "to ensure
consistency between training and inference". The revised template renders
**every** past assistant turn with its `<think>…</think>` block, not just
the turns after the last user message. `tokenizer_config.json` still carries
the earlier template, and bartowski's GGUFs embed that one: it is
byte-identical to `chat_template.jinja` at `6f3399b7`. The unsloth quants
added below embed the revised template, byte-identical to the current
`chat_template.jinja`. kodo can't swap an entry's template, so the bartowski
entries keep the older behavior. Prefer the unsloth builds when multi-turn
reasoning matters. Both templates select the same Qwen3-Coder parser.
That preference became the decision: the bartowski entries were removed
(see the update at the top).

## llama.cpp pin: 11063

**Update 2026-11-07:** the analysis below was done on bartowski's build, and
it carries over to the unsloth entries. Points 1-4 depend on the
architecture, the parser path and the tensor types, not on who quantized.
The unsloth template reaches the same Qwen3-Coder parser, and its tensor
types are all standard (see "Unsloth quants").

bartowski built the quants at b11716, but nothing newer than b11063 is
needed to run them correctly:

1. **Architecture.** `qwen35moe` has loaded since b7990 (#19468) and its MTP
   graph since b9180 (#22673; post-norm fix #24025, b9495; nextn hparam load
   order #28159, b10741). The 122B adds no new tensor kind. bartowski's
   per-tensor layouts (see below) only mix standard ggml types.
2. **Chat parser.** The template contains `<tool_call>`, `<function=` and
   `<parameter=` and none of the earlier detectors' markers, so
   `common_chat_template_detect_params_init` gives it the dedicated
   Qwen3-Coder parser, the same path as `Qwen38-35B-A3B-Distill`. The same
   three fixes set the pin: b10936 (#28742, complex tool arguments), b10996
   (#28869, budget cutoff forces `\n</think>`) and b11063 (#29161, invalid
   UTF-8 no longer 500s the parse). The Distill families checked later
   changes up to b11559. From b11559 to b11716 the only parser change is
   #30389 (b11607), a refactor of the output to parts. The server changes
   in that range concern slot saving and SWA, neither of which kodo uses
   with this model.
3. **MTP.** Built-in layers only, so no `-md` head floor applies. #30017
   (b11443) and #30097 (b11477) are nextn refactors.
4. **Open issues** (searched 2026-11-07). The only report naming this model
   is #31262, a SYCL-only prompt-processing slowdown with `-ncmoe`, not a
   correctness bug. #29092 (HIP/ROCm Gated DeltaNet state carried across
   requests on a reused slot) is open and affects every
   `qwen35`/`qwen35moe` family alike.

## Quant ladder (maintainer decision, 2026-11-07)

BF16 (249 GB), Q8_0 (133 GB), Q6_K (109 GB), Q5_K_M (94.9 GB), Q4_K_M
(78.9 GB), then a low-RAM tail of IQ3_M (67.6 GB), Q3_K_M (60.6 GB) and Q2_K
(49.0 GB). The top five are the standard Ornith rungs. The 35B-A3B's tail
(Q3_K_XL/IQ3_M/Q2_K_L) can't be copied, because this repo has no
`Q3_K_XL` or `Q2_K_L`. bartowski now builds per-tensor layouts, in which an
`_L` suffix means "the large size of the base type" rather than "embedding
and output kept at Q8_0", and `_XL` is gone. Here IQ3_M is *larger* than
Q3_K_L (64.2 GB), so the guide's small-model tail (Q3_K_L/IQ3_M/Q2_K) would
have put two rungs 3.5 GB apart, out of order. IQ3_M/Q3_K_M/Q2_K keeps the
35B's IQ3_M middle rung and steps evenly (~72/65/53 GB totals), and Q2_K
brings the model within reach of a 64 GB machine. Q4_K_M is the rung
bartowski recommends. IQ2_M (46.4 GB) and smaller were left out.

**Update 2026-11-07:** this ladder no longer ships; the bartowski entries
were dropped (see the update at the top). The shipped ladder is unsloth's
full set of ten, listed under "Unsloth quants". It runs from BF16 down to
UD-IQ1_M (40.4 GB), with UD-Q4_K_XL (79.7 GB) as the general-purpose rung.

## Sampling (maintainer decision, 2026-11-07)

ornith-ai's card gives two recipes: "For general tasks: temperature=1.0,
top_p=0.95, top_k=20, min_p=0.0, presence_penalty=1.5, repetition_penalty=1.0"
and "For precise coding tasks: temperature=0.6, top_p=0.95, top_k=20,
min_p=0.0, presence_penalty=0.0, repetition_penalty=1.0". The GGUF headers
carry the coding one (`general.sampling.temp = 0.6`, `top_p = 0.95`,
`top_k = 20`). The maintainer chose to ship the coding recipe, the same as
`Ling-3.0-Flash`: `{"tail-culling": "none", "temperature": "t06",
"nucleus-sampling": "k20-p095"}`, plus `kv-cache: f16` on BF16.
`tail-culling: none` sets min-p to 0, as the card asks; llama.cpp would
otherwise apply its own 0.05. Presence and repetition penalties are already
at llama.cpp's neutral defaults. `Ornith15-35B-A3B` ships no sampling
defaults (its headers say temp 1.0), so the two Ornith 1.5 MoE sizes
sample differently out of the box. The unsloth headers carry the same
`general.sampling.*` values, so the unsloth entries ship the same defaults.

## Unsloth quants (added 2026-11-07)

The family also ships all ten quants of `unsloth/Ornith-1.5-122B-GGUF`, as
the maintainer chose: BF16 (249 GB), Q8_0 (133 GB), UD-Q6_K_XL (114 GB),
UD-Q5_K_XL (94.6 GB), UD-Q4_K_XL (79.7 GB), UD-Q3_K_XL (59.8 GB),
UD-IQ3_XXS (49.5 GB), UD-Q2_K_XL (44.5 GB), UD-IQ2_XXS (42.0 GB) and
UD-IQ1_M (40.4 GB). They sit beside the bartowski ladder, and the
descriptions (`… by Unsloth`) tell them apart. BF16 and Q8_0 overlap
bartowski's in size and precision. The UD-Q2_K_XL and smaller builds reach
below bartowski's smallest (Q2_K, 49.0 GB), down to a 48 GB machine.

- **Headers.** All ten were read on 2026-11-07, and they agree with
  bartowski's on every key above: `qwen35moe`, 262144 context,
  `nextn_predict_layers = 1` with no shared-target key, and the same
  `general.sampling.*` values. So the entries carry the same knobs,
  `mtp_supported: true`, sampling defaults (`kv-cache: f16` on BF16) and the
  11063 pin. Their template is the revised one (see the update under
  Thinking). It contains `<tool_call>`/`<function=`/`<parameter=` and none of
  the earlier detectors' markers, so it gets the same Qwen3-Coder parser.
- **Tensor types.** Only standard ggml types (F32, BF16, Q8_0, the K-quants
  and the IQ1-IQ4 i-quants), all far older than the pin.
- **Split layout.** unsloth's split quants put metadata only in the first
  shard (`gguf.tensor_count = 0`, `split.tensors.count = 899`) and the
  tensors in the rest. llama.cpp loads that layout normally. Point
  `gguf_header.py --tensors` at shard 2 to see the tensor list; its
  self-containment verdict only means something for MTP heads.
- **MTP.** The repo has no standalone heads; the built-in layer serves.
  Unsloth's card doesn't say at what precision the MTP layer is stored.
- **Memory.** Sized with the same header math, file + ~4 GB (BF16
  + ~5.5 GB). Not measured.
- **Build.** Unsloth's card names no llama.cpp build. The repo was uploaded
  2026-10-20.

## Not verified

Running the model. The catalog checks, header reads and HEAD requests (all
24 shard files return 200) prove that the files exist and are described
correctly, not that the model behaves well in kodo's agent loop, nor the
memory totals. The unsloth files return 200 too (34 shard files). Since
the bartowski entries were dropped, the unsloth files are the only ones
shipped.
