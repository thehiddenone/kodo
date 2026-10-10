---
name: add-local-llm
description: Add a new built-in (shipped) local LLM family, or more quants of an existing one, to kodo's local registry catalog (src/kodo/llms/local_registry/catalog/) from a Hugging Face model card and GGUF quant repo, covering header verification, the llama.cpp version pin, knobs, MTP, thinking tiers, docs, tests and memory. Use this whenever the user, working in the kodo / py-kodo repo, asks to add, register, ship or catalog a local model, GGUF, quant or "built-in LLM", or pastes huggingface.co links to a model and its GGUF repo, even if they don't say "registry" or "catalog". Not for importing into a user's own ~/.kodo/local_llms/ catalog at runtime; that is the Model Importer agent's job.
---

# Add a built-in local LLM to kodo's registry

The output is one JSON file per quant under
`src/kodo/llms/local_registry/catalog/<base_llm>/`, a `README.md` for the
family, any thinking-tier wiring, doc updates, and a passing test suite.
Leave the work uncommitted unless the user asks for a commit.

## 0. The code is the spec, not this skill

kodo changes fast. Every file, symbol and rule named below was true when this
skill was written, but **re-derive each one from the code before relying
on it**. When something named here is gone or behaves differently:

1. Find its successor with `git log -S '<symbol>' --oneline` or a grep, and
   follow the code as it is now.
2. Keep going with the current behavior. Don't bend the code to match this
   skill.
3. At the end, update this skill with what changed (§9) so the next run
   starts from the truth.

Two habits that keep this honest:
- Read the **2-3 most recently added families** as working exemplars:
  `git log --diff-filter=A --name-only --format='%h %ad' -- src/kodo/llms/local_registry/catalog | head -60`.
  Their JSON and README show the current conventions better than any prose
  here.
- Load the project memory files. `json-local-llm-catalog`,
  `mtp-support-generalized`, `mtp-sidecar-heads`, `ornith-15-catalog-entries`,
  `muse-glimmer-thinking-tiers`, `local-model-purge`, `model-importer-agent`
  and the most recent `*-catalog` memory hold decisions and traps the diff
  alone won't show.

## 1. Re-read the current contract

Skim these before collecting any facts, because they decide which facts matter:

| What | Where (as of writing) | Look for |
|---|---|---|
| File format | `local_registry/_catalog_files.py` | `_STR_FIELDS`/`_INT_FIELDS`/`_BOOL_FIELDS`/`_STR_MAP_FIELDS`/`_REQUIRED`, path-derived keys (`name` = file stem, `base_llm` = dir, both rejected inside the file), `validate_catalog_entry`, `catalog_sort_key` |
| Knob ids an entry may list | `_knobs_table.py` `KNOBS_BY_ID`; shared order in `_knobs_shared.py` `SHARED_KNOBS` | shared ids first, then private ones |
| Context-extension knobs | `context_knob_architectures()` | which `general.architecture` each knob targets |
| MTP | `offers_builtin_mtp`, `_knobs_mtp.py`, `_mtp_sidecars.py` (`mtp_sidecars.json`, `MTP_HEAD_MIN_LLAMACPP_VERSION`) | built-in layers vs standalone heads |
| Import-time invariants | `_catalog.py` `_validate_catalog` | thinking slugs must exist, MTP/knob agreement, … |
| Thinking families | `_thinking.py`; per-request field names in `llamacpp/_llama.py` `_TIER_TEMPLATE_KWARG` and `_build_thinking_extra_body` | family frozensets, `QWEN_TIER_TOKEN_BUDGETS` |
| Rationale | `doc/LLM_REGISTRY.md` §4, §4.0, §4.5, §4.6; `doc/LOCAL_INFERENCE.md` §2a | field meanings, tip conventions |
| Quant-picking heuristics | `src/kodo/agents/agent_kodo_model_importer.md` | the importer's current tier/quant preferences |

## 2. Collect the facts: from files, not from cards

Model cards and repo names lie by omission. An MTP-capable GGUF may not
mention MTP, an `-MTP-GGUF` repo may not carry the layers, and two sizes of
one family can use different architectures. Read the GGUF itself. The
scripts in `scripts/` use only the standard library, so they keep working
when kodo's internals change. Run them with plain `python3`:

```bash
S=.claude/skills/add-local-llm/scripts
python3 $S/hf_quants.py <gguf_repo>              # card/license/base model + every quant, shard-summed decimal GB, first-shard filename
python3 $S/gguf_header.py <gguf_repo> <file> --template /tmp/<model>.jinja   # full header + full chat template
python3 $S/llamacpp_first_release.py <PR#|sha> ...                           # first bNNNNN containing each fix
```

