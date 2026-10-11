---
name: add-local-llm
description: Add a new built-in (shipped) local LLM family, or more quants of an existing one, to kodo's local registry catalog (src/kodo/llms/local_registry/catalog/) from a Hugging Face model card and GGUF quant repo, covering header verification, the llama.cpp version pin, knobs, MTP (built-in layers and standalone heads), thinking tiers, docs and tests. Use this whenever the user, working in the kodo / py-kodo repo, asks to add, register, ship or catalog a local model, GGUF, quant or "built-in LLM", or pastes huggingface.co links to a model and its GGUF repo, even if they don't say "registry" or "catalog". Not for importing into a user's own ~/.kodo/local_llms/ catalog at runtime; that is the Model Importer agent's job.
---

# Add a built-in local LLM to kodo's registry

The output is one JSON file per quant under
`src/kodo/llms/local_registry/catalog/<base_llm>/`, a `README.md` for the
family, any thinking-tier wiring, doc updates, and a passing test suite.
Leave the work uncommitted unless the user asks for a commit.

Everything this skill needs is in the repo. The rules, their reasons and the
traps live in **`doc/ADDING_A_LOCAL_LLM.md`** (the guide). This skill is the
procedure that applies them. Read the guide in full before step 2.

## 0. The code is the spec, not this skill or the guide

kodo changes fast. Every file, symbol and rule named here or in the guide was
true when written, but **re-derive each one from the code before relying on
it**. When something named is gone or behaves differently:

1. Find its successor with `git log -S '<symbol>' --oneline` or a grep, and
   follow the code as it is now.
2. Keep going with the current behavior. Don't bend the code to match the
   docs.
3. At the end, fix the guide and this skill (§9) so the next run starts from
   the truth.

Two habits keep this honest:
- Read the **2-3 most recently added families** as working exemplars:
  `git log --diff-filter=A --name-only --format='%h %ad' -- src/kodo/llms/local_registry/catalog | head -60`.
  Their JSON shows the current conventions better than any prose, and their
  `README.md` records the decisions and traps of that family.
- Read the history of the files you will touch for context the code alone
  won't show: `git log --oneline -- doc/ADDING_A_LOCAL_LLM.md src/kodo/llms/local_registry/_thinking.py`.

## 1. Re-read the current contract

Skim these before collecting any facts, because they decide which facts matter:

| What | Where (as of writing) | Look for |
|---|---|---|
| Procedure, rules, traps | `doc/ADDING_A_LOCAL_LLM.md` | all of it |
| File format | `local_registry/_catalog_files.py` | `_STR_FIELDS`/`_INT_FIELDS`/`_BOOL_FIELDS`/`_STR_MAP_FIELDS`/`_REQUIRED`, path-derived keys (`name` = file stem, `base_llm` = dir, both rejected inside the file), `validate_catalog_entry`, `catalog_sort_key` |
| Knob ids an entry may list | `_knobs_table.py` `KNOBS_BY_ID`; shared order in `_knobs_shared.py` `SHARED_KNOBS` | shared ids first, then private ones |
| Context-extension knobs | `context_knob_architectures()` | which `general.architecture` each knob targets |
| MTP | `offers_builtin_mtp`, `_knobs_mtp.py`, `_mtp_sidecars.py` (`mtp_sidecars.json`, `MTP_HEAD_MIN_LLAMACPP_VERSION`) | built-in layers vs standalone heads |
| Import-time invariants | `_catalog.py` `_validate_catalog` | thinking slugs must exist, MTP/knob agreement, … |
| Thinking families | `_thinking.py`; per-request field names in `llamacpp/_llama.py` `_TIER_TEMPLATE_KWARG` and `_build_thinking_extra_body` | family frozensets, `QWEN_TIER_TOKEN_BUDGETS` |
| Mechanisms and rationale | `doc/LLM_REGISTRY.md` §4, §4.0, §4.0a, §4.1b, §4.5, §4.6, §4.6a; `doc/LOCAL_INFERENCE.md` §2a | field meanings, tip conventions, the purge |
| Quant-picking heuristics | `src/kodo/agents/agent_kodo_model_importer.md` | the importer's current tier/quant preferences |

## 2. Collect the facts from files, not cards

Guide §2 lists what to record for every quant that will ship, and why the
card isn't enough. The scripts in `scripts/` use only the standard library,
so they keep working when kodo's internals change. Run them with plain
`python3`, and write outputs to the scratchpad:

