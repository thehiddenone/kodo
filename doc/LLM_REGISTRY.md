# LLM Registry — Cloud/Local Split, Effort Levels, Local-Model Management

> Where models come from, how a sub-agent's `capability` turns into an actual
> model, the four local-registry entry kinds, the llama-server binary
> override, and named multi-key cloud credential management.

Companion to [LLM_GATEWAY.md](LLM_GATEWAY.md) (request scheduling/feeds, once
a plugin is resolved), [WS_PROTOCOL.md](WS_PROTOCOL.md) (the wire shapes
referenced throughout — §4.1, §5.12/§5.12a, §6.3/§6.4, §7.5/§7.6), and
[LOCAL_MODEL_MANAGER.md](LOCAL_MODEL_MANAGER.md) (how a `hardcoded_hf`/
`custom_hf` entry's GGUF actually gets downloaded/paused/resumed/removed on
disk — this doc covers only *which* entries exist, not how their bytes are
fetched).

---

## 1. Overview

The registry used to be one flat `dict[str, LLMEntry]` (`kodo/llms/_registry.py`)
shared by cloud and local models, discriminated by a `residence` field. It is
now two independent registries:

- **Cloud registry** (`kodo/llms/_cloud_registry.py`) — a hardcoded, two-tier
  `vendor → CloudLLMEntry` tree. 100% compiled-in; there is no user-editable
  part, since adding a model always implies a matching plugin/pricing update.
- **Local registry** (`kodo/llms/_local_registry.py`) — hardcoded GGUFs
  merged with a user-managed external collection persisted in
  `~/.kodo/etc/local-llm-registry.json`. Every entry here runs on llama.cpp;
  there is no `residence` field any more (it would always say `"local"`).
  `~/.kodo/etc/` itself is created eagerly by `WorkspaceLayout.init()` at every
  server startup (`kodo/project/_layout.py`, called from
  `server/_app.py:create_app`) — alongside `logs/`/`sessions/` — so it exists
  even before anything has been written into it.

A model *key* means different things depending on which registry it came
from: for cloud it is the `model_id` (the string sent to the provider API,
also the registry key — no separate synthetic key); for local it is the
entry's own `name`. `kodo.llms.get_context_window(model_key, kodo_dir)`
checks both registries so callers that only have a resolved key (e.g. the
compactor) don't need to know which one it came from.

---

## 2. Effort levels (`capability`)

Sub-agents declare a `capability` in their markdown frontmatter — unchanged
field name, now four values instead of three: `low`, `medium`, `high`, `max`
(default `medium` when absent/invalid; see `kodo/subagents/_loader.py`).
Conceptually, for Anthropic: `low` ~ Haiku, `medium` ~ Sonnet, `high` ~ Opus,
`max` ~ Fable — but this is only the *default* mapping shown in the Cloud AI
Settings webview; each of the four effort panels lets the user assign **any**
model configured for that vendor, not just the suggested one.

`capability` is resolved to an actual model key by
`LLMPlumbingMixin._resolve_model_key` (`kodo/runtime/_engine/_llm.py`), reading
fresh settings on every dispatch:

