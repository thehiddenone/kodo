# Qwen38-Flash-Next — catalog notes

A 125B-parameter sparse MoE (512 experts, 10 routed + 1 shared active, ~6B
activated) on top of a further 51B of n-gram embeddings, built as a *hybrid*
stack: 12 repeats of `3 x Gated DeltaNet -> 1 x Qwen Sparse Attention`, so
only 12 of its 48 layers carry a real KV cache and the other 36 hold a
constant-size recurrent state instead. That is what makes the memory story
here unusual and why the entries' hardware tips read the way they do — context
is nearly free compared with a conventional attention model of this size,
while the weights themselves are the whole cost. Every quant is a split GGUF
(3-8 shards); `filename` names the first shard, as elsewhere in the
catalog, and `LocalModelManager` pulls the rest.

`mmproj-BF16.gguf`/`mmproj-F16.gguf` are deliberately **not** wired up. The
base model is multimodal (`image-text-to-text`), but `LocalLLMEntry` has no
mmproj field and mmproj companions are not reachable from the UI at all — see
doc/LLM_REGISTRY.md §4.3. These entries are text-only, exactly like every
other hardcoded entry.

**MTP comes only from the standalone heads** in the repo's `MTP/` folder
(`mtp_sidecars.json` here; doc/LLM_REGISTRY.md §4.0a). The quants themselves
carry no MTP layers — their GGUF header has 48 blocks and no
`qwen4exp.nextn_predict_layers` key, while each head file declares 49 and
`nextn_predict_layers = 1` — so every entry keeps `mtp_supported` at `False`
and the head picker offers no *Built-in* option. Checked 2026-10-04 by
reading the headers directly:

- `mtp-Qwen3.8-Flash-Next-{BF16,Q8_0,Q4_K_M}.gguf` are **self-contained**:
  each carries its own `token_embd.weight` and `output.weight`. These are
  the three listed.
- `mtp-Qwen3.8-Flash-Next-shared-{BF16,Q8_0,Q4_K_M}.gguf` set
  `qwen4exp.nextn_shared_target_tensors = true` and borrow the embedding and
  output projection from the running model. Mainline llama.cpp has no
  cross-model tensor borrowing (only unsloth's fork does), so they are not
  listed, even though unsloth recommends `shared-Q8_0`.

The heads need llama.cpp **b11330** (`llamacpp_version` on each head, not on
the entries): ggml-org/llama.cpp#29761 (merged 2026-10-01) added the
`qwen4exp` MTP graph and fixed the speculative init loading the *target*
model's path in place of the `-md` file; b11330 is the first release that
contains it. On an older build the model simply launches without
speculative decoding. unsloth's measurements: ~1.3-1.7x single-stream with
greedy sampling, a net loss at concurrency 8, and the BF16 head is bigger
*and* slower than Q8_0 at near-identical acceptance (66.5% vs 66.1%) —
listed first anyway, because the picker orders heads by precision.

`llamacpp_version` is 10829 rather than 10660, the build that first loaded
the architecture (ggml-org/llama.cpp#27742, merged 2026-08-27). Three
correctness follow-ups landed after it — #27941, #28123 (recurrent-state
rollback) and #28068 (Gated DeltaNet normalization `max` -> `rsqrt`, a
real output-quality bug) — and b10829 is the first build containing all
three.

Every entry's `license_name`/`license_url` is the base model's own license,
read off `Qwen/Qwen3.8-Flash-Next`'s model card (`license_name`/`license_link`),
not the quant repo.