Also read the base model's `README.md` and `config.json` (`curl -sL
https://huggingface.co/<repo>/raw/main/<file>`), plus the quant repo's
README, which usually names the llama.cpp build used and any MTP or vision
notes.

Record, for **every quant you will ship**, not just one:
- `general.architecture`, `<arch>.context_length`
- `<arch>.nextn_predict_layers` and `<arch>.nextn_shared_target_tensors`
- `expert_count` / `expert_used_count` (dense vs MoE drives the tip wording and `cpu-moe`)
- per-layer arrays such as `attention.head_count_kv`, plus key/value lengths, `kv_lora_rank` and sliding-window keys (these drive the KV-cache estimate)
- the chat template: how thinking is switched on and off and its default, its thinking tags, and the tool-call format
- the license (from the **base** model), the original author, the quantizer, and the llama.cpp build the quants were made with
- `mmproj-*` files. The catalog can't declare a projector, so vision models ship text-only. Say so in the README.

## 3. Ask the user what is genuinely theirs to decide

Use AskUserQuestion. Don't guess, and put the recommended option first. These
are almost always open:
- **Quant ladder.** Which rungs ship. Offer the precedent of the closest
  existing family by the same quantizer, and a consumer-focused alternative.
  Give sizes in the option text.
- **Family slug (`base_llm`).** It is the user-visible group header in
  kodo-vsix and the key for the thinking tables. Offer the naming styles
  already in the catalog.
- Anything else the facts leave ambiguous: a model with no clear thinking
  mechanism, sampling defaults that differ from house precedent, or a
  multi-repo choice.

## 4. Derive each field

Explain non-obvious choices in the family README, not only in the
conversation.

- **File name / `name`**: `<quantizer>-<slug-ish>-<quant>`, following the
  closest family's casing and separators. Renaming a shipped `name` later
  orphans users' downloads and settings, and the startup purge deletes them,
  so get it right now.
- **`filename`**: the first shard for split GGUFs. **`size_hint`**: the
  decimal-GB shard sum, to 1 decimal (`"77.8 GB"`).
- **`context_window`**: the header's `<arch>.context_length`.
- **Knobs**: every id in `SHARED_KNOBS`, in its order. At writing time that
  included `cpu-moe` even for dense models; check the exemplars. Add a
  context knob only if `context_knob_architectures()` maps one to
  **this exact architecture**. A mismatched `--override-kv` is silently
  ignored. Add `spec-decoding-mtp` and `mtp_supported: true` only if
  `nextn_predict_layers > 0` and the layers are not shared-target, checked
  per quant. Standalone `MTP/` heads go in `mtp_sidecars.json` instead
  (see §4.0a and the Qwen38 families).
- **`knob_defaults`**: follow the current precedent. Grep it with
  `grep -rh -A3 '"knob_defaults"' catalog | sort | uniq -c`. At writing time
  that meant `{"kv-cache": "f16"}` for ≥16-bit builds and `{}` otherwise,
  with no sampling defaults even when the card recommends some. Mention the
  card's recommendation to the user instead of silently diverging.
- **`llamacpp_version`**: the oldest build that runs the model **correctly
  for an agent**, which is often newer than the quantizer's build. Check:
  - when the architecture landed: `src/models/<arch>.cpp` history
  - whether llama.cpp parses this chat template's thinking tags and tool
    calls: `common/chat.cpp` detection plus `common/parsers/<family>.cpp`,
    and fixes to them
  - whether MTP drafting works for the architecture
  - open issues about the model

  Find the build with `llamacpp_first_release.py`. If a fix PR was closed
  unmerged, find what superseded it in the PR's comments.
  (Example: Ling 3.0 quants were made at b10472, but tool calls before
  `</think>` were lost until b11057, and invalid-UTF-8 500s were fixed in b11063.)
- **Memory**: total ≈ weights + KV cache at 128K + ~1-2 GB compute. Compute
  the KV from the header: per layer that has KV,
  `n_head_kv × (key_len + value_len) × bytes × ctx`. Use the MLA latent
  (`kv_lora_rank + rope dim`) when present, and count recurrent or linear
  layers (`head_count_kv = 0`) as ~0. The default cache type is whatever
  `kv-cache` defaults to now. `min_memory` is the smallest sensible combined
  VRAM+RAM total and `memory` the comfortable one, in GB. kodo-vsix compares
  them with VRAM+RAM sums, so keep the tips consistent with them.