- `mode == "local"` — every capability collapses to the single
  `settings["models"]["local"]` key (one local llama-server serves one model
  at a time; there's no per-capability local selection).
- `mode == "cloud"` — `settings["models"]["cloud"][active_cloud_vendor][capability]`,
  falling back to that vendor's `medium` entry, then the capability name
  itself, if unset.

`_resolve_plugin` then determines cloud-vs-local **by registry membership of
the resolved key**, not by re-checking `mode` — this matters for
`force_model_key` (used so a model-switch compaction runs on the *previous*
model even if `mode` itself changed since that model was selected; see
`ContextCompactor.handle_config_changed`, `kodo/runtime/_engine/_compaction.py`).

---

## 3. Cloud registry

```python
@dataclass(frozen=True)
class CloudLLMEntry:
    name: str            # display name, e.g. "Claude Opus 4.8"
    model_id: str        # API model id — also the registry key
    description: str
    context_window: int = 0
    recommendation: str = ""  # "when to pick this" blurb, Cloud AI Settings webview only
```

`kodo/llms/_cloud_registry.py` holds one hardcoded tuple of entries per
vendor (`_ANTHROPIC_MODELS`, ...), aggregated into `_CLOUD_REGISTRY: dict[str,
tuple[CloudLLMEntry, ...]]` keyed by a lowercase vendor slug (`"anthropic"`).
A separate `_CLOUD_VENDOR_MODULE` dict maps that same vendor slug to the
dotted plugin module (`"kodo.llms.anthropic"`) — one plugin class per vendor,
shared by every model from that vendor (unlike the old per-model `module`
field). `_CLOUD_VENDOR_DISPLAY` holds the human-readable name shown in the UI
("Anthropic").

Today's Anthropic entries (`claude-fable-5`, `claude-opus-4-8`/`4-7`/`4-6`,
`claude-sonnet-5`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`) — seven
models, defaulted one per effort tier (low→haiku, medium→sonnet-5,
high→opus-4-8, max→fable-5) but all seven selectable in any of the four
effort panels. Fable is listed first in `_ANTHROPIC_MODELS` — deliberately
out of API-vintage order — so the Cloud AI Settings webview renders it as the
top/first option in every effort panel; each entry's `recommendation` string
is the one-line "when to pick this" blurb shown next to it there. Pricing
(`kodo/llms/anthropic/_usage.py`) is keyed by version-agnostic family prefix
(`claude-opus`, `claude-sonnet`, `claude-haiku`, `claude-fable`) so a new
version of an existing family is priced correctly without a pricing-table
change; Fable is priced at Opus-tier rates as the max-effort tier.

**Adding a cloud vendor or model is a code change** — add a tuple + registry
entries in `_cloud_registry.py`, and if it's a new vendor, a plugin
implementing `LLMPlugin` plus a `_CLOUD_VENDOR_MODULE` entry. There is no
external/JSON part to this registry.

---

## 4. Local registry

```python
@dataclass(frozen=True)
class LocalLLMEntry:
    name: str
    kind: str  # "hardcoded_hf" | "custom_hf" | "custom_file" | "custom_server_url"
    description: str = ""
    repo_id: str = ""       # hardcoded_hf / custom_hf
    filename: str = ""      # hardcoded_hf / custom_hf
    context_window: int = 0  # any llama-server kind — the active flavor's own -c/--ctx-size overrides it, see §4.6
    flavors: tuple[LlamaFlavor, ...] = field(default_factory=LlamaFlavor.default_flavours_field)  # predefined; hardcoded_hf only — see §4.6
    path: str = ""          # custom_file
    url: str = ""           # custom_server_url
    base_llm: str = ""      # hardcoded_hf only — e.g. "qwen36-27b"
    quant_author: str = ""  # hardcoded_hf only — e.g. "Unsloth"
    quant_type: str = ""    # hardcoded_hf only — e.g. "UD_Q4_K_XL"
    size_hint: str = ""     # hardcoded_hf only — e.g. "28.6 GB"
    gpu_tip: str = ""       # hardcoded_hf only — e.g. "~43GB total at 128K
                             # context — no need to hunt for a giant
                             # workstation card. llama.cpp splits dense
                             # models layer-by-layer between GPU and CPU, so
                             # an 8GB GPU (e.g. RTX 4060) carries a solid
                             # share of the layers at full speed, with
                             # ~48GB of ordinary DDR5 system RAM covering
                             # the rest."
    mac_tip: str = ""       # hardcoded_hf only — e.g. "Needs ~43GB —
                             # comfortable on a 64GB MacBook Pro (M4 Pro/Max
                             # or M5 Pro/Max); a 48GB config is tight."
    min_memory: int = 0     # hardcoded_hf only — absolute minimum combined VRAM+RAM (GB); 0 = unknown
    memory: int = 0         # hardcoded_hf only — recommended combined VRAM+RAM (GB); 0 = unknown
```

`base_llm`/`quant_author`/`quant_type`/`size_hint`/`gpu_tip`/`mac_tip`/
`min_memory`/`memory` are metadata-only (never read by `ensure_llama_running`
or the WS handlers) — they identify, respectively, the original unquantized
model, who produced the quant, the quant spec, the GGUF file's on-disk size
(as displayed on the model's HuggingFace file listing, hand-copied — not
fetched at runtime), a hand-written discrete-GPU-plus-system-RAM
recommendation, a hand-written MacBook Pro (Apple Silicon unified-memory)
recommendation, and two hand-picked combined-memory thresholds (GB) used for
the client-side hardware comparison below, for every compiled-in
`hardcoded_hf` entry in `_HARDCODED_LOCAL_MODELS`. `gpu_tip` and `mac_tip`
are both rough estimates off the same underlying total-memory figure —
weight size (`size_hint`) plus an approximated KV-cache footprint at 128K
context (scaled from each model family's known/assumed architecture: layer
count, attention-head config, and the KV cache quantization each entry's
default flavor requests — see §4.6). `gpu_tip` deliberately does **not** round that figure
to "a single GPU big enough to hold it all" — almost nobody owns a
48GB+ workstation card. Instead it frames the figure as a modest 8-16GB
consumer GPU (what most people actually own, e.g. RTX 4060/RTX 3060
Ti/RX 7600 at 8GB, or RTX 4060 Ti 16GB/RTX 5070 Ti at 16GB) plus enough
ordinary DDR5 system RAM to cover the remainder, since llama.cpp can split a
model's weights across both: per-layer offloading (`-ngl`) for dense models,
or MoE-expert offloading (keeping shared/attention tensors on the GPU and
spilling inactive experts to RAM) for sparse models, which loses much less
speed than the dense case since only a handful of experts actually run per
token. `gpu_tip` calls out which offloading style applies. `mac_tip` maps
the same total-memory figure onto MacBook Pro unified-memory tiers (M4/M4
Pro/M4 Max and M5/M5 Pro/M5 Max configs) with extra headroom built in for
macOS's own memory overhead — Apple Silicon has no separate VRAM/RAM split
to offload across, so it stays framed as one pool. Neither `gpu_tip` nor
`mac_tip` is a precise sizing tool. `min_memory`/`memory` are the same
underlying total-memory estimate expressed as two plain integers instead of
prose — combined VRAM + system RAM together, not VRAM alone — see §4.4 for
how kodo-vsix compares them against `detected_vram_gb` + `detected_ram_gb`.
All eight
fields are always `""`/`0` for `custom_hf`/`custom_file`/`custom_server_url`
— none of the `local_llm.add_*` WS commands accept them, so a user-added
entry can never populate them. All eight of
these **are** included in
`_local_registry_payload()`'s wire shape (§4.4), alongside the raw
`context_window` field itself — added so kodo-vsix can render the sidebar's
per-card "Context:" line (§4.4) — though its *effective, flavor-resolved*
value is still never sent over the wire as its own field; that value is
computed twice independently instead, server-side via
`resolve_context_window` (§4.6, for auto-compaction budgeting) and
client-side in kodo-vsix (`flavorContextSize`/`resolveContextSize` in
`llm-registry-types.ts`, mirroring `LlamaFlavor.get_context_size`/
`resolve_context_window` for display). `flavors` **is** sent to
kodo-vsix too — predefined entries plus any custom ones merged in, see §4.6
— since flavors are the only source of llama-server launch args now: there
is no `llama_args` field on `LocalLLMEntry` at all any more.

Four entry kinds:

| kind | added via | installed-state rule | install/uninstall? |
|---|---|---|---|
| `hardcoded_hf` | compiled-in (`_HARDCODED_LOCAL_MODELS`) | installed per `LocalModelManager` state | yes |
| `custom_hf` | "Add local LLM from huggingface.com" | same as `hardcoded_hf` | yes |
| `custom_file` | "Add local LLM from file" | file exists at `entry.path` | no — see below |
| `custom_server_url` | "Add a link to local llama-server" | always installed | no |

`kodo/llms/_local_registry.py` owns `get_local_registry(kodo_dir)`, which
merges the compiled-in tuple with the external collection persisted at
`~/.kodo/etc/local-llm-registry.json`:

```json
{
  "entries": [
    { "name": "...", "kind": "custom_hf", "repo_id": "...", "filename": "...", "description": "...",
      "context_window": 262144 },
    { "name": "...", "kind": "custom_file", "path": "/abs/path/model.gguf", "description": "...",
      "context_window": 262144 },
    { "name": "...", "kind": "custom_server_url", "url": "http://host:port", "description": "..." }
  ],
  "llama_server_override_path": null,
  "flavors": {
    "my-custom-model": [
      { "id": "default", "name": "default", "description": "Default flavor",
        "llama_args": {"--cache-type-k": "q8_0"} }
    ],
    "unsloth-qwen36-27b-q4-k-xl": [
      { "id": "1m-context", "name": "1M Context", "description": "...",
        "llama_args": {"--ctx-size": "1048576", "--rope-scaling": "yarn", "--rope-scale": "4"} },
      { "id": "default", "name": "Default (fp16 KV cache)", "description": "Override of the built-in default",
        "llama_args": {"--cache-type-k": "fp16", "--cache-type-v": "fp16", "--ctx-size": "0", "--jinja": ""} }
    ]
  },
  "active_flavors": {
    "unsloth-qwen36-27b-q4-k-xl": "1m-context"
  }
}
```

The second `unsloth-qwen36-27b-q4-k-xl` flavor above (`id: "default"`) is an
**override** of that entry's built-in predefined `"default"` flavor — the
user edited it via "Manage flavors" (§4.6), which stores the new definition
here under the same id rather than mutating the hardcoded Python literal.

Note that no `entries[]` object carries `llama_args` any more — flavors are
the *only* source of it (§4.6); a `custom_hf`/`custom_file` entry's own
initial args end up in `flavors["<name>"]` (its seeded `"default"` custom
flavor, as shown for `my-custom-model` above) rather than on the entry
itself. `flavors`/`active_flavors` are two more sibling top-level keys in
this same file, unrelated to the `entries` list — see §4.6.

This file is **owned entirely by the Python server** (read and written by
`kodo/llms/_local_registry.py`); kodo-vsix never writes it directly, only
through the `local_llm.*` WS commands (§7.6). `add_local_entry`/
`remove_local_entry` reject duplicate names and reject removing a
`hardcoded_hf` entry; `add_local_entry` also forces `entry.flavors` to `()`
regardless of what's passed in, so a stray predefined-looking literal can
never shadow a same-id custom flavor added later (§4.6). `llama_args`/
`context_window` are optional on the `local_llm.add_huggingface`/
`local_llm.add_file` commands (never offered for `add_server_url`, which
isn't a llama-server process kodo launches) — the kodo-vsix "Add local LLM"
modals still collect `llama_args` as one space-separated `--flag value` line
and parse it client-side into the wire dict shape, and `context_window`
still defaults to `262144` in those modals, but neither sets a field on the
entry any more: `_seed_default_flavor` (`kodo/server/_app.py`) uses them to
create that entry's first (custom) flavor, named/slugged `"default"` to
match the built-in flavor a `hardcoded_hf` entry gets, right after
`add_local_entry` succeeds. `context_window` still falls back server-side to
`get_context_window`'s default when zero/absent (that fallback lives in
`resolve_effective_llama_config`/`get_context_window`, not the add handlers).
(The "manage flavors" modal added in §4.6 uses a different, multi-line input
for the same `llama_args` shape — one flag per line, parsed **server-side**
instead — since a flavor typically carries more flags than a base entry's
initial one does.)

**`custom_file` installed-state is special**: per design, kodo does not copy
or own the file, and its presence is checked **once, by the kodo-vsix
extension, at its own activation** — not re-verified by the Python server on
every `hello.ack`/`registry_state` push, and not re-checked mid-session even
if the file is deleted. The extension caches that boolean for the rest of the
process lifetime (a stale/missing file simply can't be re-selected until the
next VS Code restart, since the UI never rendered it as installed after
detecting the deletion in the first place, and a freshly-added `custom_file`
entry — just picked via a native file dialog that can only return existing
files — is treated as installed for the remainder of that session without
waiting for the next restart). The Python server has no independent opinion
on this — `ensure_llama_running` (§4.1) trusts the path once a
`custom_file` entry is selected as active, since the UI only lets the user
select entries it has already flagged installed.

**`custom_server_url` is not managed by kodo at all** — no download, no
process, always installed. Selecting it as the active local model:

1. Stops kodo's own managed llama-server, if one is running.
2. Points `LlamaPlugin`'s OpenAI-compatible client straight at `entry.url`
   (assumed to already be a running llama-server-or-compatible endpoint).
3. Does **not** start a new managed process — one stays stopped until the
   user selects a `hardcoded_hf`/`custom_hf`/`custom_file` entry again.

This is implemented in `LlamaPlugin.__ensure_running` (`kodo/llms/llamacpp/_llama.py`)
and mirrored in `_app.py`'s `local_llm.start` handler for the explicit
`llama.start` command path.

### 4.1 Install / pause / resume / uninstall

All four are fire-and-forget: the handler replies immediately with
`local_llm.registry_state`, *then* kicks off (or signals) the transfer — there
is no byte-level progress event on the wire. kodo-vsix follows progress by
polling `manager-state.json` directly off disk instead; see
[LOCAL_MODEL_MANAGER.md](LOCAL_MODEL_MANAGER.md) §11 for the full design and
*why* (no connection-broadcast infra needed, survives the requesting window
closing, works the same after a real server restart). Install/resume push
**one further** `local_llm.registry_state` on the same connection once the
background transfer actually finishes (success or failure), so the
`installed`/`installed_path` flip is reflected without a reconnect — see §11.

- **Install** (`local_llm.install {name}`, `hardcoded_hf`/`custom_hf` only) —
  `server/_app.py`'s `_handle_local_llm_install` fires
  `kodo.llms.llamacpp.get_local_model_manager(kodo_dir).download_model(entry.name,
  entry.repo_id, entry.filename)` on a worker thread, keyed by `entry.name`.
  Full design, including *why* this no longer goes through
  `huggingface_hub.hf_hub_download` for the byte transfer, in
  [LOCAL_MODEL_MANAGER.md](LOCAL_MODEL_MANAGER.md).
- **Resume** (`local_llm.resume {name}`) — fires `resume_download(name)` for
  a model that already has a download record (paused, failed, or left
  `DOWNLOADING` by a server restart — see the reconciliation note below).
  Replies with a `local_llm_error` if there's no record to resume.
- **Pause** (`local_llm.pause {name}`) — `LocalModelManager.pause_download`;
  a no-op if nothing is currently transferring for that id.
- **Uninstall** (`local_llm.uninstall {name}`) — `LocalModelManager.uninstall`
  simply deletes the model's own subdirectory — downloads no longer go
  through HF's shared dedup blob cache at all, so there's no cache-eviction
  step any more. A no-op if not installed. Also the "cancel a download"
  action — pauses first, then deletes the partial files.
- **Remove** (`local_llm.remove {name}`) — deregisters a custom entry from
  `local-llm-registry.json`; if it has *any* download record (finished or
  partial — checked via `get_record`, not just "fully installed"), uninstalls
  first to avoid an orphaned partial GGUF. Rejected for `hardcoded_hf`
  entries.

The manager also supports split-GGUF multi-file downloads, mmproj companion
files, and per-call HF tokens — none of that is exercised from these WS
commands yet (tracked as follow-up).

A file left `DOWNLOADING` by a killed/crashed kodo-server is forced to
`PAUSED` the next time `LocalModelManager` is constructed for that models
directory (i.e. effectively "at the next kodo-server startup") — see
LOCAL_MODEL_MANAGER.md §11. The Local Inference Settings webview surfaces
this as a resumable download, same as one the user paused deliberately.

`~/.kodo/etc/local-llm-index.json` (the old flat `{name: path}` index) is
retired — superseded by `LocalModelManager`'s own `manager-state.json`,
scoped under the models directory itself rather than `etc/`.

### 4.2 llama-server binary override

A **global** setting, not a model — addresses the lack of CUDA support in
vanilla llama.cpp on Linux by letting a user point kodo at their own
`llama-server`-compatible build/script instead of the bundled binary, for
**every** local model (hardcoded and custom alike). Stored as
`llama_server_override_path` in the same `local-llm-registry.json` file
(`null` = no override, use the bundled binary).

`ensure_llama_running` (`kodo/llms/llamacpp/_manager.py`) checks the override
before falling back to `LlamaInstall.executable`; the CLI-argument-generation
logic in `LlamaServerConfig`/`LlamaServer.__build_command` is completely
unchanged either way — only the executable path differs. Set/cleared via
`llama_server_override.set {path}` / `llama_server_override.remove` (§7.6),
validated server-side (path must exist).

The Local Inference Settings webview (§6) surfaces this as a standalone
control — a label showing the current override path or "No override" plus
"Set llama.cpp override" / "Remove llama.cpp override" buttons — separate
from the model card grid, since it isn't itself a model.

### 4.3 Hardware detection (`detected_vram_gb`, `detected_ram_gb`)

`kodo/llms/_hardware.py`'s `detect_vram_gb()` and `detect_ram_gb()` are
best-effort local GPU VRAM / system RAM detection, computed fresh on every
`hello.ack` **and** every `local_llm.registry_state` event (both go through
`_local_registry_payload()` now) and sent as the top-level `detected_vram_gb`
/ `detected_ram_gb` fields — see WS_PROTOCOL.md §4.1 for the wire shape.
Together they express "total memory available for a GPU+CPU-offloaded
model" — see §4.4 for how kodo-vsix sums them for the hardware-warning
comparison.

Detection strategy, by platform:

- **macOS**: `detect_vram_gb()` reports total system RAM via
  `psutil.virtual_memory().total`, treated as VRAM-equivalent — Apple
  Silicon shares one unified memory pool between CPU and GPU, so there's no
  separate VRAM figure to query. `detect_ram_gb()` always returns `None` on
  macOS: a separate RAM figure would just double-count the same physical
  memory `detect_vram_gb()` already reports in full.
- **Windows/Linux**: `detect_vram_gb()` sums VRAM across every NVIDIA GPU
  visible to the driver, via `pynvml` (`nvmlDeviceGetMemoryInfo(handle).total`
  per device). **AMD GPUs are not detected** — out of scope for now; an
  AMD-only machine reports `null` for VRAM even with a discrete GPU present.
  `detect_ram_gb()` reports total system RAM via
  `psutil.virtual_memory().total`, independent of any GPU detection.

Both raw byte totals are normalized to the nearest tier in a fixed ascending
list (4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128, 192, 256 GB)
— real hardware rarely reports an exact round number (e.g. a "24GB" card
shows ~23.99 GiB), so nearest-tier snapping gives a clean, stable figure.
Above the top tier (e.g. multi-GPU rigs, or a large-RAM workstation) each
rounds to the nearest 32 GB instead of clamping. Either returns `None` (→
wire `null`) if nothing could be detected: no supported GPU, no driver, or
the detection library isn't installed/importable — every failure mode is
caught and swallowed, since this must never block the `hello` handshake.

### 4.4 kodo-vsix wire shape and the download-progress polling design

`_local_registry_payload()` (`server/_app.py`) sends every `LocalLLMEntry`
field kodo-vsix needs — `name`, `kind`, `description`, `repo_id`, `filename`,
`path`, `url`, `installed`, `installed_path`, `base_llm`, `quant_author`,
`quant_type`, `size_hint`, `gpu_tip`, `mac_tip`, `min_memory`, `memory`,
`context_window` — plus
top-level `llama_server_override_path`, `detected_vram_gb`,
`detected_ram_gb`, and `thinking_families` (§4.5). `installed_path` is new: the absolute path to the
installed file(s) (`LocalModelManager.get_model_path()` for
`hardcoded_hf`/`custom_hf`, `entry.path` for `custom_file`, `null` for
`custom_server_url` or anything not installed) — it's what "Show me local
files" in the Local Inference Settings webview reveals via VS Code's
`revealFileInOS` command, entirely client-side (no extra WS round trip).

kodo-vsix sums `detected_vram_gb` + `detected_ram_gb` (nulls treated as `0`
in the sum, but if *both* are `null` the comparison is skipped entirely —
"unknown — don't warn") and compares that total against each entry's
`min_memory`/`memory` (both GB, same combined-memory units, both `0` meaning
"unknown — don't warn"): below `min_memory` is a red "won't run" warning;
below `memory` (and not already red) is a yellow "may not perform well at
large contexts" warning. When `min_memory == memory` only the red case can
ever fire — meeting the minimum already means meeting the recommendation
too, so there is no separate yellow branch to special-case. On macOS,
`detected_ram_gb` is always `null` (see §4.3), so the sum degrades to
`detected_vram_gb` alone — the single unified-memory figure Apple Silicon
already reports in full.

**Download progress is not part of this payload** — see
[LOCAL_MODEL_MANAGER.md](LOCAL_MODEL_MANAGER.md) §11. kodo-vsix polls
`manager-state.json` directly off disk once a second instead, independent of
the WS connection.

**Display convention: `name` is never shown to the user, `description` always is.** For a `hardcoded_hf` entry, `name` is an internal registry-key slug (e.g. `unsloth-qwen36-27b-q8-k-xl`); `description` is the human-readable label (e.g. "Qwen 3.6 27B UD-Q8_K_XL by Unsloth"). Every kodo-vsix surface that lists local models — the sidebar model-picker cards, the Local Inference Settings model cards, the "running: …" status line, the flavor-management modal title, and download-progress rows — titles itself off `entry.description`, falling back to `entry.name` only where `description` can legitimately be empty (a `custom_*` kind entry, where `name` is whatever display text the user typed when adding it, per §4). `name` still flows through the wire/DOM as a plain identifier (dataset keys, radio values, postMessage payload fields) — that's fine; the rule is only about user-visible text.

Each sidebar model-picker card also shows two meta lines below its title: `Quant: <entry.quant_type>` (falling back to `"—"` for a `custom_*` entry, which never has one — see above) and `Context: <resolved size>`. The context figure is **not** `entry.context_window` verbatim — it's resolved the same way `resolve_context_window` resolves it server-side (§4.6), just computed client-side against the card's *currently selected* flavor: `resolveContextSize(entry, activeFlavor)` in `llm-registry-types.ts` calls `flavorContextSize(activeFlavor)` (mirroring `LlamaFlavor.get_context_size()` — scans that flavor's own `llama_args` for `--ctx-size`/`-c`) and falls back to `entry.context_window` when that's absent or `0` (including every built-in flavor's default `--ctx-size: "0"` "use the GGUF's own trained length" sentinel). Recomputed whenever the card's flavor `<select>` changes, so switching flavors updates the Context line without a server round trip. `sidebar-provider.ts`'s webview script can't import that TS module directly (it's a plain string-embedded `<script>`, not a bundled module — see §4.4's `_local_registry_payload` note), so it carries its own inline JS copy of the same two functions; keep them in sync by hand if either side's resolution rule changes.

### 4.5 Thinking-tier families

Some `base_llm` families support a controllable "thinking budget" — how much
of the model's reasoning/`<think>` output llama-server is allowed to produce
before it must answer. Two mechanisms exist, keyed off `base_llm` (never
`entry.name`, so every quant of a base model shares one setting):

- **`qwen_reasoning_budget`** (6 tiers: `minimal`, `low`, `medium`, `high`,
  `huge`, `unlimited`) — `Qwen36-27B`, `Qwen36-35B-A3B`, `Qwen35-9B`,
  `Gemma4-26B-A4B`, `Gemma4-31B`, `Ornith10-35B`
  (`QWEN_REASONING_BUDGET_FAMILY` in `kodo/llms/_local_registry.py`; notably
  **not** `Qwen3-Coder-Next-80B`, which despite the name shares no thinking
  mechanism with the rest of the Qwen lineup — it has no thinking family at
  all, same as any `custom_*` registry entry).
  `ensure_llama_running` (`kodo/llms/llamacpp/_manager.py`) launches these
  with `--reasoning-budget -1 --reasoning-budget-message "<REASONING_BUDGET_MESSAGE>"`.
  The CLI value must be exactly `-1` — llama.cpp only honors a per-request
  override when the launch-time budget is unrestricted; any other explicit
  CLI value locks the budget and per-request overrides are silently ignored.
  Both flags are **force-assigned**, never merely defaulted: no flavor may
  set either one itself (`RESERVED_REASONING_CAP_ARGS`, §4.6) — `add_flavor`/
  `update_flavor` silently strip them from user-supplied `llama_args` before
  a flavor is ever persisted, and `ensure_llama_running` re-asserts the
  correct values at launch regardless, as a second line of defense.
  Each chat request then sets the effective budget via a **top-level**
  `thinking_budget_tokens` field (`0` immediate end / `N>0` token budget —
  see `QWEN_TIER_TOKEN_BUDGETS` for the per-tier `N`, including `unlimited`,
  which despite the name is a real finite cap now — 1.5x the `huge` tier, not
  the `-1`/no-limit sentinel it used to be). Default tier is `unlimited`.
  `Qwen35-9B` additionally needs
  `chat_template_kwargs: {"enable_thinking": true}` on every request, since
  its chat template has thinking off by default (the other five family
  members think by default). Per-request `max_tokens` is no longer a flat
  constant either: `_build_thinking_extra_body` (`_llama.py`) sizes it as the
  resolved tier's budget plus a fixed 8192-token headroom
  (`_QWEN_MAX_TOKENS_HEADROOM`), so the model always has room left, even at
  the tier's full budget, for llama.cpp to print
  `--reasoning-budget-message` and still answer — a truly unbounded
  `unlimited` tier (or any tier whose budget reached the old flat cap, as
  `high` already did) left no such room, and the exhaustion message could
  never print at all. See doc/LOCAL_INFERENCE.md §2a for the full mechanism.
- **`gpt_oss_reasoning_effort`** (3 tiers: `low`, `medium`, `high`) —
  `GPT-OSS-120B`, `GPT-OSS-20B` (`GPT_OSS_REASONING_EFFORT_FAMILY`). No
  launch-time flags. Each request sets a **nested**
  `chat_template_kwargs: {"reasoning_effort": "<tier>"}` — not a top-level
  field. Default tier is `medium` (the model's own native default).

`kodo.llms.local_thinking_family(base_llm)` /
`local_thinking_tiers(base_llm)` / `local_thinking_default_tier(base_llm)`
(all in `_local_registry.py`) are the single source of truth for both the
launch-time flag injection and the per-request field construction — adding a
model to a family is a one-line change to the relevant `frozenset`, never a
per-quant `llama_args` edit.

The **current selection** is **not** a settings.json key — unlike
`models.local`/`models.cloud`, thinking level is a **per-session** value
(`SessionState.thinking_level`, doc/SESSIONS.md), tracked by the engine and
set via `thinking_level.set` (WS_PROTOCOL.md §7.4e) or seeded automatically
per session (new-session family default, or an explicit `hello` seed —
WS_PROTOCOL.md §4.1). The **available families/tiers**, being registry data
the server already owns, are pushed to kodo-vsix via
`_local_registry_payload()`'s `thinking_families` key — `base_llm ->
{family, tiers, default}` — on every `hello.ack` and
`local_llm.registry_state` event, so kodo-vsix never needs a second
hardcoded copy of family membership, and can compute the next tier to
request when the user clicks the thinking-level control. `LlamaPlugin.
__raw_stream` (`kodo/llms/llamacpp/_llama.py`) resolves the active request's
`base_llm` from the registry and calls `_build_thinking_extra_body(base_llm,
override_tier=thinking_level)`, where `thinking_level` is the caller-supplied
tier for this call — the engine passes the session's `thinking_level` on
every ordinary turn, and the validator's `llm.complete` command passes its
own per-call override — falling back to the family default when absent or
invalid for `base_llm`. Entries with no thinking family (`base_llm == ""`,
or a hardcoded model outside both families) get no `extra_body` at all — no
behavior change.

### 4.6 Flavors

A **flavor** is a named launch configuration for one local registry entry —
and, since `LocalLLMEntry` carries no launch args of its own, the **only**
source of them. Every entry that runs through llama-server has at least one:
a `hardcoded_hf` entry ships a built-in `"default"` flavor via
`LlamaFlavor.default_flavours_field` (the dataclass field's default factory)
unless it explicitly declares a different `flavors=` literal (e.g. the F16
GGUFs use `make_default_kv_fp16` instead, for their KV cache type); a
`custom_hf`/`custom_file` entry gets its `"default"` flavor **seeded** (as a
regular *custom* flavor, not baked into Python source) from its own "Add
local LLM" form the moment it's created (`_seed_default_flavor`,
`kodo/server/_app.py`) — see §4 above. Beyond that one, flavors are the
mechanism behind two more use cases: extended-context variants (e.g. a "1M
Context" flavor using YaRN rope-scaling on a Qwen quant whose default
`context_window` is 262144) and VRAM-fit variants (GPU-offload flags like
`--n-cpu-moe`/`--override-tensor`/`--tensor-split` tuned for a specific card,
for the large models that "don't fit on GPU VRAM" as-is). Unlike thinking
level (§4.5, session-scoped, applied per-request), a flavor changes actual
llama-server **launch** arguments, so it is a **global** concept — one active
flavor per entry, shared by every open session/window, exactly like which
local model is active in the first place (`models.local`). llama-server is a
machine-wide singleton process (`kodo/server/_app.py` module docstring), so
there is no way for two sessions to run the same entry with two different
flavors at once.

```python
@dataclass(frozen=True)
class LlamaFlavor:
    id: str                                    # slug, unique per entry (predefined + custom)
    name: str                                   # display name
    description: str = ""
    llama_args: dict[str, str] = field(default_factory=dict)  # the complete CLI flag set
```

`llama_args` is the **complete** set of CLI flags passed to `llama-server`
while this flavor is active — not "extras" layered on top of some other
default, since there is no other default any more. `LlamaServerConfig`
(`kodo/llms/llamacpp/_llama_server.py`) only carries server-management fields
(executable, model path, host, port, log paths); `LlamaServer.__build_command`
appends whatever `llama_args` it was constructed with, verbatim, with nothing
else merged in — including `--jinja`, which used to be unconditionally
appended regardless of any flavor and now has to be part of a flavor's own
`llama_args` like everything else (`make_default_kv_q8`/`make_default_kv_fp16`
both include it).

**Full replace, not merge**: switching the active flavor **fully replaces**
the previously-active flavor's `llama_args` — two flavors' args are never
merged together, so a flavor that wants another flavor's
`--cache-type-k`/`--cache-type-v` (or anything else) must repeat them itself.

**Two exceptions to "the complete set":** `RESERVED_REASONING_CAP_ARGS`
(`_local_registry.py`) — `--reasoning-budget` and `--reasoning-budget-message`
— are the one pair of flags no flavor may ever set, regardless of family.
`add_flavor`/`update_flavor` silently strip either key from user-supplied
`llama_args` before a flavor is persisted (logging a warning when they
actually drop something), and `ensure_llama_running` force-assigns the
correct values again at launch time regardless — belt-and-suspenders against
a flavor saved before this restriction existed. These two are the per-session
reasoning-budget mechanism's launch-time half (§4.5); letting a flavor set
them would silently lock out every session's `thinking_level`, or (worse)
suppress the exhaustion message that tells the model — and the user — that
its thinking got cut off. See doc/LOCAL_INFERENCE.md §2a.

There is no separate `context_window` field on `LlamaFlavor` any more —
`resolve_context_window(entry, flavor) -> int` (`kodo/llms/_local_registry.py`)
deduces the effective context size from *flavor*'s own launch args instead:
its `--ctx-size` value (checked first) or `-c` value, if either parses to a
positive integer; otherwise falls back to `entry.context_window`. This is
why the built-in default flavor's `--ctx-size 0` (telling llama.cpp to read
the GGUF's own trained context length) resolves to the entry's nominal
`context_window` for budgeting purposes rather than `0`. `resolve_effective_llama_config(kodo_dir, entry)
-> (llama_args, context_window)` (`kodo/llms/_local_registry.py`) is the
single place both resolutions are combined:

1. The flavor resolved by `get_effective_flavor_id(kodo_dir, entry)` — the
   active flavor (`get_active_flavor`) if set and still present, otherwise
   (unset, or a stale id whose definition was since removed — "Default" in
   the UI) the first available flavor from `get_flavors` (predefined slots
   first) — the entry's built-in `"default"` for `hardcoded_hf`, or the
   oldest custom flavor for a `custom_*` entry (typically the one seeded
   when it was added).
2. If *entry* has no flavors at all (only reachable for a `custom_*` entry
   whose sole flavor was since removed, or a `custom_server_url` entry,
   which never actually launches this way), `({}, entry.context_window)`.
3. Otherwise, `(flavor.llama_args, resolve_context_window(entry, flavor))`.

`ensure_llama_running` (`kodo/llms/llamacpp/_manager.py`) and
`get_context_window` (`kodo/llms/_context.py`, used by auto-compaction
budgeting) both call `resolve_effective_llama_config` instead of reading
anything off `entry` directly — `ensure_llama_running` passes the resolved
`llama_args` to `LlamaServer` as a constructor argument (not a
`LlamaServerConfig` field, since it varies per launch while the rest of that
config doesn't) and ignores the resolved `context_window` entirely (only
`get_context_window` needs it). It also resolves `get_effective_flavor_id`
and passes that too (see below), purely for crash messaging.

**Startup-crash diagnostics.** A bad custom flavor (typically a malformed or
unsupported CLI flag in its `llama_args`) is the most likely way
`LlamaServer.start` fails, so `start` redirects the child's stdout/stderr
into a per-launch startup-log file (`~/.kodo/logs/llama-server-startup.log`,
truncated on every call — separate from llama-server's own `--log-file`,
which only starts recording once its logger initializes and so misses an
early CLI-parse failure entirely). If the process exits before the health
check passes, `LlamaServer.__wait_ready` folds the tail of that file (last
4000 chars) into the raised `RuntimeError`, and — if `ensure_llama_running`
passed a `flavor_id` that isn't `"default"` — appends a nudge to try the
default flavor instead. Both are message-only: no new wire fields, and
nothing changes for a process that starts successfully.

**Two flavor sources**, merged by `get_flavors(kodo_dir, entry) ->
tuple[LlamaFlavor, ...]` (predefined slots first, then any extra custom
flavors):

- **Predefined** — `LocalLLMEntry.flavors`, a tuple literal baked into
  `_HARDCODED_LOCAL_MODELS` alongside the entry itself. `hardcoded_hf` only —
  `add_local_entry` forces this to `()` for every `custom_*` kind regardless
  of what's passed in, since a caller-supplied non-empty value would
  otherwise silently shadow a same-id custom flavor added later. Every
  hardcoded entry currently gets the single built-in `"default"` flavor (q8
  or, for the two F16 GGUFs, fp16 KV cache); populating *real* predefined
  variants (1M-context / VRAM-tight per model) beyond that one is tracked
  separately from the mechanism built here.
- **Custom** (user-added, via any local LLM entry — hardcoded or custom kind,
  **except** `custom_server_url`, which isn't a process kodo launches) —
  stored in two more sibling top-level keys of
  `~/.kodo/etc/local-llm-registry.json` (see the JSON shape in §4), keyed by
  *entry name*, not entry kind:
  - `flavors: {entry_name: [flavor...]}` — custom flavor definitions. A
    `custom_hf`/`custom_file` entry's first entry here is the one seeded at
    creation time (§4); a `hardcoded_hf` entry only appears here once the
    user adds one, or edits an existing one, via "Manage flavors".
  - `active_flavors: {entry_name: flavor_id}` — the active flavor per entry;
    an entry absent from this map (or the empty string) means "unset" —
    resolved per the fallback rule above, not itself a distinct
    `LlamaFlavor` object.

  A custom flavor whose `id` matches a predefined one would be an
  **override** — `get_flavors` uses its definition in place of the
  predefined one, at the same list position, rather than dropping it. This
  merge is kept purely for resilience against a same-id override written by
  an older kodo version, before predefined flavors became read-only (below)
  — nothing in the current public API can create a new one.

  **Predefined flavors are strictly read-only.** `update_flavor` rejects
  `flavor_id` outright if it names one of the entry's predefined flavors
  (checked against `entry.flavors`, the hardcoded tuple — the same source
  `remove_flavor` already checked); there is no override mechanism any
  more. Anyone who wants a predefined flavor's config with different
  values — a different `--n-gpu-layers`, a different `min_ram`/`min_vram`,
  etc. — copies its `name`/`llama_args` into a brand-new custom flavor via
  `add_flavor` and edits the copy; the predefined literal itself, and its
  effective definition, can never be mutated in place.

  `add_flavor(kodo_dir, entry_name, name, ...)` always creates a **new**
  flavor slot — it auto-generates `id` by slugifying `name` and
  de-duplicating against every flavor (predefined or custom) the entry
  already has (`my-flavor`, `my-flavor-2`, ...), so it can never collide
  with (and therefore never overrides) an existing id. Both `add_flavor` and
  `update_flavor` also reject a *name* that exact-matches (case-sensitive,
  after trimming) another flavor `get_flavors` already returns for that
  entry — two flavors of the same entry can share a slugified `id` prefix
  (different names, e.g. "Tight VRAM" vs "tight vram"), but never the exact
  same display name; `update_flavor` excludes the flavor being edited itself
  from that check, so resubmitting a flavor under its own unchanged name
  isn't flagged as a clash with itself. `update_flavor(kodo_dir,
  entry_name, flavor_id, name, ...)` is the counterpart that overwrites an
  **existing custom** flavor's definition in place, keeping its `id` — the
  id passed in is never re-derived from `name`.
  `remove_flavor`/`update_flavor`/`set_active_flavor` reject predefined
  flavors (even a legacy overridden one, for `remove_flavor` — removing the
  override would silently revert it to the hardcoded definition, which
  isn't "removing a flavor" from the user's perspective) and unknown
  entries/ids respectively; removing the active flavor resets that entry's
  selection to unset (falls back to the first available flavor, per the
  rule above). `remove_local_entry` also cleans up a removed custom entry's
  own flavor data (both maps), since nothing else ever would.

**WS surface** (`local_llm.add_flavor` / `local_llm.update_flavor` /
`local_llm.remove_flavor` / `local_llm.set_active_flavor`, §7.6) all reply
with the same `local_llm.registry_state` event as every other `local_llm.*`
mutation — each entry in that payload's `local_registry` now carries
`flavors: [...]` (each `{id, name, description, llama_args, predefined,
min_ram, min_vram}`, §4.6a) and `active_flavor` (a flavor id, or `""` for
unset/Default), so kodo-vsix never needs a separate fetch. `predefined`
reflects whether `id` is one of `entry.flavors`' ids — it's what drives the
"Manage flavors" modal disabling both "Remove" and "Submit" for that
flavor client-side, mirroring `remove_flavor`/`update_flavor`'s own
server-side rejection.

**Restart semantics**: changing the active flavor (`set_active_flavor`), or
editing the flavor that is currently in effect (`update_flavor` — compared
via `get_effective_flavor_id`, not just the raw active-flavor selection,
since an *unset* active flavor still effectively runs the first available
one), only restarts llama-server when *entry_name* is the currently selected
local model (`models.local`) — changing an inactive entry's flavor just
persists the choice for whenever it is next selected. Even then, the
restart is forced explicitly: `ensure_llama_running` treats "already running
this entry name" as "nothing to do" (it has no way to know a flavor
changed, since flavors don't change `entry.name`), so the handlers
(`kodo/server/_app.py`, `_restart_llama_server_if_running`) stop the running
server themselves first, then call `ensure_llama_running` again to relaunch
with the freshly-resolved args — mirroring `llm.select`'s restart (§7.6a)
but without a model-name change to trigger it naturally.

**kodo-vsix UI**: flavor *selection* lives only in the sidebar (installed
entries only) — a `<select>` per local LLM card listing every flavor
`get_flavors` returns for that entry, falling back to the first flavor's id
when `active_flavor` is `""` (mirroring `resolve_effective_llama_config`'s
own fallback) and sending `set_active_flavor` immediately on change (no
separate Apply step). The Local Inference Settings panel's cards carry no
flavor dropdown at all — only a "Manage flavors" button (first in the
card's button row, before "Show me local files"/"Uninstall"/"Remove") that
opens a list-detail modal, twice the width of the panel's other modals and
split into two panes:

- **Left pane** — every flavor `get_flavors` returns for that entry (predefined
  first), each row selectable, inside a fixed-`height` (not `max-height`)
  scrollable list (`.flavor-list`, 360px) so the "Add"/"Remove" buttons below
  it stay pinned in place regardless of how many flavors exist, instead of
  drifting down the modal as rows are added; "Add" (clears the right pane to
  a blank form, deselecting) and "Remove" (deletes the selected flavor;
  disabled when nothing is selected or the selection is predefined —
  `predefined: true` in the payload, matching `remove_flavor`'s own
  server-side rejection) buttons below the list.
- **Right pane** — the selected flavor's parameters: name (with an inline
  error and a disabled "Submit" if it exact-matches another flavor of the
  same entry, mirroring the same-repo `nameTaken` check the "Add local LLM"
  modals already use for top-level entry names, client-side only; the
  server enforces the same rule independently, see above), description, a
  **multi-line** raw-text `llama_args` box — one `--flag value` per line,
  parsed server-side via `parse_llama_args_text`, unlike the single-line
  client-parsed box the "Add local LLM" modals use, see §4 — and two number
  inputs, "Minimum RAM (GB)" and "Minimum VRAM (GB)" (§4.6a's `min_ram`/
  `min_vram`; no context-window field, since that's now deduced per
  `resolve_context_window` instead of being its own input), plus "Submit"
  and "Close" buttons. "Submit" sends `add_flavor` when nothing is selected
  (the "Add" flow) or `update_flavor` with the selected flavor's id when
  editing an existing *custom* one. **Predefined flavors are read-only in
  this modal**: selecting one sets every field (`readonly`, so its text
  remains selectable/copyable — the intended way to start a new flavor from
  a predefined one's config is to copy its values into a fresh "Add") and
  disables both "Submit" and "Remove"; the client-side disable exists purely
  for UX (immediate, no round trip) — `update_flavor`/`remove_flavor` both
  reject a predefined `flavor_id` server-side regardless (see above), so
  there's no path to an inconsistent state even if the client and server's
  notion of `predefined` were ever to disagree.

### 4.6a Per-flavor hardware-fit gate (`min_ram`, `min_vram`)

`LlamaFlavor` carries two more fields beyond `llama_args`:

```python
min_ram: int = 0   # GB — system RAM (Windows/Linux), or unified memory on Mac
min_vram: int = 0  # GB — discrete GPU VRAM (Windows/Linux); always 0 on Mac
```

Unlike `LocalLLMEntry.min_memory`/`memory` (§4.4 — one **combined**
VRAM+RAM figure, rendered as an inline yellow/red warning that never blocks
anything), these are two **independent** thresholds, checked as separate
pools, and gate an actual action: selecting a flavor whose requirement
exceeds detected hardware pops a native "I understand the risk, proceed" /
"Cancel" confirmation before kodo-vsix sends `set_active_flavor` — a flavor
often pins the model fully onto one pool (e.g. `--n-gpu-layers -1`), so a
combined figure would hide a genuine single-pool shortfall the way it does
for the entry-level warning.

Both default to `0`, meaning "no known requirement — check inactive, treat
as runnable everywhere"; this is still the default for every *predefined*
flavor today (no hardcoded flavor sets real numbers yet — populating them
per model/flavor is tracked separately from the mechanism built here, same
as real predefined flavor variants in §4.6). If **either** is non-zero the
check is active.

**Authoring convention** — since Apple Silicon has one unified memory pool
(§4.3), a flavor expresses its target platform by *which* field is
non-zero:

- `min_vram = 0`, `min_ram > 0` — a Mac/unified-memory flavor; `min_ram` is
  the unified-memory requirement.
- `min_vram > 0`, `min_ram >= 0` — a Windows/Linux discrete-GPU flavor;
  `min_vram` is the VRAM requirement, `min_ram` an optional additional
  system-RAM requirement (e.g. for CPU-offloaded MoE experts).

**Wire shape**: `_flavors_payload` (`kodo/server/_app.py`) adds `min_ram`/
`min_vram` to each flavor object in `local_registry[].flavors[]` (alongside
`id`/`name`/`description`/`llama_args`/`predefined`, §4.6's WS surface) —
mirrored in kodo-vsix's `LlamaFlavorInfo` (`src/llm-registry-types.ts`).
`_flavor_to_json`/`_flavor_from_json` round-trip both fields through
`~/.kodo/etc/local-llm-registry.json` for a custom flavor.

**Editable via the "Manage flavors" modal** (§4.6) — `add_flavor`/
`update_flavor` both take `min_ram`/`min_vram` keyword params (WS payload
fields `min_ram`/`min_vram`, parsed server-side by `_parse_non_negative_int`,
defaulting to `0` when absent), matching two number inputs in the modal's
right pane. Since predefined flavors are read-only (§4.6), only a *custom*
flavor's thresholds can ever be set this way — a predefined flavor's
`min_ram`/`min_vram` stay at whatever its hardcoded Python literal declares
(`0` for every one today) unless/until real values are added there directly.
Unlike the old (removed) carry-forward behavior, `update_flavor` does *not*
preserve the previous values when the caller omits `min_ram`/`min_vram` —
the modal always resends its own fields' current contents, so this only
matters for a direct (non-UI) caller.

**The check** — `hardwareFitWarningForFlavor(flavor, detectedVramGb,
detectedRamGb, isMac)` (`src/llm-registry-types.ts`) returns `null` (no
gate) or a message with real detected numbers for the confirmation dialog's
body:

1. Inactive (`min_ram <= 0 && min_vram <= 0`) → `null`.
2. On Mac, `detected_ram_gb` is always `null` (§4.3 — `detected_vram_gb`
   already reports the full unified pool there), so the "detected RAM"
   figure used for comparison is `isMac ? detectedVramGb : detectedRamGb` —
   otherwise a Mac flavor's `min_ram` could never be checked against
   anything. `min_vram` always compares against `detectedVramGb` on both
   platforms.
3. If *both* detected figures are `null` (nothing could be detected at all)
   → `null` — skip rather than gate on a guess. Otherwise a `null` figure is
   treated as `0`.
4. If detected < required on either pool → a message naming every non-zero
   requirement/detected pair (not just the short one), so "VRAM and/or RAM"
   is always answered with concrete numbers.

**The gate** lives entirely in the extension host, not the webview:
`_setActiveFlavor(name, flavorId)` (`src/extension.ts`) looks up the flavor
from `localRegistryState`, runs the check, and — only if it returns a
warning — awaits `vscode.window.showWarningMessage(warning, {modal: true},
'I understand the risk, proceed')` before forwarding `set_active_flavor` to
kodo over WS. This is a client-side UX gate only — kodo's
`set_active_flavor` WS handler performs no hardware check of its own, so a
proceed-anyway selection reaches the server exactly like any other. If the
user cancels/dismisses, the request is never sent and `sidebarProvider`
re-pushes its (unchanged) state, which resets the sidebar's flavor
`<select>` back to the real active flavor on the next render (the sidebar
is the only live flavor-selection surface, per §4.6's kodo-vsix UI note —
"Manage flavors" is CRUD-only, not a selection action, so it isn't gated).

---

## 5. Settings schema

`~/.kodo/etc/settings.json` (`kodo/server/_config.py`'s `_DEFAULT_USER_SETTINGS`):

```json
{
  "mode": "cloud",
  "active_cloud_vendor": "anthropic",
  "models": {
    "local": "llamacpp-qwen36-27b-q4-k-xl",
    "cloud": {
      "anthropic": { "low": "claude-haiku-4-5-20251001", "medium": "claude-sonnet-5",
                      "high": "claude-opus-4-8", "max": "claude-fable-5" }
    }
  }
}
```

`mode` and `active_cloud_vendor` are both client-authored settings.json
writes followed by `config.reload` (§7.5) — same pattern as the pre-existing
`set_mode`/`set_active_model` sidebar wiring, no dedicated WS message. Same
for each of the four effort-panel selections in Cloud AI Settings: the
extension writes `models.cloud.<vendor>.<effort>` directly and sends
`config.reload`. Thinking level (§4.5) is **not** in this file — it is a
per-session value tracked by the engine, not a global setting keyed by
`base_llm` (doc/SESSIONS.md). This file has no per-workspace layering (a
single global file) and no migration path from the old 3-tier/flat schema —
an incompatible or missing file simply falls back to
`_DEFAULT_USER_SETTINGS`.

---

## 6. Cloud API key management (kodo-vsix only)

Named, multi-key, per-vendor credential management, owned **entirely by the
extension** — the Python server's `api_key.request`/`api_key.revoke` pull
protocol (WS_PROTOCOL.md §6.3/§6.4) is unchanged; it never sees key names,
UUIDs, or how many keys exist, only the resolved secret.

- `~/.kodo/etc/cloud_settings.json` (kodo-vsix-owned): a per-vendor map of
  user-chosen friendly names to VS Code SecretStorage keys (UUIDs), plus
  which one is active:
  ```json
  { "anthropic": { "keys": { "work key": "3fa8...uuid", "personal": "9c21...uuid" },
                    "active": "3fa8...uuid" } }
  ```
- The actual secret lives in VS Code `SecretStorage`, keyed by the UUID (not
  by vendor, unlike the pre-overhaul single-secret-per-vendor scheme).
- **Adding a key** (proactively, via "Add new API access key" in Cloud AI
  Settings, or reactively the first time a vendor has no keys configured):
  prompt for a friendly name, generate a UUID, prompt for the secret, store
  the secret under the UUID in SecretStorage, record `{name: uuid}` in
  `cloud_settings.json`, mark it active.
- **Forgetting a key** ("Forget this key", gated by a yes/no confirm modal):
  delete the secret from SecretStorage and its entry from
  `cloud_settings.json`; if it was active, the vendor is left with no active
  key (next `api_key.request` re-triggers the reactive add flow).
- **Making a key active** ("Make active"): flips `active` in
  `cloud_settings.json`; no SecretStorage change.
- Answering `api_key.request {vendor}`: look up `active` for that vendor,
  `SecretStorage.get(uuid)`; if none configured, fall back to the reactive
  add flow above (preserves the original "ask when nothing is configured
  yet" behavior while adding proactive management on top).
- Answering `api_key.revoke {vendor}`: forget whichever key is currently
  active for that vendor.

Only one key per vendor may be active at a time; only Anthropic is wired up
today (single-vendor cloud registry, §3), but the shape is per-vendor from
the start.
