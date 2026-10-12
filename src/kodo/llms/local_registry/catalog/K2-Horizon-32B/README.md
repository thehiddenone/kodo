# K2-Horizon-32B — catalog notes

IFM's large dense K2 Horizon model, released 2026-09
(https://huggingface.co/IFM/K2-Horizon-32B, Apache 2.0): a 32B decoder-only
model (bartowski's card says 35B for the source checkpoint) with a 512K
context, trained to that length in midtraining and then SFT'd with an
agentic/reasoning mix. Fully open: data, recipe, training code and
intermediate checkpoints are public. Quants are bartowski's
(https://huggingface.co/bartowski/K2-Horizon-32B-GGUF), made with llama.cpp
b11462 using per-model computed tensor layouts (published in the repo's
`layouts/` folder). Only BF16 is split (2 shards); `filename` names the
first.

**Architecture: `k2-horizon`**, read off the GGUF header of all eight shipped
quants (2026-10-11). 64 layers, every one full GQA attention with 64 query /
8 KV heads of dimension 128, no sliding window, no recurrent layers, no
experts (`expert_count = 0`; the MoE "MoVA" sibling is a separate model).
The KV cache is the same shape as Granite 4.2 30B's: 64 × 8 × (128 + 128)
elements per token, about 18GB at 128K with the default q8_0 cache and about
34GB at f16 (the BF16 entry's default). Every entry's total at 128K is file
size + ~20GB (BF16: + ~36GB). That is why the low-RAM tail saves so little,
and why the tips on the two smallest rungs point at the KV cache knob's
`q4_0` option.

**Context: native 524288, launched at 128K by default.** `context_window` is
the header's `k2-horizon.context_length` (524288, matching `config.json`'s
`max_position_embeddings`). But kodo's base `--ctx-size 0` makes llama.cpp
allocate the full trained length, and since llama.cpp #19070 (2026-01) an
explicit `--ctx-size 0` also stops `--fit` from shrinking the context
(`fit_params_min_ctx = UINT32_MAX` in `common/arg.cpp`). Shipped as-is, every
K2 launch would reserve a ~73GB q8_0 KV cache (~137GB at f16) on top of the
weights. So the family has its own knob, `context-k2-horizon`
(`_knobs_k2_horizon.py`, maintainer's choice, 2026-10-11): 128K (default,
`--ctx-size 131072`), 256K, and native 512K (no args). All three are within
the trained length, so no option needs rope-scaling or `--override-kv`, and
the knob does not appear in `context_knob_architectures()` (which maps only
the YaRN knobs). An explicit non-zero `--ctx-size` is honored exactly by
`--fit` (`common/fit.cpp` only shrinks when `n_ctx == 0`), so the chosen
size is the size the model runs at. The memory totals and tips are quoted at
the 128K default. `test/test_local_registry_k2_horizon_context.py` pins this
and also asserts that no shipped entry launches above 256K by default.
The Model Importer doesn't know about this knob, so a user importing another
`k2-horizon` GGUF gets the full native length.

**MTP: none.** No quant has `k2-horizon.nextn_predict_layers`, and the repo
has no standalone heads (`hf_quants.py` lists only the imatrix besides the
quants). bartowski's card says "Speculative decoding: no". All entries set
`mtp_supported: false`.

**Thinking tiers: none** (maintainer's choice, 2026-10-11). The template
takes `chat_template_kwargs.reasoning_effort` of `high` / `medium` / `low`
(default `high`; anything else raises). The generation prompt opens
`<ifm|think>`, `<ifm|think_fast>` or `<ifm|think_faster>` respectively.
The vocabulary matches GPT-OSS's, but that family defaults to `medium`. Two
facts argue for leaving the model on its template default of `high`:
- the card's Best Practices #1: "Reasoning effort: always `high`. All
  reported results use high reasoning effort. … `medium` and `low` effort
  settings are not recommended except for research on reasoning efforts";
- on current llama.cpp, medium and low are broken for an agent. The model
  often closes `<ifm|think_fast>` with the generic `</ifm|think>`, which the
  parser doesn't accept as a closer, so the answer or the whole tool call
  lands in `reasoning_content` and the reply comes back empty with
  `finish_reason: stop` (ggml-org/llama.cpp#30246, open; fix PR #30335,
  open as of 2026-10-11, measured 32 of 120 empty replies on master).

kodo therefore sends no `chat_template_kwargs` for this family, and every
request runs at `high`. Revisit once #30335 is in a release: a new
`reasoning_effort` family with default `high` would need the five-item list
in doc/ADDING_A_LOCAL_LLM.md §5, and the entries' `llamacpp_version` would
need to move to that build. The template also takes `tool_call_format`
(`json` / `xml` / `xml_typed`, default `xml`) and `tool_presentation_format`
(default `markdown`). kodo leaves both at their defaults, which is what
llama.cpp's parser expects when they are absent.

**`llamacpp_version` is 11454, older than bartowski's b11462.** llama.cpp
#29535 (first in b11454) added the `k2-horizon` architecture
(`src/models/k2-horizon.cpp`) together with a dedicated chat parser
(`common/parsers/k2-horizon.cpp`). `common/chat.cpp` routes any template
containing `<|ifm|im_start|>` and `<ifm|tool_calls>` to it, because the
template rejects assistant turns without `reasoning_content`, which defeats
the autoparser's discovery. The parser declares the effort-matched
`<ifm|think…>` start/end tags and parses the default `xml` tool-call format
(`<ifm|tool_calls><ifm|tool_call>name <ifm|arg_key>…</ifm|arg_key>
<ifm|arg_value>…</ifm|arg_value></ifm|tool_call></ifm|tool_calls>`).
Changes to that path since b11454:
- b11484, #30088: names tool and argument grammar rules by index, fixing a
  segfault when a tool is named `call` (#29967). In this parser the clash
  was with `tool-calls`. No kodo tool is named `call` or `calls`, so it
  doesn't affect kodo and isn't pinned;
- b11531, #30210: a chat API refactor, with no behavior change for this
  parser.

The quants were made at b11462, later than the pin, but the GGUF format
didn't change in between. Open upstream issues naming K2 Horizon
(searched 2026-10-11): #30246 (above, effort tiers only).

**Quant ladder** (maintainer's choice, 2026-10-11):
BF16/Q8_0/Q6_K/Q5_K_M/Q4_K_M, the five-rung bartowski precedent, plus a
low-RAM tail of IQ3_M/Q3_K_M/Q2_K. The repo has no `Q3_K_XL` or `Q2_K_L`, so
the large-model tail (Granite 4.2 30B, Ling-3.0-Flash) isn't available.
The small-model tail (Q3_K_L/IQ3_M/Q2_K) would ship two near-identical rungs:
here IQ3_M (18.68GB, 4.24 body bits per weight) is *larger* than Q3_K_L
(18.34GB, 4.00 bpw), so Q3_K_L was dropped and Q3_K_M (16.71GB) put in its
place for a real step down.

**Sampling: IFM's recommendation ships as defaults** (maintainer's choice,
2026-10-11). The card says "Sampling parameters. `temperature=1.0`,
`top_p=0.95`" (Best Practices #2) and repeats it in the API tip. The GGUF
header has no `general.sampling.*` keys. Every entry defaults `temperature`
to `t10`, `nucleus-sampling` to `p095`, and `tail-culling` to `none`
(`--min-p 0.0`). The last one matches the vLLM/SGLang serving the card
documents, where min-p defaults to 0, rather than llama.cpp's own 0.05.
This is the same set Granite 4.2 ships.

**Not wired:** reasoning-effort tiers (above), the alternative tool-call
formats (above), and the other K2 Horizon sizes (0.9B to 7B dense, the 36B
MoVA), which are separate families if ever added. The model is text-only.
