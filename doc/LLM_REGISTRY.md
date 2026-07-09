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
    llama_args: dict[str, str] = field(default_factory=dict)  # any llama-server kind
    context_window: int = 0                                    # any llama-server kind
    path: str = ""          # custom_file
    url: str = ""           # custom_server_url
    base_llm: str = ""      # hardcoded_hf only — e.g. "qwen36-27b"
    quant_author: str = ""  # hardcoded_hf only — e.g. "Unsloth"
    quant_type: str = ""    # hardcoded_hf only — e.g. "UD_Q4_K_XL"
    size_hint: str = ""     # hardcoded_hf only — e.g. "28.6 GB"
    gpu_tip: str = ""       # hardcoded_hf only — e.g. "~43GB VRAM at 128K
                             # context — fits a single 48GB GPU (e.g. RTX
                             # 6000 Ada/A6000, or RTX PRO 5000 Blackwell)."
    mac_tip: str = ""       # hardcoded_hf only — e.g. "Needs ~43GB —
                             # comfortable on a 64GB MacBook Pro (M4 Pro/Max
                             # or M5 Pro/Max); a 48GB config is tight."
    min_memory: int = 0     # hardcoded_hf only — absolute minimum VRAM (GB); 0 = unknown
    memory: int = 0         # hardcoded_hf only — recommended VRAM (GB); 0 = unknown
```

`base_llm`/`quant_author`/`quant_type`/`size_hint`/`gpu_tip`/`mac_tip`/
`min_memory`/`memory` are metadata-only (never read by `ensure_llama_running`
or the WS handlers) — they identify, respectively, the original unquantized
model, who produced the quant, the quant spec, the GGUF file's on-disk size
(as displayed on the model's HuggingFace file listing, hand-copied — not
fetched at runtime), a hand-written discrete-GPU recommendation, a
hand-written MacBook Pro (Apple Silicon unified-memory) recommendation, and
two hand-picked VRAM thresholds (GB) used for the client-side hardware
comparison below, for every compiled-in `hardcoded_hf` entry in
`_HARDCODED_LOCAL_MODELS`. `gpu_tip` and `mac_tip` are both rough estimates
off the same underlying VRAM figure — weight size (`size_hint`) plus an
approximated KV-cache footprint at 128K context (scaled from each model
family's known/assumed architecture: layer count, attention-head config, and
the KV cache quantization each entry's `llama_args` requests). `gpu_tip`
rounds that figure to a practical discrete-GPU VRAM tier (including
current-gen RTX PRO Blackwell workstation cards: 4000/24GB, 4500/32GB,
5000/48GB, 5000 72GB/72GB, 6000/96GB); `mac_tip` maps the same figure onto
MacBook Pro unified-memory tiers (M4/M4 Pro/M4 Max and M5/M5 Pro/M5 Max
configs) with extra headroom built in for macOS's own memory overhead.
Neither is a precise sizing tool. `min_memory`/`memory` are the same
underlying estimate expressed as two plain integers instead of prose — see
§4.4 for how kodo-vsix compares them against `detected_vram_gb`. All eight
fields are always `""`/`0` for `custom_hf`/`custom_file`/`custom_server_url`
— none of the `local_llm.add_*` WS commands accept them, so a user-added
entry can never populate them. Unlike `llama_args`/`context_window` (dataclass-
only, never sent to kodo-vsix), all eight of these **are** included in
`_local_registry_payload()`'s wire shape (§4.4).

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
      "llama_args": {"--cache-type-k": "q8_0"}, "context_window": 262144 },
    { "name": "...", "kind": "custom_file", "path": "/abs/path/model.gguf", "description": "...",
      "llama_args": {}, "context_window": 262144 },
    { "name": "...", "kind": "custom_server_url", "url": "http://host:port", "description": "..." }
  ],
  "llama_server_override_path": null
}
```

