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
- **Local registry** (`kodo/llms/local_registry/`) — hardcoded GGUFs
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

Today's Anthropic entries (`claude-fable-5`, `claude-opus-5`,
`claude-opus-4-8`/`4-7`/`4-6`, `claude-sonnet-5`, `claude-sonnet-4-6`,
`claude-haiku-4-5-20251001`) — eight models, defaulted one per effort tier
(low→haiku, medium→sonnet-5, high→opus-5, max→fable-5) but all eight
selectable in any of the four effort panels. Fable is listed first in
`_ANTHROPIC_MODELS` — deliberately
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
    context_window: int = 0  # any llama-server kind — the resolved args' own -c/--ctx-size overrides it, see §4.6
    base_llama_args: dict[str, str] = field(default_factory=lambda: dict(BASE_LLAMA_ARGS))  # the Default profile's floor — see §4.6
    knobs: tuple[LlamaKnob, ...] = SHARED_KNOBS  # the Default profile's controls — see §4.6
    knob_defaults: dict[str, str] = field(default_factory=dict)  # per-entry knob default overrides — see §4.6a
    path: str = ""          # custom_file
    url: str = ""           # custom_server_url
    base_llm: str = ""      # hardcoded_hf only — e.g. "qwen36-27b"
    llm_author: str = ""    # hardcoded_hf only — e.g. "Alibaba Cloud"
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
    llamacpp_version: int = 0  # hardcoded_hf only in practice — minimum llama.cpp build number; 0 = any version works
```

`base_llm`/`llm_author`/`quant_author`/`quant_type`/`size_hint`/`gpu_tip`/`mac_tip`/
`min_memory`/`memory`/`llamacpp_version` are metadata-only (never read by `ensure_llama_running`
or the WS handlers) — they identify, respectively, the original unquantized
model, who produced that original model, who produced the quant, the quant spec, the GGUF file's on-disk size
(as displayed on the model's HuggingFace file listing, hand-copied — not
fetched at runtime), a hand-written discrete-GPU-plus-system-RAM
recommendation, a hand-written MacBook Pro (Apple Silicon unified-memory)
recommendation, two hand-picked combined-memory thresholds (GB) used for
the client-side hardware comparison below, and the minimum llama.cpp build
number the model needs (also compared client-side, §4.4), for every compiled-in
`hardcoded_hf` entry in `_HARDCODED_LOCAL_MODELS`. `gpu_tip` and `mac_tip`
are both rough estimates off the same underlying total-memory figure —
weight size (`size_hint`) plus an approximated KV-cache footprint at 128K
context (scaled from each model family's known/assumed architecture: layer
count, attention-head config, and the KV cache quantization each entry's
Default profile — see §4.6). `gpu_tip` deliberately does **not** round that figure
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
`llamacpp_version` is the same idea applied to the installed llama.cpp build
number instead of memory — see §4.4.
All ten
fields are always `""`/`0` for `custom_hf`/`custom_file`/`custom_server_url`
— none of the `local_llm.add_*` WS commands accept them, so a user-added
entry can never populate them. All ten of
these **are** included in
`_local_registry_payload()`'s wire shape (§4.4), alongside the raw
`context_window` field itself — added so kodo-vsix can render the sidebar's
per-card "Context:" line (§4.4) — though its *effective, config-resolved*
value is still never sent over the wire as its own field; that value is
computed twice independently instead, server-side via
`resolve_context_window` (§4.6, for auto-compaction budgeting) and
client-side in kodo-vsix (`llamaArgsContextSize`/`resolveContextSize` in
`llm-registry-types.ts`, mirroring `LlmProfile.get_context_size`/
`resolve_context_window` for display). The launch configuration **is** sent to
kodo-vsix too — predefined entries plus any custom ones merged in, see §4.6
— since it is the only source of llama-server launch args: there
is no `llama_args` field on `LocalLLMEntry` at all any more.

Four entry kinds:

| kind | added via | installed-state rule | install/uninstall? |
|---|---|---|---|
| `hardcoded_hf` | compiled-in (`_HARDCODED_LOCAL_MODELS`) | installed per `LocalModelManager` state | yes |
| `custom_hf` | "Add local LLM from huggingface.com" | same as `hardcoded_hf` | yes |
| `custom_file` | "Add local LLM from file" | file exists at `entry.path` | no — see below |
| `custom_server_url` | "Add a link to local llama-server" | always installed | no |

`kodo/llms/local_registry/` owns `get_local_registry(kodo_dir)`, which
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
  "profiles": {
    "unsloth-qwen36-27b-q4-k-xl": [
      { "id": "tight-vram", "name": "Tight VRAM", "description": "8GB card",
        "llama_args": { "--ctx-size": "131072", "--n-cpu-moe": "24",
                        "--cache-type-k": "q8_0", "--cache-type-v": "q8_0",
                        "--jinja": "" } }
    ]
  },
  "active_profiles": {
    "unsloth-qwen36-27b-q4-k-xl": "tight-vram"
  },
  "knob_selections": {
    "unsloth-qwen36-27b-q4-k-xl": { "tail-culling": "medium" }
  }
}
```

