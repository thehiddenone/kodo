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

Two things in the repository are deliberately **not** wired up:

- `mmproj-BF16.gguf`/`mmproj-F16.gguf`. The base model is multimodal
  (`image-text-to-text`), but `LocalLLMEntry` has no mmproj
  field and mmproj companions are not reachable from the UI at all — see
  doc/LLM_REGISTRY.md §4.3. These entries are text-only, exactly like every
  other hardcoded entry.
- The `MTP/` NextN speculative-decoding heads. llama.cpp's draft-head
  support for this architecture is still an open PR (ggml-org/llama.cpp#27836
  / #27842 as of 2026-09-13) and kodo launches no `--spec-type` flag. This
  is a support gap in llama.cpp, not in the GGUF's own data, so every entry
  here keeps `mtp_supported` at its default
  `False` until that upstream work lands — see `_knobs_mtp.py`.

`llamacpp_version` is 10829 rather than 10660, the build that first loaded
the architecture (ggml-org/llama.cpp#27742, merged 2026-08-27). Three
correctness follow-ups landed after it — #27941, #28123 (recurrent-state
rollback) and #28068 (Gated DeltaNet normalization `max` -> `rsqrt`, a
real output-quality bug) — and b10829 is the first build containing all
three.

Every entry's `license_name`/`license_url` is the base model's own license,
read off `Qwen/Qwen3.8-Flash-Next`'s model card (`license_name`/`license_link`),
not the quant repo.