This file is **owned entirely by the Python server** (read and written by
`kodo/llms/_local_registry.py`); kodo-vsix never writes it directly, only
through the `local_llm.*` WS commands (§7.6). `add_local_entry`/
`remove_local_entry` reject duplicate names and reject removing a
`hardcoded_hf` entry. `llama_args`/`context_window` are optional on the
`local_llm.add_huggingface`/`local_llm.add_file` commands (never offered for
`add_server_url`, which isn't a llama-server process kodo launches) — the
kodo-vsix "Add local LLM" modals collect `llama_args` as one space-separated
`--flag value` line and parse it client-side into the wire dict shape;
`context_window` defaults to `262144` in those modals but falls back
server-side to `get_context_window`'s default when zero/absent.

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

### 4.3 Hardware detection (`detected_vram_gb`)

`kodo/llms/_hardware.py`'s `detect_vram_gb()` is best-effort local GPU/
unified-memory detection, computed fresh on every `hello.ack` **and** every
`local_llm.registry_state` event (both go through `_local_registry_payload()`
now) and sent as the top-level `detected_vram_gb` field — see WS_PROTOCOL.md
§4.1 for the wire shape.

Detection strategy, by platform:

- **macOS**: total system RAM via `psutil.virtual_memory().total`, treated
  as VRAM-equivalent — Apple Silicon shares one unified memory pool between
  CPU and GPU, so there's no separate VRAM figure to query.
- **Windows/Linux**: sum of VRAM across every NVIDIA GPU visible to the
  driver, via `pynvml` (`nvmlDeviceGetMemoryInfo(handle).total` per device).
  **AMD GPUs are not detected** — out of scope for now; an AMD-only machine
  reports `null` even with a discrete GPU present.

The raw byte total is normalized to the nearest tier in a fixed ascending
list (4, 6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128, 192, 256 GB)
— real hardware rarely reports an exact round number (e.g. a "24GB" card
shows ~23.99 GiB), so nearest-tier snapping gives a clean, stable figure.
Above the top tier (multi-GPU rigs) it rounds to the nearest 32 GB instead of
clamping. Returns `None` (→ wire `null`) if nothing could be detected: no
supported GPU, no driver, or the detection library isn't installed/importable
— every failure mode is caught and swallowed, since this must never block
the `hello` handshake.

### 4.4 kodo-vsix wire shape and the download-progress polling design

`_local_registry_payload()` (`server/_app.py`) sends every `LocalLLMEntry`
field kodo-vsix needs — `name`, `kind`, `description`, `repo_id`, `filename`,
`path`, `url`, `installed`, `installed_path`, `base_llm`, `quant_author`,
`quant_type`, `size_hint`, `gpu_tip`, `mac_tip`, `min_memory`, `memory` — plus
top-level `llama_server_override_path` and `detected_vram_gb`. `installed_path`
is new: the absolute path to the installed file(s) (`LocalModelManager.
get_model_path()` for `hardcoded_hf`/`custom_hf`, `entry.path` for
`custom_file`, `null` for `custom_server_url` or anything not installed) —
it's what "Show me local files" in the Local Inference Settings webview
reveals via VS Code's `revealFileInOS` command, entirely client-side (no
extra WS round trip).

kodo-vsix compares `detected_vram_gb` against each entry's `min_memory`/
`memory` (both GB, same units, both `0` meaning "unknown — don't warn"):
below `min_memory` is a red "won't run" warning; below `memory` (and not
already red) is a yellow "may not perform well at large contexts" warning.
When `min_memory == memory` only the red case can ever fire — meeting the
minimum already means meeting the recommendation too, so there is no
separate yellow branch to special-case.

**Download progress is not part of this payload** — see
[LOCAL_MODEL_MANAGER.md](LOCAL_MODEL_MANAGER.md) §11. kodo-vsix polls
`manager-state.json` directly off disk once a second instead, independent of
the WS connection.

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
`config.reload`. This file has no per-workspace layering (a single global
file) and no migration path from the old 3-tier/flat schema — an
incompatible or missing file simply falls back to
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