Note that no `entries[]` object carries `llama_args` any more — a custom
entry's "Add local LLM" form args are persisted as its `base_llama_args`
instead (§4.6), which the Default profile's knobs layer over.
`profiles`/`active_profiles`/`knob_selections` are three sibling top-level
keys in this same file, unrelated to the `entries` list — see §4.6.
`knob_selections` is deliberately **sparse** (only knobs moved off their
default appear, so `tail-culling` above is the one thing this user changed);
`active_profiles` omits an entry entirely when the Default profile is
selected.

The `flavors`/`active_flavors` keys a pre-knobs kodo wrote are **not
migrated** — matching a hand-edited arg dict back to a knob selection would
have to guess. They are left untouched on disk (`_load_raw`/`_save_raw` never
drop keys they don't know about) and simply ignored: an install that had
custom flavors starts fresh on the Default profile, with the old definitions
recoverable by hand from the file.

`add_local_entry` forces `entry.knobs` to `()` before persisting — knobs are
code, re-attached on every load (§4.6), which is what lets a kodo release
change the shared knob set with no file migration.

(The "Manage profiles" modal (§4.6) uses a different, multi-line input
for the same `llama_args` shape — one flag per line, parsed **server-side**
instead — since a profile typically carries more flags than a base entry's
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
files, and per-call HF tokens. Split-GGUF downloads and per-call HF tokens
are exercised from the install/resume/update WS commands — the server
requests the active HF token from the extension before each download
(`hf_token.request`, WS_PROTOCOL.md §6.5). mmproj companion files are not
yet reachable from the UI.

A file left `DOWNLOADING` by a killed/crashed kodo-server is forced to
`PAUSED` the next time `LocalModelManager` is constructed for that models
directory (i.e. effectively "at the next kodo-server startup") — see
LOCAL_MODEL_MANAGER.md §11. The Local Inference Settings webview surfaces
this as a resumable download, same as one the user paused deliberately.

`~/.kodo/etc/local-llm-index.json` (the old flat `{name: path}` index) is
retired — superseded by `LocalModelManager`'s own `manager-state.json`,
scoped under the models directory itself rather than `etc/`.

### 4.1a Update checking and re-fetch

Every time the Local Inference Settings panel opens, kodo-vsix fires
`local_llm.check_updates {names}` (fire-and-forget, every installed
`hardcoded_hf`/`custom_hf` name) so the server can compare each model's
on-disk GGUF ETag against HuggingFace's current one
(`LocalModelManager.check_for_update` — metadata-only, no bytes downloaded)
and reply with `local_llm.updates_available {updatable}`. A non-empty result
shows a yellow banner and adds an **Update** button to each stale, installed
card. Clicking it disables the button and sends `local_llm.update {name}`,
which the server implements as a plain `uninstall` immediately followed by
the same `download_model` path `install` uses — i.e. exactly "click
Uninstall, wait, click Install," reusing those two existing manager calls
rather than a new atomic re-fetch. Full design (why ETag, why fire-and-forget,
why uninstall+reinstall instead of an in-place overwrite) is in
[LOCAL_MODEL_MANAGER.md](LOCAL_MODEL_MANAGER.md) §12.

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
`llm_author`, `llamacpp_version`,
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

kodo-vsix runs an analogous check against `llamacpp_version`: it parses the
numeric suffix off the installed llama.cpp build (`llamaCpp.installedVersion`
in webview state, a `"b<N>"` string sourced from `hello.ack`'s
`llama_version`/kept current via `llamacpp.version_info.ack`, §7.6 in
WS_PROTOCOL.md) and compares it against the entry's `llamacpp_version` (`0`
means "any version works — don't warn", same convention as `min_memory`/
`memory`). If the installed build is older, the card shows a red
"llama.cpp update required" warning — same visual treatment as the
`min_memory` red case, but a distinct check: a machine can have plenty of
RAM/VRAM for a model and still be unable to run it because the installed
llama.cpp predates support for that model's architecture/quantization.
`llm_author` carries no warning logic — it's display-only metadata (the org
that produced the base model, e.g. `"Alibaba Cloud"`).

**Pre-launch confirmation gate.** Unlike this section's older text below
might suggest, these two entry-level warnings are no longer purely
inline/non-blocking: `localLaunchWarnings(entry, detectedVramGb,
detectedRamGb, installedLlamaCppVersion, isMac)` (`src/llm-registry-types.ts`)
is a second, extension-host-importable copy of the same two rules
(duplicated, not shared, since `ramWarning`/`llamacppVersionWarning` above
live in the webview-only `settings-webview/localLlmUtils.ts` — keep both in
sync by hand) *plus* a third, platform-compatibility rule (§4.6b) that has
no webview-inline equivalent above since it isn't hardware-detection-based,
consumed by `confirmLocalLlamaLaunch(openSettings)`
(`src/extension/local-llm-registry.ts`). That gate fires before llama-server
actually launches — from the sidebar's explicit ▶ Start/↺ Restart llama.cpp
button (`startLlamaCpp`, `src/extension/llamacpp.ts`), and again from
`SessionController._submitPrompt` (`src/session/controller.ts`, via the
injected `SessionDeps.confirmLocalLaunch`) right before a local-mode prompt
send that would trigger the engine's automatic launch (i.e. local mode and
the running server, if any, isn't already serving the active model).