- **`gpu_tip` / `mac_tip`**: copy the voice of the exemplars. `gpu_tip` pairs a
  modest 8-16 GB GPU with a DDR5 kit and names the offloading style (expert
  offload for MoE, layer offload for dense). `mac_tip` maps the total onto
  MacBook Pro tiers, or says plainly when nothing short of a Mac Studio fits.
- **`description`**: `"<Display Name> <QUANT> by <quantizer>"`.
- **`license_*`, `llm_author`, `quant_author`**: from the base model's page.
  `quant_author` is the repo owner as written.

Generate the files with a short throwaway script (in the scratchpad) rather
than by hand, so the 5-20 near-identical files can't drift. Match the
existing formatting: `json.dumps(indent=2, ensure_ascii=False)` plus a
trailing newline.

## 5. Thinking tiers

Decide from the **template and llama.cpp's parser**, not the card:
- The template thinks by default with `<think>`-style tags that llama.cpp's
  parser declares (so `--reasoning-budget` can see them). Join the token-budget
  family: add the slug to its frozenset and a budget row to
  `QWEN_TIER_TOKEN_BUDGETS`, copying the closest family's scale. Off by
  default needs an `enable_thinking` special case. Check how `_llama.py`
  handles `Qwen35-9B` today.
- The template takes a graded effort or strength kwarg. Use the family with
  the **same field name and the same tier vocabulary**, or add a new family:
  frozenset, tiers, default, `local_thinking_*` branches, the
  `_TIER_TEMPLATE_KWARG` row (a missing row raises on every request, and a
  wrong name is silently ignored), the `_validate_catalog` union, the
  test-family tables, and kodo-vsix's `THINKING_FAMILIES`
  (`src/llm-registry-types.ts`) plus its description table
  (`src/webview/ModeControls.tsx`).
- No usable mechanism: no family, and say so in the README.

## 6. README and docs

- `catalog/<base_llm>/README.md`: architecture facts, how they were verified,
  the context, MTP, thinking and version-pin reasoning, the ladder, anything
  deliberately not wired (vision, draft models), and sampling notes. Dated
  "Update YYYY-MM-DD" notes append and never rewrite history.
- `grep -rn '<an existing slug>' doc README.md` lists every place families
  are enumerated (the §4.5 family list, the §4.6 `mtp_supported` list, the
  §4.0a heads list). Add the new slug there and fix any staleness you notice.

## 7. Verify

```bash
hatch run python .claude/skills/add-local-llm/scripts/verify_family.py <base_llm>
for each entry: curl -s -o /dev/null -w '%{http_code}' -I -L https://huggingface.co/<repo>/resolve/main/<filename>   # expect 200
hatch run pytest -q -p no:cacheprovider && hatch run ruff check src test && hatch run mypy
```

Use `hatch run …`. A bare `pytest` picks up a global install without the
project's dependencies. `hatch run python -c` mangles `{…}`, so put probes in
a file. If a test fails because it enumerates families or counts entries,
check whether it should read the registry instead (see CLAUDE.md, "read it off
the registry") before editing expectations.

## 8. Report and remember

- Save or refresh a project memory, `<family>-catalog`: the user's decisions,
  the version-pin reasoning, and any trap found. Add a one-line pointer to
  `MEMORY.md`.
- Tell the user what shipped, the non-obvious calls (the pin, MTP, thinking,
  memory), what was not verified (usually: actually running the model), and
  any open choice. Don't commit unless asked.

## 9. Keep this skill current

If any step above was wrong for the current code (a moved symbol, a new
required field, a new knob, a changed convention), edit this skill before
finishing. The repo copy at `.claude/skills/add-local-llm/` is canonical,
and `~/.claude/skills/add-local-llm` is a symlink to it. Fix the bundled
scripts the same way when they break.

## Traps seen so far

- An architecture mismatch for the context knob fails silently: dense vs MoE
  sizes of one family differ (`qwen35` vs `qwen35moe`).
- MTP support varies **within one family** across quant repos. Check every
  repo you use.
- `_L`/`_XL` quants are not smaller at small sizes, because the embeddings
  dominate. Never copy a quant name across sizes without checking bytes.
- kodo's own header reader cuts strings at 2000 chars. Use `gguf_header.py`
  for templates.
- Template kwarg names differ between families (`reasoning_effort` vs
  `reasoning_strength`), and llama.cpp ignores unknown names silently.
- `_thinking.py` slugs must match a shipped `base_llm`, or kodo refuses to
  start, so renaming a family directory means editing `_thinking.py` too.
- `mmproj-*`, imatrix files and standalone heads are never quants.
