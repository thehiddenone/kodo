# Kodo — Settings Reference

> Reference: [WS_PROTOCOL.md](WS_PROTOCOL.md) §7.5, §6.3–6.4; [LLM_REGISTRY.md](LLM_REGISTRY.md) for the full model-registry/effort-level design.

This document describes every key recognised in Kodo's settings file, where it lives, and how to change it at runtime.

---

## 1. File location

Kodo is a machine-wide singleton server rooted at `~/.kodo`. Settings live in a **single** `~/.kodo/etc/settings.json` — there is no per-workspace/per-project layering; any key absent from the file falls back to its compiled-in default (`kodo/server/_config.py`'s `_DEFAULT_USER_SETTINGS`).

`~/.kodo/etc/settings.json` is created automatically on first run with the defaults in §3. It is re-read fresh on every `config.reload` request (WS_PROTOCOL.md §7.5) and, for most keys, fresh on every LLM dispatch (no caching).

**API keys are not settings.** They are delivered at runtime over the WebSocket via the `api_key.request`/`api_key.revoke` pull protocol (WS_PROTOCOL.md §6.3–6.4) and held in memory only by the server — the kodo-vsix extension owns all persistent key storage (VS Code SecretStorage) and never writes a key to this file. See LLM_REGISTRY.md §6 for the extension-side multi-key management this now supports.

The local-model registry's user-added entries and the llama-server binary override live in a **separate** file, `~/.kodo/etc/local-llm-registry.json`, owned by `kodo/llms/_local_registry.py` — see LLM_REGISTRY.md §4. That file is not part of `settings.json` and is not affected by `config.reload`.

---

## 2. Keys

### 2.1 `log_level`

Python logging level for the server process.

| Value | Meaning |
|---|---|
| `"DEBUG"` | Verbose — every internal decision logged |
| `"INFO"` | Normal operation (default) |
| `"WARNING"` | Warnings and errors only |
| `"ERROR"` | Errors only |

```json
{ "log_level": "DEBUG" }
```

### 2.2 `mode`, `active_cloud_vendor`, `models`

Together these three keys select the model used for every LLM call. Full
design (registries, effort levels, resolution order) is in
[LLM_REGISTRY.md](LLM_REGISTRY.md) §2/§5 — this is the settings-file summary.

| Key | Type | Meaning |
|---|---|---|
| `mode` | `"local"` \| `"cloud"` | Which registry supplies the active model |
| `active_cloud_vendor` | string | Vendor key into `models.cloud` when `mode == "cloud"` (e.g. `"anthropic"`) |
| `models.local` | string | Local registry entry name used for every capability when `mode == "local"` |
| `models.cloud.<vendor>.<effort>` | string | Cloud `model_id` used for that vendor + effort level (`low`/`medium`/`high`/`max`) when `mode == "cloud"` |

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

**Model switching**: change the relevant key(s), save the file, send `config.reload`. In local mode there is one active model regardless of a sub-agent's declared `capability`; in cloud mode, `capability` selects which of the active vendor's four effort-level assignments is used. There is no `default_model`/flat-`models`-dict scheme any more (an older revision of this document described one; it never matched the shipped code — see `_resolve_model_key` in `kodo/runtime/_engine/_llm.py` for the actual resolution logic).

**Registering a new model**: cloud models are 100% hardcoded (`kodo/llms/_cloud_registry.py`) — adding one is a code change, not a settings change. Local models can be added live from the Local Inference Settings webview (`local_llm.add_huggingface`/`add_file`/`add_server_url`, WS_PROTOCOL.md §7.6) without touching `settings.json` at all; only *which* installed local model is active goes through `models.local` here.

### 2.3 Context limit (per-model — not a setting)

The token budget for an entry agent's **main context** (the shared Guide / Problem Solver conversation) is **not** a global setting. It is the **current model's context window**, defined per model as `context_window` in `kodo/llms/_cloud_registry.py` or `kodo/llms/_local_registry.py` (e.g. Claude Opus/Sonnet/Fable = 1,000,000; Haiku 4.5 = 200,000; local Qwen3 = 262,144; local Gemma = 131,072), resolved via `kodo.llms.get_context_window`. After every entry-agent turn the engine measures the context (last call's input + cache + output tokens); once it reaches **90%** of the current model's window it automatically runs the `compactor` sub-agent, which summarises the conversation and resets the live context in place (a `compaction` marker is written to `session.jsonl`; the full log is kept as audit). The user can also trigger this at any idle moment via the header's **Compact now** button (`compact.now`).

Because the limit follows the model, **switching the model changes it immediately** (`config.reload` notifies every live session). Switching to a model whose window is **smaller than the live context** triggers an auto-compaction *using the outgoing model* before the switch takes effect (see STATE_AND_LIFECYCLE.md §4.5). The legacy `context_limit` setting was **removed**; to change the budget, change the model or edit its `context_window` in the registry.

### 2.4 `cloud_concurrency`

Maximum number of concurrent in-flight requests per cloud vendor feed (`kodo/llms/_gateway.py`). Local inference always uses a serial feed (`max_slots == 1`) regardless of this setting, since one `llama-server` process serves one request at a time.

```json
{ "cloud_concurrency": 2 }
```

### 2.5 `llm_models_dir`

Overrides the directory where downloaded GGUF files are cached (default `~/.kodo/llama.cpp/models`). Read directly by `kodo/llms/llamacpp/_manager.py`'s `get_local_model_manager`.

### 2.6 `stuck_detection`

Governs the stuck-agent watchdog (doc/STUCK_DETECTION.md) — detects a turn that ended without finishing its task (e.g. an empty final response, or one truncated by the output-length cap) and nudges the agent to continue. This same block also gates the mid-stream cyclic-thinking detector (doc/STUCK_DETECTION.md §2.7), which catches a thinking block degenerating into a repetition loop *while it streams*, rather than after the fact — reused unchanged rather than exposing a second settings surface. Exposed in the Kōdo Settings webview panel's "General" section via the `stuck_detection.get`/`.set` WS commands (WS_PROTOCOL.md §7.6d); this file is still the on-disk ground truth, and hand-editing it + sending `config.reload` still works.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `stuck_detection.active` | `"off"` \| `"local_only"` \| `"local_and_cloud"` | `"local_only"` | Which model residence the watchdog (and the cyclic-thinking detector) runs for. Local LLMs are the primary target — both are small/quantized-model failure modes cloud models rarely exhibit. |
| `stuck_detection.scope` | `"top_level"` \| `"top_level_and_subagents"` | `"top_level"` | Whether only the main entry agent (Guide/Problem Solver) is watched, or sub-agents (`run_subagent`/`run_author_critic_iteration`) too — applies to both detectors identically. |
| `stuck_detection.auto_unstuck_interactive` | `bool` | `false` | Outside autonomous mode, whether a detected stall is nudged automatically (`true`) or surfaced as a `prompt.stuck_alert` the user must confirm (`false`). Autonomous mode always nudges immediately, regardless of this flag. **Ordinary stall remediation only** — the cyclic-thinking detector never consults this flag, since by the time it fires the stream is already dead and the repeated content already generated, so remediation there is always immediate. |

```json
{ "stuck_detection": { "active": "local_only", "scope": "top_level", "auto_unstuck_interactive": false } }
```

---

## 3. Default user settings

`~/.kodo/etc/settings.json` is written automatically on first server startup if it does not exist:

```json
{
  "log_level": "INFO",
  "mode": "local",
  "cloud_concurrency": 2,
  "active_cloud_vendor": "anthropic",
  "models": {
    "local": "llamacpp-qwen36-27b-q4-k-xl",
    "cloud": {
      "anthropic": {
        "low": "claude-haiku-4-5-20251001",
        "medium": "claude-sonnet-5",
        "high": "claude-opus-4-8",
        "max": "claude-fable-5"
      }
    }
  },
  "stuck_detection": {
    "active": "local_only",
    "scope": "top_level",
    "auto_unstuck_interactive": false
  }
}
```

An incompatible or missing file falls back to these compiled-in defaults key by key — there is no migration path from the older 3-tier/flat-`models`-dict schema.