If the active model had a `'platform'` warning (§4.6b — none of its flavors
are compatible with this host), the gate shows a plain OK-only error
(`vscode.window.showErrorMessage`, no "Start anyway") and unconditionally
cancels — checked first, *before* consulting `dismissedLocalLaunchWarnings`
below, since a platform mismatch isn't something "don't ask again" (meant
for memory/version risk the user is willing to take) can paper over. This
is also the one warning kind kodo's own `ensure_llama_running` independently
refuses on server-side (§4.6b) — defense in depth for a caller that reaches
`llama.start`/an auto-launch without going through this gate.

Otherwise, if the active model has any outstanding memory/version warning
(red *or* yellow), a native modal lists every one and offers "Start
anyway", "Start anyway, don't ask again for this model", an implicit Cancel
(Escape/dismiss), and — only when a `llamacpp_version` warning is present —
"Update llama.cpp…", which opens Kōdo Settings' Local Inference tab
(`vscode.commands.executeCommand('kodo.openSettings', 'local-inference')`
from the session path, to dodge a circular import; a direct `openKodoSettings`
call from the sidebar-button path) instead of starting. Like §4.6a's
per-flavor gate (removed, §4.6b), the memory/version half of this is a client-side UX
gate only — nothing server-side blocks a launch on those two fields either
(unlike the platform check, which kodo also enforces itself).

**"Don't ask again."** Choosing that button calls
`dismissLocalLaunchWarnings(entry.name)` (`src/extension/settings-io.ts`),
which appends the registry entry's `name` to a new
`dismissedLocalLaunchWarnings: string[]` field in `UiSettings`
(`src/settings-panel/types.ts`), persisted in the same kodo-vsix-only
`~/.kodo/etc/ui-settings.json` file `pinnedLocalModels` lives in (§4.4's wire
shape is unaffected — this never touches the server). `confirmLocalLlamaLaunch`
checks this list *before* computing any warnings, so once a quant is on it,
every future launch attempt for that exact `name` skips the dialog entirely,
regardless of which warnings apply at that later time (a newer, different
warning does not re-arm the dialog). This is deliberately **one-way**: there
is no button, setting, or command that removes a name from the list once
added — an advanced user can only do so by hand-editing the JSON file. VS
Code's native modal dialog (`showWarningMessage`) has no checkbox widget,
so "don't ask again" is expressed as an extra button choice, not a literal
checkbox next to "Start anyway" — consistent with every other confirmation
dialog in kodo-vsix, all of which are native modals.

**Download progress is not part of this payload** — see
[LOCAL_MODEL_MANAGER.md](LOCAL_MODEL_MANAGER.md) §11. kodo-vsix polls
`manager-state.json` directly off disk once a second instead, independent of
the WS connection.

**Display convention: `name` is never shown to the user, `description` always is.** For a `hardcoded_hf` entry, `name` is an internal registry-key slug (e.g. `unsloth-qwen36-27b-q8-k-xl`); `description` is the human-readable label (e.g. "Qwen 3.6 27B UD-Q8_K_XL by Unsloth"). Every kodo-vsix surface that lists local models — the sidebar model-picker cards, the Local Inference Settings model cards, the "running: …" status line, the Configure and Manage-profiles modal titles, and download-progress rows — titles itself off `entry.description`, falling back to `entry.name` only where `description` can legitimately be empty (a `custom_*` kind entry, where `name` is whatever display text the user typed when adding it, per §4). `name` still flows through the wire/DOM as a plain identifier (dataset keys, radio values, postMessage payload fields) — that's fine; the rule is only about user-visible text.

Each sidebar model-picker card also shows two meta lines below its title: `Quant: <entry.quant_type>` (falling back to `"—"` for a `custom_*` entry, which never has one — see above) and `Context: <resolved size>`. The context figure is **not** `entry.context_window` verbatim — it's resolved the same way `resolve_context_window` resolves it server-side (§4.6), just computed client-side against the card's *currently selected* profile: `resolveContextSize(entry, args)` in `llm-registry-types.ts` calls `llamaArgsContextSize(args)` (mirroring `LlmProfile.get_context_size()` — scans for `--ctx-size`/`-c`) and falls back to `entry.context_window` when that's absent or `0` (including the base args' `--ctx-size: "0"` "use the GGUF's own trained length" sentinel). The args come from the *selected* profile: a user-defined profile's own `llama_args`, or the server-computed `default_profile_args` for the Default profile — which is why the picker recomputes the line on `change` without a server round trip. `sidebar-provider.ts`'s webview script can't import that TS module directly (it's a plain string-embedded `<script>`, not a bundled module — see §4.4's `_local_registry_payload` note), so it carries its own inline JS copy of the same functions; keep them in sync by hand if either side's resolution rule changes.

