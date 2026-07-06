# LLM Registry — Cloud/Local Split, Effort Levels, Local-Model Management

> Where models come from, how a sub-agent's `capability` turns into an actual
> model, the four local-registry entry kinds, the llama-server binary
> override, and named multi-key cloud credential management.

Companion to [LLM_GATEWAY.md](LLM_GATEWAY.md) (request scheduling/feeds, once
a plugin is resolved) and [WS_PROTOCOL.md](WS_PROTOCOL.md) (the wire shapes
referenced throughout — §4.1, §5.12/§5.12a, §6.3/§6.4, §7.5/§7.6).

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
```

`kodo/llms/_cloud_registry.py` holds one hardcoded tuple of entries per
vendor (`_ANTHROPIC_MODELS`, ...), aggregated into `_CLOUD_REGISTRY: dict[str,
tuple[CloudLLMEntry, ...]]` keyed by a lowercase vendor slug (`"anthropic"`).
A separate `_CLOUD_VENDOR_MODULE` dict maps that same vendor slug to the
dotted plugin module (`"kodo.llms.anthropic"`) — one plugin class per vendor,
shared by every model from that vendor (unlike the old per-model `module`
field). `_CLOUD_VENDOR_DISPLAY` holds the human-readable name shown in the UI
("Anthropic").

Today's Anthropic entries (`claude-opus-4-8`/`4-7`/`4-6`, `claude-sonnet-5`,
`claude-sonnet-4-6`, `claude-haiku-4-5-20251001`, `claude-fable-5`) — seven
models, defaulted one per effort tier (low→haiku, medium→sonnet-5,
high→opus-4-8, max→fable-5) but all seven selectable in any of the four
effort panels. Pricing (`kodo/llms/anthropic/_usage.py`) is keyed by
version-agnostic family prefix (`claude-opus`, `claude-sonnet`,
`claude-haiku`, `claude-fable`) so a new version of an existing family is
priced correctly without a pricing-table change; Fable is priced at
Opus-tier rates as the max-effort tier.

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
    llama_args: dict[str, str] = field(default_factory=dict)  # hardcoded_hf / custom_hf
    context_window: int = 0
    path: str = ""          # custom_file
    url: str = ""           # custom_server_url
```

Four entry kinds:

| kind | added via | installed-state rule | install/uninstall? |
|---|---|---|---|
| `hardcoded_hf` | compiled-in (`_HARDCODED_LOCAL_MODELS`) | present in `~/.kodo/etc/local-llm-index.json` | yes |
| `custom_hf` | "Add local LLM from huggingface.com" | same as `hardcoded_hf` | yes |
| `custom_file` | "Add local LLM from file" | file exists at `entry.path` | no — see below |
| `custom_server_url` | "Add a link to local llama-server" | always installed | no |

`kodo/llms/_local_registry.py` owns `get_local_registry(kodo_dir)`, which
merges the compiled-in tuple with the external collection persisted at
`~/.kodo/etc/local-llm-registry.json`:

```json
{
  "entries": [
    { "name": "...", "kind": "custom_hf", "repo_id": "...", "filename": "...", "description": "..." },
    { "name": "...", "kind": "custom_file", "path": "/abs/path/model.gguf", "description": "..." },
    { "name": "...", "kind": "custom_server_url", "url": "http://host:port", "description": "..." }
  ],
  "llama_server_override_path": null
}
```

This file is **owned entirely by the Python server** (read and written by
`kodo/llms/_local_registry.py`); kodo-vsix never writes it directly, only
through the `local_llm.*` WS commands (§7.6). `add_local_entry`/
`remove_local_entry` reject duplicate names and reject removing a
`hardcoded_hf` entry.

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

### 4.1 Install / uninstall

- **Install** (`local_llm.install {name}`, `hardcoded_hf`/`custom_hf` only) —
  unchanged download mechanics: `kodo.llms.llamacpp.download_model` calls
  `huggingface_hub.hf_hub_download`, recording the resolved path in
  `~/.kodo/etc/local-llm-index.json` keyed by entry name.
- **Uninstall** (`local_llm.uninstall {name}`) — **new**:
  `kodo.llms.llamacpp.delete_model` evicts the cached HF blob via
  `huggingface_hub`'s cache-scan/delete-revision API (a plain file delete
  would leave the bytes in HF's dedup cache) and drops the index entry. A
  no-op if not installed.
- **Remove** (`local_llm.remove {name}`) — deregisters a custom entry from
  `local-llm-registry.json`; if it's currently installed, uninstalls first
  to avoid an orphaned GGUF. Rejected for `hardcoded_hf` entries.

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
