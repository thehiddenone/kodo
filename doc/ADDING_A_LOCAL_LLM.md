# Adding a Built-in Local LLM

> What a catalog entry *is* — the file format, the knobs, MTP heads, thinking
> families, the startup purge: [LLM_REGISTRY.md](LLM_REGISTRY.md) §4. How its
> GGUF gets downloaded: [LOCAL_MODEL_MANAGER.md](LOCAL_MODEL_MANAGER.md). How
> llama-server is launched for it: [LOCAL_INFERENCE.md](LOCAL_INFERENCE.md).
>
> **This document is about models that ship *in the repo*** — files under
> `src/kodo/llms/local_registry/catalog/`. A user adding a model to their own
> `~/.kodo/local_llms/` does that through the Model Importer agent
> ([LLM_REGISTRY.md](LLM_REGISTRY.md) §4.0b), not by following this guide.

A working guide to adding a new local model family to kodo's shipped catalog,
or more quants to an existing family. It holds the rules and traps that came
out of the families added so far; the reference doc above explains the
mechanisms they protect. The repo's `add-local-llm` Claude Code skill
(`.claude/skills/add-local-llm/`) walks this procedure step by step and
bundles scripts for the GGUF and Hub reads.

## Contents

1. [What a change consists of](#1-what-a-change-consists-of)
2. [Facts come from the GGUF, not the card](#2-facts-come-from-the-gguf-not-the-card)
3. [Decisions that belong to the maintainer](#3-decisions-that-belong-to-the-maintainer)
4. [Deriving each field](#4-deriving-each-field)
   - [4a. Standalone MTP heads](#4a-standalone-mtp-heads-mtp_sidecarsjson)
5. [Thinking tiers](#5-thinking-tiers)
6. [The family README and the docs](#6-the-family-readme-and-the-docs)
7. [Verifying](#7-verifying)
8. [Traps](#8-traps)

## 1. What a change consists of

- One JSON file per quant at `catalog/<base_llm>/<name>.json`
  ([LLM_REGISTRY.md](LLM_REGISTRY.md) §4.0). The directory is the family slug
  and the file stem is the entry's `name`.
- For a new family, `catalog/<base_llm>/README.md`: everything the JSON can't
  say (§6).
- Optionally `catalog/<base_llm>/mtp_sidecars.json`, when the quant repo ships
  standalone MTP heads ([LLM_REGISTRY.md](LLM_REGISTRY.md) §4.0a; how to write
  it: §4a below).
- Thinking-tier wiring in `_thinking.py` (and possibly `_llama.py` and
  kodo-vsix), if the model has a usable thinking control (§5 below).
- A new private knob only if the model needs one: its own
  `_knobs_<family>.py`, registered in `_knobs_table._ALL_KNOBS`.
- Doc updates wherever families are enumerated (§6).

The most recently added families are the best working examples of the
current conventions:

```bash
git log --diff-filter=A --name-only --format='%h %ad' -- src/kodo/llms/local_registry/catalog | head -60
```

## 2. Facts come from the GGUF, not the card

Model cards and repo names leave things out. Ornith 1.5 35B-A3B's card never
mentions MTP, yet its GGUFs carry the layers. An `-MTP-GGUF` repo can lack
them, and two sizes of one family can be different architectures. The one
dependable source is the GGUF header itself, which you can read with an HTTP
range fetch of the file's first few MB, without downloading the model. A
248K-token vocabulary alone makes the header about 11 MB, so fetch in growing
windows. `kodo.llms.local.read_gguf_metadata` does exactly that, but it cuts
string values at 2000 characters (`_MAX_STRING_CHARS`), so it can't return a
full chat template. The skill's `gguf_header.py` can.

Record these for **every quant that will ship**, not just one per family:

| Header key / source | Drives |
|---|---|
| `general.architecture`, `<arch>.context_length` | `context_window`, which context knob applies (§4) |
| `<arch>.nextn_predict_layers`, `<arch>.nextn_shared_target_tensors` | `mtp_supported` / `spec-decoding-mtp`, and whether a head is usable (§4a) |
| `expert_count`, `expert_used_count` | dense vs MoE: the `gpu_tip` offload wording |
| `attention.head_count_kv` (often a per-layer array), key/value lengths, `kv_lora_rank`, sliding-window keys | the KV-cache estimate behind the memory fields |
| `tokenizer.chat_template` (full text) | thinking on/off and its default, thinking tags, tool-call format (§5) |
| the base model's page | license, original author |
| the quant repo's README | quantizer, the llama.cpp build the quants were made with, MTP/vision notes |
| `mmproj-*` files in the repo | vision companions, which the catalog can't declare (§8) |
| files under an `MTP/` folder or named `mtp-*` | standalone MTP heads (§4a); read each head's header **and tensor list** too |

For a Hugging Face safetensors repo (no GGUF), MTP shows up as `mtp.*` keys in
`model.safetensors.index.json`. `transformers`' `config.json` omits MTP
entirely, so don't look for it there.

## 3. Decisions that belong to the maintainer

Settle these with the person making the change. Don't pick them silently:

- **The quant ladder**, i.e. which rungs ship. The established precedent for a
  bartowski family is five rungs, BF16/Q8_0/Q6_K/Q5_K_M/Q4_K_M, plus a
  three-rung low-RAM tail. The tail is Q3_K_XL/IQ3_M/Q2_K_L on large models
  and Q3_K_L/IQ3_M/Q2_K on small ones, for the size reason in §8. Ornith 1.5
  and Ling-3.0-Flash follow it. Offer the closest existing family's ladder by
  the same quantizer, and quote sizes.
- **The family slug (`base_llm`).** It's the group header kodo-vsix shows,
  and it keys the thinking tables. Offer the naming styles already in the
  catalog: `Qwen38-27B`, `Ornith15-9B`, or dotted names like
  `Laguna-S-2.1` and `Ling-3.0-Flash`.
- **Which standalone MTP heads ship**, when the repo has usable ones (§4a).
  Every listed head downloads automatically alongside the family's first
  installed quant, so each one costs every user of the family disk and
  bandwidth (Qwen38-Flash-Next's three heads total 14.7 GB). Quote sizes.
- **Whether to ship the card's sampling recommendation** as
  `knob_defaults`, when the card gives one (§4, `knob_defaults`). Quote the
  card's exact wording and whether the GGUF header's `general.sampling.*`
  keys agree.
- **Anything the facts leave open**: a model with no clear thinking
  mechanism, or a choice between quant repos.

Record each decision and its reason in the family README.

## 4. Deriving each field

Explain any non-obvious choice in the family README, not only in a commit
message.

- **`name` (the file stem)**: `<quantizer>-<slug-ish>-<quant>`, following the
  closest family's casing and separators (`bartowski-ling-3-0-flash-q4-k-m`).
  Treat it as permanent. It keys the download directory and every stored
  profile, active profile and knob selection, so renaming it later orphans all
  of them, and the next start's purge deletes the download
  ([LLM_REGISTRY.md](LLM_REGISTRY.md) §4.1b). There is no rename migration, by
  design.
- **`filename`**: the first shard of a split GGUF. **`size_hint`**: the sum
  of all shards in decimal GB, to three significant figures: one decimal
  from 10 GB up (`"77.8 GB"`), two below (`"5.54 GB"`).
- **`context_window`**: the header's `<arch>.context_length`.
- **`knobs`**: every id in `SHARED_KNOBS`, in that order (this includes
  `cpu-moe` even on dense models). Then add:
  - a context knob only if `context_knob_architectures()` maps one to
    **exactly** this `general.architecture` (§8);
  - `spec-decoding-mtp` plus `"mtp_supported": true` only when the quant's
    own header has `nextn_predict_layers > 0` and the layers aren't
    shared-target, checked per quant. These are the **built-in** MTP layers.
    Keep listing `spec-decoding-mtp` even when the family also has standalone
    heads: an entry file describes its own GGUF, and the head picker is
    attached only when the registry is served (§4a). Never write a
    `spec-decoding-mtp-head:…` id into an entry file.
- **`knob_defaults`**: `{"kv-cache": "f16"}` for F16/BF16 weights, `{}`
  otherwise ([LLM_REGISTRY.md](LLM_REGISTRY.md) §4.6a), plus sampling
  defaults only when the maintainer chose to ship the card's (§3). Most
  families keep the shared defaults. The Granite 4.2 cards insist on
  temperature 1.0 / top_p 0.95 for every task, and both sizes ship
  `{"tail-culling": "none", "temperature": "t10", "nucleus-sampling":
  "p095"}`. Ling-3.0-Flash's
  card asks for temperature 0.6 / top_p 0.95 / top_k 20, and it ships
  `{"temperature": "t06", "nucleus-sampling": "k20-p095", "tail-culling":
  "none"}`. Map the card's values onto existing knob options. If a value has
  no matching option, say so instead of adding one silently. Mind min-p:
  cards usually assume vLLM/SGLang, where min_p defaults to 0, but
  llama.cpp's own default is 0.05. Leaving `tail-culling` on `off` keeps the
  0.05, and only `none` turns it off. Record the choice and the card's
  wording in the README either way.
- **`llamacpp_version`**: the oldest llama.cpp build that runs the model
  **correctly for an agent**. This is often newer than the build the quants
  were made with. Check all four:
  1. when the architecture landed (`src/models/<arch>.cpp` history);
  2. whether llama.cpp's chat parser handles this template's thinking tags
     and tool calls (`common/chat.cpp` detection, `common/parsers/<family>.cpp`,
     and later fixes to both);
  3. whether MTP drafting works for the architecture (any `-md` standalone
     head needs at least b11330, [LLM_REGISTRY.md](LLM_REGISTRY.md) §4.0a);
  4. open upstream issues about the model.

  If a fix PR was closed unmerged, the PR's comments usually name what
  superseded it. Worked example: bartowski made the Ling-3.0-Flash quants at
  b10472, which loads the model. But until b11057 (llama.cpp #28682) a tool
  call emitted before `</think>` came back as reasoning text, so the agent
  loop saw no tool calls at all, and an invalid-UTF-8 byte piece 500'd the
  parse until b11063 (#29161, which replaced the closed #28724). The pin is
  11063 (`catalog/Ling-3.0-Flash/README.md`).
- **`min_memory` / `memory`** (GB, combined VRAM+RAM): total ≈ weights + KV
  cache at 128K + 1-2 GB compute. Compute the KV from the header: for each
  layer that has KV, `n_head_kv × (key_len + value_len) × bytes_per_element ×
  context`. Use the MLA latent (`kv_lora_rank + rope dim`) where present, and
  count recurrent/linear-attention layers (`head_count_kv = 0`) as about 0.
  The element size follows the `kv-cache` knob's default (`q8_0`, or `f16`
  where `knob_defaults` says so). For example, Ling-3.0-Flash has KV on only
  8 of 43 layers, so its totals are file size + ~3-4 GB. `min_memory` is the
  smallest sensible total and `memory` the comfortable one. kodo-vsix compares
  both against detected VRAM+RAM ([LLM_REGISTRY.md](LLM_REGISTRY.md) §4.4).
- **`gpu_tip` / `mac_tip`**: copy the voice of the existing entries
  ([LLM_REGISTRY.md](LLM_REGISTRY.md) §4). `gpu_tip` pairs a modest 8-16 GB
  GPU with DDR5 system RAM and names the offload style: expert offload for
  MoE, layer offload for dense. `mac_tip` maps the same total onto MacBook Pro
  tiers, or says plainly when only a Mac Studio fits. Keep both consistent
  with `min_memory`/`memory`.
- **`description`**: `"<Display Name> <QUANT> by <quantizer>"`. It's the only
  label users see; `name` never appears in the UI.
- **`license_name` / `license_url` / `llm_author`**: from the **base**
  model's page, since quant repos rarely restate licensing.
  **`quant_author`**: the quant repo's owner as written.

### 4a. Standalone MTP heads (`mtp_sidecars.json`)

Some quant repos ship the MTP head as its own small GGUF, instead of or as
well as baking the layers into every quant. unsloth puts them in an `MTP/`
folder, sometimes with a duplicate at the repo's top level. Any quant of a
model works with any of that model's heads, so a family lists its heads once,
in `catalog/<base_llm>/mtp_sidecars.json`. The mechanism (picker, launch,
downloads, pruning) is [LLM_REGISTRY.md](LLM_REGISTRY.md) §4.0a. This section
covers writing the file.

**Check every candidate head's own header and tensors.** List a head only
when all of these hold:

1. `<arch>.nextn_predict_layers > 0`: it really carries MTP layers.
2. Its `general.architecture` equals the family's quants' architecture.
   llama.cpp rejects a head built for another model outright.
3. It is **self-contained**: its tensor list includes `token_embd.weight` and
   `output.weight`, and its header has no `<arch>.nextn_shared_target_tensors`.
   unsloth's `shared-*` heads lack both tensors and borrow them from the
   running model, which mainline llama.cpp can't do (they load only on
   unsloth's fork). The `shared` in the file name is only a hint; the header
   and tensor list decide. The skill's `gguf_header.py --tensors` prints the
   tensors and a verdict.
4. The llama.cpp build that loads it exists: at least b11330 for any `-md`
   head (llama.cpp #29761 fixed the draft loader opening the target model's
   path instead of the head file), and later if the architecture's MTP graph
   landed later.

**The file:**

```json
{
  "q8_0": {
    "repo_id": "unsloth/Qwen3.8-Flash-Next-GGUF",
    "filename": "MTP/mtp-Qwen3.8-Flash-Next-Q8_0.gguf",
    "quant_type": "Q8_0",
    "size_hint": "4.14 GB",
    "llamacpp_version": 11330
  }
}
```

- **The head id** (the key) is the head's quant type as a lowercase slug
  (`bf16`, `q8_0`, `q4_k_m`), never `off` or `builtin`. Treat it as
  permanent, like an entry `name`: it is stored as the user's knob selection
  and inside the head's download-record id, so renaming it orphans both.
- `repo_id`, `filename` (the path inside the repo, folder included) and
  `quant_type` are required. `quant_type` needs a readable bit width, because
  the picker orders heads most precise first by it, not by speed.
- `size_hint`: the decimal-GB file size to two decimals, as the shipped
  heads use (`"1.37 GB"`).
- `llamacpp_version`: the head's own floor (`MTP_HEAD_MIN_LLAMACPP_VERSION`,
  11330, or later per check 4). It goes on the head, **not** on the entries:
  a build older than the head's floor just launches without speculative
  decoding, and nobody is told to upgrade for a feature they haven't
  switched on.
- When the repo has the same head twice (in `MTP/` and at the top level),
  list one copy. The shipped families use the `MTP/` path.
- Every listed head is downloaded with the family's first installed quant
  (§3 covers choosing which to ship).

**What users get.** Every entry of a family with heads is served a head
picker (`spec-decoding-mtp-head:<base_llm>`, plus `:builtin` when that
entry's own GGUF has built-in layers) in place of the `spec-decoding-mtp`
checkbox. The two shipped shapes:

| family | entry files | served picker options |
|---|---|---|
| `Qwen38-27B` (built-in layers **and** heads) | `mtp_supported: true`, list `spec-decoding-mtp` | `off`, `builtin`, `q4_0` |
| `Qwen38-Flash-Next` (heads only) | `mtp_supported: false`, no MTP knob | `off`, `bf16`, `q8_0`, `q4_k_m` |

In the family README, record which heads you checked, which you rejected and
why (shared, wrong architecture, no layers), and the build floor.

Generate the near-identical files with a short throwaway script rather than
by hand, so they can't drift apart. Write them as
`json.dumps(body, indent=2, ensure_ascii=False)` plus a trailing newline,
which is the shipped files' canonical form (`entry_to_catalog_json`).

## 5. Thinking tiers

Decide from the **chat template and llama.cpp's parser**, not the card. The
families and their mechanics are in [LLM_REGISTRY.md](LLM_REGISTRY.md) §4.5.

- **The template thinks with `<think>`-style tags that llama.cpp's parser
  declares**, so `--reasoning-budget` can see them: join
  `QWEN_REASONING_BUDGET_FAMILY` and add a row to `QWEN_TIER_TOKEN_BUDGETS`,
  copying the scale of the closest family (for example 512..16384 for
  mid-size MoE families). A template that doesn't think by default also needs
  the `enable_thinking` request kwarg, the way `Qwen35-9B` is handled in
  `_llama.py`.
- **The template takes a graded effort/strength kwarg**: join the family
  with the **same kwarg name and the same tier vocabulary**, or add a new
  family. A new family needs all of the following, and missing any one breaks
  it:
  1. a frozenset, its tiers and its default in `_thinking.py`, plus the
     `local_thinking_*` branches;
  2. a `_TIER_TEMPLATE_KWARG` row in `kodo/llms/llamacpp/_llama.py` (a missing
     row raises on every request);
  3. the family in `_catalog._validate_catalog`'s union;
  4. the test family tables
     (`test_build_thinking_extra_body_handles_every_tier_of_every_family` in
     `test/test_llama_pure.py` walks every shipped tiered model);
  5. kodo-vsix: `THINKING_FAMILIES` in `src/llm-registry-types.ts` and the
     tier description table in `src/webview/ModeControls.tsx`. An older VSIX
     coerces the unknown family to "no thinking mode".
- **No usable mechanism**: no family. Say so in the README.

A thinking-table slug must match a shipped `base_llm`, or kodo refuses to
start. That means renaming a family directory requires editing
`_thinking.py` in the same commit.

## 6. The family README and the docs

- **`catalog/<base_llm>/README.md`** records the architecture facts and how
  each was verified, the context, MTP, thinking and version-pin reasoning,
  the ladder and why, the maintainer's decisions (§3), anything deliberately
  left unwired (vision, separate draft models), and sampling notes.
  `catalog/Ling-3.0-Flash/README.md` and `catalog/Ornith15-9B/README.md` are
  good examples.
- **Keep history.** Later corrections go in as a dated
  `**Update YYYY-MM-DD:**` paragraph that says what was believed, what showed
  it wrong, and what holds now. Don't rewrite the earlier text. The same rule
  applies to the reference docs (see the Muse Glimmer note in
  [LLM_REGISTRY.md](LLM_REGISTRY.md) §4.5).
- **Enumerations in the docs**: `grep -rn '<an existing slug>' doc README.md`
  finds every place families are listed, such as the §4.5 family lists, the
  §4.6 `mtp_supported` list and the §4.0a heads table. Add the new slug to
  each, and fix any staleness you notice.

## 7. Verifying

```bash
hatch run python .claude/skills/add-local-llm/scripts/verify_family.py <base_llm>
# for each entry and each head; expect 200:
curl -s -o /dev/null -w '%{http_code}' -I -L https://huggingface.co/<repo_id>/resolve/main/<filename>
hatch run check     # fmt, lint, typecheck, tests
```

- Run Python through `hatch run`. A bare `pytest` may resolve to a global
  install that lacks the project's dependencies, and the uv `.venv` has no
  pytest at all.
- `hatch run python -c '…'` treats `{…}` as hatch placeholders and mangles
  them, so put probe code in a file.
- Shipped files are validated at import, and an invalid one stops kodo from
  starting at all, so a clean import is the first check. That covers
  `mtp_sidecars.json` too, including one placed in a directory with no
  shipped entry.
- `verify_family.py` prints each entry's **served MTP control**. Check it
  against the §4a table: the built-in checkbox when only built-in layers
  exist, a picker with `builtin` when there are both, a picker without it
  for heads only, and `none` otherwise. It also lists the heads as served.
- If a test fails because it enumerates families or counts entries, first ask
  whether it should read the registry instead (CLAUDE.md, "Don't hardcode
  which tool/spec has a property"). Only then edit its expectations.
- Not verified by any of the above: actually running the model. Say so when
  handing the change over.

## 8. Traps

Each of these has cost a real change before:

- **The context knob must match the exact architecture, and a mismatch fails
  silently.** Dense and MoE sizes of one family differ (`qwen35` vs
  `qwen35moe`). A knob whose `--override-kv` names a key the GGUF lacks is
  ignored by llama.cpp, so the 512K/1M options look like they work while the
  cache stays at the trained length ([LLM_REGISTRY.md](LLM_REGISTRY.md) §4.6,
  the Ornith paragraph).
- **MTP support varies within one family across quant repos.**
  `AlexAtomic/qwen36-27b-GGUF` has no `nextn_predict_layers` key, while its
  Unsloth siblings in `Qwen36-27B` do. Check every repo you use, and never
  infer support from `-MTP-GGUF` branding (§4.6).
- **`shared-*` MTP heads are unusable.** They carry
  `nextn_shared_target_tensors` and lack `token_embd`/`output`, and mainline
  llama.cpp can't borrow tensors across models (§4a). Some families have
  heads but no built-in layers (Qwen38-Flash-Next), so heads are their only
  MTP route.
- **Heads hide outside `MTP/`.** unsloth also put a head at the top level of
  `unsloth/Qwen3.8-Flash-Next-GGUF` (`mtp-Qwen3.8-Flash-Next-Q8_0.gguf`).
  Anything named `mtp-*` is a head, not a quant.
- **Entry files never name the head picker.** It is attached when the
  registry is served; an entry with built-in layers keeps
  `spec-decoding-mtp` in its file (§4).
- **`_L`/`_XL` quants are not smaller at small parameter counts.** They keep
  embedding/output tensors at Q8_0, which dominate a small model: Ornith 1.5
  9B's Q3_K_XL is 6.00 GB, larger than its own 5.91 GB Q4_K_M. Never copy a
  quant name across sizes without checking bytes.
- **Vision models ship text-only.** `LocalLLMEntry` has no projector field,
  so `mmproj-*` companions can't be declared. Say so in the README. Wiring it
  would need a `_types.py` field, download wiring and a `--mmproj` launch arg.
- **`mmproj-*`, imatrix files and standalone MTP heads are never quants.** Only
  the first shard of a split file is a `filename`.
- **Template kwarg names differ between families** (`reasoning_effort` for
  GPT-OSS and Qwen4exp, `reasoning_strength` for Muse Glimmer), and llama.cpp
  silently ignores an unknown name, leaving the model at its default.
- **The quantizer's build is not the pin.** See `llamacpp_version` in §4.
- **Renaming a shipped `name` deletes users' downloads** at their next start
  (§4 above, [LLM_REGISTRY.md](LLM_REGISTRY.md) §4.1b). Renaming a family
  directory also breaks startup until `_thinking.py` follows (§5).
- **Truncated templates.** kodo's own header reader cuts strings at 2000
  characters, so read the full `tokenizer.chat_template` with a tool that
  doesn't (§2).