Each card also shows a ⚠ warning icon to the left of the pin/favorite star whenever `localLaunchWarnings` (this section, "Pre-launch confirmation gate" above) returns anything non-empty for that entry — red if any warning is `level: 'red'`, otherwise yellow; hovering it lists every outstanding warning's `text` (native `title` attribute, one line per warning). Unlike the Context-line functions above, this one is **not** duplicated as inline webview JS: `SidebarProvider._computeLocalWarnings` (`src/sidebar-provider.ts`) calls the real `localLaunchWarnings` on the extension-host side — it can, since it's plain TS, not a webview script — and ships the per-entry result as a new top-level `localWarnings: Record<name, LocalLaunchWarning[]>` field alongside every `update` postMessage (computed fresh from the post-merge state each time, not cached in `SidebarState`/`ui-settings.json`). The webview script just looks up `localWarnings[model.name]` and renders/skips the icon — no independent copy of the memory/version warning rules to keep in sync here, unlike the Context-line and confirm-dialog cases. There is no per-configuration compatibility filter any more (§4.6b).

### 4.5 Thinking-tier families

Some `base_llm` families support a controllable "thinking budget" — how much
of the model's reasoning/`<think>` output llama-server is allowed to produce
before it must answer. Two mechanisms exist, keyed off `base_llm` (never
`entry.name`, so every quant of a base model shares one setting):

- **`qwen_reasoning_budget`** (6 tiers: `minimal`, `low`, `medium`, `high`,
  `huge`, `unlimited`) — `Qwen36-27B`, `Qwen36-35B-A3B`, `Qwen35-9B`,
  `Gemma4-26B-A4B`, `Gemma4-31B`, `Ornith10-35B-A3B`
  (`QWEN_REASONING_BUDGET_FAMILY` in `kodo/llms/local_registry/`; notably
  **not** `Qwen3-Coder-Next-80B`, which despite the name shares no thinking
  mechanism with the rest of the Qwen lineup — it has no thinking family at
  all, same as any `custom_*` registry entry).
  `ensure_llama_running` (`kodo/llms/llamacpp/_manager.py`) launches these
  with `--reasoning-budget -1 --reasoning-budget-message "<REASONING_BUDGET_MESSAGE>"`.
  The CLI value must be exactly `-1` — llama.cpp only honors a per-request
  override when the launch-time budget is unrestricted; any other explicit
  CLI value locks the budget and per-request overrides are silently ignored.
  Both flags are **force-assigned**, never merely defaulted: no profile may
  set either one itself (`RESERVED_LLAMA_ARGS`, §4.6) — `add_profile`/
  `update_profile` strip them from user-supplied `llama_args` before
  a profile is ever persisted, and no knob writes them at all;
  `ensure_llama_running` re-asserts the
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
(all in `local_registry/`) are the single source of truth for both the
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

### 4.6 Launch configuration: knobs and profiles

A local registry entry is launched under one of exactly two things:

- Its **Default profile** — never stored as a profile at all. Its
  `llama_args` are *computed* from the entry's `base_llama_args` plus whatever
  its **knobs** currently resolve to. Every launchable entry has one, it
  cannot be deleted or renamed, and it is what an entry runs under until the
  user picks something else.
- One of zero or more **user-defined profiles** (`LlmProfile`) — raw
  `{flag: value}` arg sets the user builds in the "Manage profiles" editor.
  These have no knobs.

This replaced the older **flavor** model, where every useful combination had
to be enumerated as its own predefined `LlamaFlavor` literal in Python. The
Laguna-S-2.1 catalog is the clearest illustration of why: each of its 20
quants shipped eight flavors (`default`, five fixed sampling presets, and two
extended-context variants), 160 objects that between them still could not
express "strong tail culling *and* a low temperature", because nobody had
written that particular pair down. The same coverage is now four dropdowns.

Like the flavors it replaces — and unlike thinking level (§4.5, session-scoped
and applied per-request) — a launch configuration changes actual llama-server
**launch** arguments, so it is a **global** concept: one active configuration
per entry, shared by every open session/window, exactly like which local model
is active in the first place (`models.local`). llama-server is a machine-wide
singleton process (`kodo/server/_app.py` module docstring), so two sessions
can never run the same entry two different ways at once.

#### Knobs

A **knob** is a typed, declarative control on the Default profile that owns a
fixed set of CLI flags. Knobs are **hardcoded in `kodo.llms.local_registry`** —
there is no user-defined knob and no way to add one over the wire.