```bash
S=.claude/skills/add-local-llm/scripts
python3 $S/hf_quants.py <gguf_repo>              # card/license/base model + every quant, shard-summed decimal GB, first-shard filename; MTP heads listed apart
python3 $S/gguf_header.py <gguf_repo> <file> --template <scratchpad>/<model>.jinja   # full header + full chat template
python3 $S/gguf_header.py <gguf_repo> <head-file> --tensors                          # an MTP head: header + tensor list + self-contained verdict
python3 $S/llamacpp_first_release.py <PR#|sha> ...                                   # first bNNNNN containing each fix
```

Also read the base model's `README.md` and `config.json` (`curl -sL
https://huggingface.co/<repo>/raw/main/<file>`), plus the quant repo's
README, which usually names the llama.cpp build used and any MTP or vision
notes.

MTP comes in two forms, and you check both:
- **Built-in layers**: `nextn_predict_layers` in each quant's own header.
- **Standalone heads**: every file `hf_quants.py` lists under "standalone MTP
  heads", including copies outside `MTP/`. Run `gguf_header.py --tensors` on
  each candidate and apply the four checks in guide §4a. The `[shared:
  unusable]` flag is a name hint; the tensor verdict decides.

## 3. Ask the user what is genuinely theirs to decide

Use AskUserQuestion. Don't guess, and put the recommended option first. Guide
§3 lists the decisions: almost always the **quant ladder** (offer the closest
existing family's ladder by the same quantizer, sizes in the option text) and
the **family slug**. When usable standalone heads exist, also ask **which
heads ship**, with sizes: each listed head downloads with the family's first
quant. Add anything else the facts leave ambiguous.

## 4. Derive each field

Apply guide §4 field by field. Two things specific to this procedure:
- Find current precedent with greps rather than trusting the guide's
  snapshot, for example
  `grep -rh -A3 '"knob_defaults"' src/kodo/llms/local_registry/catalog | sort | uniq -c`.
- Generate the files with a short throwaway script in the scratchpad, not by
  hand, so 5-20 near-identical files can't drift. Use
  `json.dumps(indent=2, ensure_ascii=False)` plus a trailing newline.

Find the `llamacpp_version` with `llamacpp_first_release.py` on the fix PRs
or commits you identified (guide §4, the four checks).

## 4a. MTP heads

Only when step 3 settled on shipping heads. Write
`catalog/<base_llm>/mtp_sidecars.json` per guide §4a: the head id is the
lowercase quant type and is permanent; `filename` includes the folder;
`size_hint` has two decimals; and `llamacpp_version` goes on each head (at
least `MTP_HEAD_MIN_LLAMACPP_VERSION`), never on the entries. Leave the entry
files alone: an entry with built-in layers keeps `spec-decoding-mtp`, and no
file names the head picker. List rejected heads, and why, in the README.

## 5. Thinking tiers

Apply guide §5. If the model needs a **new** template-argument family, the
guide's five-item list spans two repos. The kodo-vsix half is in
`../kodo-vsix` (`src/llm-registry-types.ts`, `src/webview/ModeControls.tsx`).

## 6. README and docs

Apply guide §6: write `catalog/<base_llm>/README.md`, including the user's
decisions from step 3, and add the slug everywhere families are enumerated
(`grep -rn '<an existing slug>' doc README.md`).

## 7. Verify

Run guide §7 exactly:

```bash
hatch run python .claude/skills/add-local-llm/scripts/verify_family.py <base_llm>
for each entry and head: curl -s -o /dev/null -w '%{http_code}' -I -L https://huggingface.co/<repo>/resolve/main/<filename>   # expect 200
hatch run check
```

In `verify_family.py`'s output, check each entry's `MTP control` line against
the table in guide §4a, and that the heads it lists are the ones step 3 chose.

## 8. Report

Tell the user what shipped, the non-obvious calls (the pin, MTP, thinking),
what was not verified (usually: actually running the model), and any open
choice. Don't commit unless asked.

Durable knowledge from this run goes into the repo, not into any local or
per-user store: decisions and family-specific findings go in the family
`README.md`, and a new general rule or trap goes in the guide (§9).

## 9. Keep the guide and this skill current

If any step was wrong for the current code (a moved symbol, a new required
field, a new knob, a changed convention, a new trap), fix it before
finishing:
- rules, reasons and traps → `doc/ADDING_A_LOCAL_LLM.md` (§8 is the trap list);
- procedure, scripts and the contract table → this skill.

The repo copy at `.claude/skills/add-local-llm/` is canonical. A personal
`~/.claude/skills/add-local-llm` may be a symlink to it. Fix the bundled
scripts the same way when they break.