```python
class KnobKind(StrEnum):
    CHECKBOX = "checkbox"   # exactly two options, ids "off"/"on"
    DROPDOWN = "dropdown"   # two or more options
    NUMBER   = "number"     # one flag, a numeric value the user types

@dataclass(frozen=True)
class KnobOption:
    id: str                                                   # slug, unique within its knob
    name: str                                                 # display name
    description: str = ""                                     # what picking this state does
    llama_args: dict[str, str] = field(default_factory=dict)  # what it contributes

@dataclass(frozen=True)
class LlamaKnob:
    id: str                                    # GLOBAL id — deduplicated on the wire
    name: str
    description: str = ""
    kind: KnobKind = KnobKind.DROPDOWN
    advanced: bool = False                     # behind the modal's "Advanced" section
    options: tuple[KnobOption, ...] = ()       # checkbox/dropdown only
    default_option: str = ""                   # "" = the first option
    flag: str = ""                             # NUMBER only — the single flag it writes
    minimum / maximum / step: float | None     # NUMBER only, advisory
    unset_label: str = ""                      # NUMBER only — placeholder for "not set"
    default_value: str = ""                    # NUMBER only, "" = flag not emitted
```

**The load-bearing invariant: two knobs on the same entry may never own the
same CLI flag.** `validate_knobs` enforces it and `_catalog._validate_catalog`
runs it over every entry at import time, so a bad declaration is a hard
startup failure rather than a mystery at launch. It is what lets knob args be
composed with a plain `dict.update` — no precedence rules, no merge strategy,
because a collision cannot exist. The comparison is over each knob's
*reachable* flags (`knob_owned_flags`, the union across all of its options),
not the flags its current selection happens to set: a collision only some
option pairs would produce is still a collision.

The shared knobs — offered by every launchable entry, `_knobs_shared.py`:

| id | kind | options / range | flags |
|----|------|-----------------|-------|
| `kv-cache` | dropdown | `q8_0` (default), `f16` | `--cache-type-k`, `--cache-type-v` |
| `tail-culling` | dropdown | `off` (default), `minimal`, `light`, `medium`, `strong` | `--top-k`, `--top-p`, `--min-p`, `--top-nsigma` |
| `temperature` | dropdown | `default` (0.8), `low` (0.3), `near-greedy` (0.05) | `--temp` |
| `gpu-layers` | number, advanced | default `-1` | `--n-gpu-layers` |
| `cpu-moe` | number, advanced | unset by default | `--n-cpu-moe` |
| `flash-attention` | dropdown, advanced | `auto` (default), `on`, `off` | `--flash-attn` |

`kv-cache` is what replaced the `make_default_kv_q8` / `make_default_kv_fp16`
pair of predefined flavors: an F16 GGUF now just declares
`knob_defaults={"kv-cache": "f16"}` (§4.6a).

`tail-culling` and `temperature` are deliberately **two** knobs rather than
one "sampling preset" dropdown, and each holds the other's territory fixed:
every culling option leaves `--temp` alone, and the temperature option writes
nothing but `--temp`. That is the same "one axis moves at a time" rule the
five predecessor preset flavors followed by convention (doc/QUANT_SAMPLING.md
§4) — as knobs it is enforced structurally by the invariant above. Every
active culling option also pins `--top-k 0`/`--top-p 1.0` so that min-p (plus
top-n-sigma in the strongest state) is the *only* truncation stage in play;
otherwise llama.cpp's own `top_k 40`/`top_p 0.95` defaults would still be
silently cutting alongside it.

**No knob enables a repetition penalty** — not DRY, not `--repeat-penalty`,
not presence/frequency. One flavor once did, and it made `read_attachment`
fail: penalising verbatim reproduction from context is exactly what quoting
back an attachment's UUID requires. Loop handling belongs to the watchdog
(doc/STUCK_DETECTION.md §2.7/§2.10, doc/QUANT_SAMPLING.md §3f). The rule binds
what kodo *ships*, not what a user may do — a penalty is still reachable on a
user-defined profile and as a per-session override, since hiding it in one of
the two editors while offering it in the other would be arbitrary.

**Private per-model knobs.** Anything needing model knowledge is built by the
family module instead of being shared. The only ones today are the three YaRN
long-context knobs (`_knobs_context.make_yarn_context_knob`), which need the
model's architecture key and native context length:

| knob id | arch key | native | options |
|---------|----------|--------|---------|
| `context-qwen35` | `qwen35` | 262144 | native (default), 512K, 1M |
| `context-qwen35moe` | `qwen35moe` | 262144 | native (default), 512K, 1M |
| `context-laguna` | `laguna` | 8192 | native (default), 512K, 1M |

Each extended option writes `--ctx-size`, `--rope-scaling yarn`,
`--rope-scale` (target ÷ native), `--yarn-orig-ctx` and
`--override-kv <arch>.context_length=int:<size>`; the "native" option writes
nothing at all, letting the base `--ctx-size 0` stand. `arch_key` is model
knowledge — never derive it from the entry name.

#### Composition

```
Default profile args  =  entry.base_llama_args  +  knob args (knob args win)
```

`base_llama_args` defaults to `BASE_LLAMA_ARGS` — `--ctx-size 0`,
`--reasoning-format auto`, `--jinja` — which is what nearly every hardcoded
entry wants. **Base args are the floor and knob args are layered on top**;
that direction is fixed and is the only precedence rule in the system. It is
what lets a context knob's `--ctx-size 524288` override the base `0`, which is
the one place a knob legitimately owns a flag the base args also set (the
"no two knobs share a flag" invariant is knob-vs-knob only).

A `custom_hf`/`custom_file` entry contributes the launch args typed into its
"Add local LLM" form as its `base_llama_args`, merged *over* `BASE_LLAMA_ARGS`
so it still gets `--jinja` (without which tool calling does not work at all)
unless the form deliberately overrode it. It gets `SHARED_KNOBS` and no
private ones, so its Configure modal works exactly like a built-in LLM's.
`custom_server_url` gets neither knobs nor base args — kodo does not launch
that process.

**Knobs are code, never stored data.** `add_local_entry` forces `knobs=()`
before persisting a custom entry and `_with_custom_entry_knobs` re-attaches
them on every load. That is what lets a kodo release add or change a shared
knob and have it reach every existing custom entry with no file migration.

#### Selections

What the user picked is a flat `{knob_id: selection}` map per entry
(`knob_selections` in `local-llm-registry.json`). The string means an option id
for a checkbox/dropdown knob, or the value as text for a NUMBER knob — where
`""` means "don't emit the flag at all", which is **not** a synonym for zero.

Stored **sparsely**: `set_knobs` drops any selection equal to the knob's
currently resolved default rather than writing it. That is what lets a later
kodo release change a knob's default (or an entry's `knob_defaults`) and have
it take effect for everyone who never deliberately moved that knob, while
still respecting the choice of everyone who did.

Read back **resolved**: `get_knob_selections` returns one entry per knob,
never sparse, with a stored selection naming an option the knob no longer has
replaced by the resolved default (and logged) — so the UI can bind a
`<select>` straight to it and can never render a control with no matching
option.

#### User-defined profiles

```python
@dataclass(frozen=True)
class LlmProfile:
    id: str                                    # slug, unique per entry; "" is reserved
    name: str                                   # display name
    description: str = ""
    llama_args: dict[str, str] = field(default_factory=dict)  # the complete CLI flag set
```

Every profile is user-defined — there is no `predefined` flag, no `platform`,
and no `min_ram`/`min_vram`, because everything that used to be a predefined
flavor is a knob now and per-configuration hardware/platform gating was
removed with the flavor model (see §4.6a/§4.6b below). `update_profile`
therefore has no read-only case to reject.

**Full replace, not merge**: selecting a profile **fully replaces** the
Default profile's args. The two are never combined, so a profile that wants
the Default profile's KV-cache flags must repeat them — which is why the
"Manage profiles" editor seeds a new profile from
`entry.default_profile_args` rather than from nothing.

`active_profiles[entry_name]` holds the selected profile's id, with `""` (or
an absent key) meaning the Default profile. A **stale** id — a profile removed
since it was selected — resolves back to `""`, not to some other profile:
falling through to an arbitrary neighbour is how the flavor model used to
surprise people, and there is now always a real, always-valid configuration to
fall back to.

#### Resolution

`resolve_effective_llama_config(kodo_dir, entry) -> (llama_args, context_window)`
is the single entry point:

- the active user-defined profile's args verbatim, if one is selected;
- otherwise `resolve_default_profile_args` (base + knobs);
- `({}, entry.context_window)` for `custom_server_url`.

`context_window` comes from `resolve_context_window`, which reads the resolved
args' own `--ctx-size`/`-c` when positive and falls back to the entry's
`context_window` otherwise (including the `--ctx-size 0` "use the GGUF's own
trained length" sentinel). `LlamaServerConfig` carries only server-management
fields (executable, model path, host, port, log paths);
`LlamaServer.__build_command` appends the resolved args verbatim with nothing
merged in.

**Reserved args.** `RESERVED_LLAMA_ARGS` (`local_registry/_reserved.py`) is the
set of flags no user-defined profile may carry: the server-managed ones
(`--model`/`-m`, `--host`, `--port`, `--alias`, `--log-file`,
`--log-timestamps`), which `LlamaServerConfig` sets per launch, and
`RESERVED_REASONING_CAP_ARGS` (`--reasoning-budget`,
`--reasoning-budget-message`), which the Thinking Level control sets per
session (§4.5). `add_profile`/`update_profile` strip them before persisting
(logging what went), they are excluded from the argument catalog (§4.7), and
`ensure_llama_running` force-assigns the reasoning-cap pair anyway for a
`qwen_reasoning_budget` model — defense in depth for anything saved before
that restriction existed. The Default profile never sets them at all, since
no knob writes them.

#### Wire shape and UI

Every `hello.ack`/`local_llm.registry_state` payload carries, per entry:

- `knobs: [knob_id, ...]` — the ids this entry offers, in display order;
- `knob_selections: {knob_id: selection}` — **resolved, never sparse**;
- `default_profile_args: {flag: value}` — what those selections resolve to
  right now, so the client can show the effective context size (and the exact
  flags a knob produced) without re-implementing knob composition or making a
  round trip;
- `profiles: [{id, name, description, llama_args}]` — user-defined only;
- `active_profile` — a profile id, or `""` for the Default profile;

plus, once per payload rather than per entry:

- `knob_defs: {knob_id: {...}}` — every knob definition any entry offers,
  **deduplicated by id** (`_knob_defs_payload`). All 82 built-ins share the
  same six knobs and only the three context knobs are per-family, so repeating
  each definition (five options, each with a paragraph of help text) on every
  entry would dominate the payload. `_validate_catalog` guarantees two entries
  never disagree about what one id means, so the flattening is lossless.
- `llama_arg_catalog: [...]` — see §4.7.

Five client→server messages manage all of this (doc/WS_PROTOCOL.md §7.6):
`local_llm.add_profile`, `.update_profile`, `.remove_profile`,
`.set_active_profile`, and `.set_knobs`. All five reply with
`local_llm.registry_state`. The last two restart llama-server immediately
**only** if the entry is both the selected local model (`models.local`) and the
one currently running — reconfiguring an inactive entry just persists the
choice. `set_knobs` additionally compares the *resolved args* before and after
and skips the restart when they are unchanged, so opening Configure, changing
nothing and pressing Apply never interrupts a window mid-generation.

**kodo-vsix UI.** Three surfaces, in two places:

- The **sidebar** LLM card carries a profile picker (`Default` + each
  user-defined profile) and, whenever `Default` is selected, a **Configure**
  button. Changing the picker posts `set_active_profile` and updates the card's
  Context line immediately from the pending profile's own args — no round trip.
  Configure deep-links into Kōdo Settings (`openKodoSettings('local-inference',
  name)` → `KodoSettingsPanel.configureLocalModel`), because a modal of knobs
  with descriptions and an Advanced section does not fit a 300px sidebar built
  as a plain-JS string-embedded script.
- The **Configure modal** (`settings-webview/ConfigureModal.tsx`) renders one
  block per knob — control, the knob's description, the selected option's own
  description, and the exact flags that state produces — with the advanced
  knobs behind a collapsible section that is collapsed every time the modal
  opens. Edits are local until **Apply**, which sends the whole selection at
  once; **Cancel** discards them. Deliberately unlike the profile editor, where
  a field writes through immediately: a knob change can restart llama-server,
  so "change three knobs, restart once" has to be possible.
- The **Manage profiles modal** (`settings-webview/ProfileModal.tsx`) is the
  user-defined-profile editor — see §4.7 for its argument picker. The settings
  panel's Local LLMs card offers both buttons (Configure, Manage profiles).

### 4.6a Per-entry knob defaults (`knob_defaults`)

An entry may override a knob's own default state:

```python
LocalLLMEntry(
    name="unsloth-gpt-oss-120b-f16",
    knob_defaults={"kv-cache": "f16"},
    ...
)
```

This is how one entry starts from a different position than the shared knob's
own default, and it is what replaced the `make_default_kv_fp16` predefined
flavor. `_validate_catalog` checks at import time that every `knob_defaults`
key names a knob the entry actually offers and that its value is a real
option.

Because selections are stored sparsely (§4.6), changing a `knob_defaults`
value in a later release reaches every user who never deliberately moved that
knob — the same property that makes changing a knob's own `default_option`
safe.

### 4.6b What happened to the per-flavor hardware and platform gates

Both are **gone**. A flavor used to be able to declare `platform`
(`mac`/`gpu`/`both`) and `min_ram`/`min_vram`, which drove a sidebar
compatibility filter, an unconditional ⛔ "not compatible with this platform"
launch block, and a "proceed anyway?" hardware-fit confirmation. A knob option
carries none of that, so:

- `LlamaFlavorPlatform`, `current_host_platform`, `_flavor_compatible_with_host`,
  `has_compatible_flavor`, `get_effective_flavor_id` (Python) and
  `flavorCompatibleWithHost`, `entryHasCompatibleFlavor`,
  `hardwareFitWarningForFlavor`, `platformWarning` (TypeScript) **no longer
  exist**;
- `LocalLaunchWarning.kind` is now `'memory' | 'version'` — the `'platform'`
  variant is gone, and `localLaunchWarnings` no longer takes an `isMac`
  argument;
- the entry-level `min_memory`/`memory` combined VRAM+RAM warning (§4.4) and
  the llama.cpp-version warning are the **only** hardware checks left, applied
  at launch time by `confirmLocalLlamaLaunch`.

The practical consequence: the 512K/1M context options on Laguna and Qwen are
offered on every host, including a Windows/Linux discrete-GPU box where the KV
cache at those sizes is impractical to split across VRAM and system RAM. Each
extended option's own `description` says so, which is now the only place that
guidance lives.

### 4.7 The llama-server argument catalog

`LLAMA_ARG_CATALOG` (`kodo/llms/_arg_catalog.py`) is a hand-maintained table
of the `llama-server` flags worth exposing, shipped once per registry payload
as `llama_arg_catalog` and rendered by the "Manage profiles" editor's "Add
argument" picker.

```python
@dataclass(frozen=True)
class LlamaArgSpec:
    flag: str                  # long form, e.g. "--ctx-size" — the key written into llama_args
    label: str
    kind: Literal["str", "int", "float", "bool", "enum", "str_list"]
    category: str              # picker grouping header, e.g. "Context & memory"
    help: str
    advanced: bool = False
    minimum / maximum / step: float | None = None
    choices: tuple[str, ...] = ()      # "enum" only
    placeholder: str = ""              # "str" only
    default: str = ""                  # what llama.cpp does without the flag, as display text
    sensible_minimum / sensible_maximum: float | None = None   # sampling flags only
    valid_values: tuple[str, ...] | None = None                # "--samplers" only
```

Deliberately **curated, not exhaustive** (~60 flags across four categories:
Context & memory, GPU & performance, Model behaviour, Sampling).
`llama-server --help` lists roughly two hundred, most irrelevant to running a
coding agent, and transcribing all of them would create a table that silently
rots against every llama.cpp release. Anything not in it is still reachable —
the editor keeps a raw "one flag per line" box beside the picker.

Two things it is **not**: it is not validation (nothing here is enforced
server-side; the bounds drive input widgets and an advisory ⚠), and it is not
the knob framework (knobs are curated *combinations* chosen by kodo; this is
individual flags chosen by the user).

The sampling half is **derived** from `SAMPLING_PARAM_SPECS`
(`_sampling_arg_specs`, using each spec's `cli_flags[0]`), so the recommended
bands, the `--samplers` whitelist and the help text stay single-sourced with
the session sampling modal (doc/SAMPLING.md §8d/§8e) rather than drifting as a
second copy. `min_keep` is skipped — it has no CLI flag and is session-override
only. Every `RESERVED_LLAMA_ARGS` flag is excluded, checked at import time.

**The editor.** `ProfileModal.tsx` offers two views of one string. The picker
renders a typed row per flag present in the profile's args that the catalog
knows about — label, an input matching the flag's kind, its help text and a
remove button — and "Add argument" (grouped by `category`, with an "advanced"
toggle) adds a row for a flag not yet present. Everything else lands in the raw
box below. Neither is separate state: both read and write the one
`llama_args_text`, so they can never disagree. Save is disabled while any row
is marked ⚠ — an unknown `--samplers` stage name, unparseable text, or a value
outside its recommended band — or while the name is blank or clashes.

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
                      "high": "claude-opus-5", "max": "claude-fable-5" }
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

### 6a. HuggingFace access token management (kodo-vsix only)

HuggingFace tokens follow the same pull-protocol pattern as cloud API keys
but are simpler: single-purpose (no vendor concept), single token active at a
time, and optional (public repos don't need one). Managed in the "HuggingFace"
tab of the Kōdo Settings panel.

- `~/.kodo/etc/hf_tokens.json` (kodo-vsix-owned): a map of token UUIDs to
  friendly names, plus which one is active:
  ```json
  { "tokens": { "3fa8...uuid": "work", "9c21...uuid": "personal" },
    "active": "3fa8...uuid" }
  ```
- The actual secret lives in VS Code `SecretStorage`, keyed by the UUID.
- **Adding a token** (via "Add new token" button in Kōdo Settings →
  HuggingFace): prompt for a friendly name and the token secret, generate a
  UUID, store the secret in SecretStorage, record `{uuid: name}` in
  `hf_tokens.json`, mark it active.
- **Removing a token** ("Remove this token"): delete the secret from
  SecretStorage and its entry from `hf_tokens.json`; if it was active, pick
  the first remaining token as the new active (or leave none if empty).
- **Activating a token** ("Make active"): flips `active` in `hf_tokens.json`.
- Answering `hf_token.request` (WS_PROTOCOL.md §6.5): look up `active` UUID,
  `SecretStorage.get(uuid)`; if none configured, respond with empty string —
  the download proceeds unauthenticated (works for public repos).
- Answering `hf_token.revoke` (WS_PROTOCOL.md §6.6): remove the currently
  active token entirely (same as the "Remove this token" action) and show a
  warning notification to the user.

The server sends `hf_token.request` on the **control connection** before
every download (install, resume, or update) — not on session connections,
because model downloads are window-global. The extension responds
immediately with the active token or empty string.

**Gated repos without a token:** if the user attempts to download a gated
model without configuring a token, the download fails with a clear error
message ("is a gated repository — provide an HF access token with access
to it"). The user can then add a token through the HuggingFace settings tab
and retry.

Only one key per vendor may be active at a time; only Anthropic is wired up
today (single-vendor cloud registry, §3), but the shape is per-vendor from
the start.
