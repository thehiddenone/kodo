# Kodo — Settings Reference

> Reference: [WS_PROTOCOL.md](WS_PROTOCOL.md) §7.7–7.8.

This document describes every key recognised in Kodo's settings files, where those files live, and how to change them at runtime.

---

## 1. File locations and precedence

Kodo merges two settings files at server startup and on every `config.reload` command.  Project-level values override user-level values; any key absent from both files falls back to its compiled-in default.

| Layer | Path | Scope |
|---|---|---|
| User | `~/.kodo/settings.json` | All projects on this machine |
| Project | `<project>/.kodo/settings.json` | This project only |

`~/.kodo/settings.json` is created automatically on first run with sensible defaults (see §3).  `<project>/.kodo/settings.json` is never created automatically; create it only when you need project-specific overrides.

**API keys are not settings.**  They are delivered at runtime over the WebSocket via `credentials.set` (WS_PROTOCOL.md §7.8) and held in memory only.  Never write an API key to either settings file.

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

### 2.2 `default_model`

The dict key of the model the engine uses when a subagent does not specify one.  Must match an entry in the `models` dict (§2.4).  Subagents may override this per-invocation by requesting any key present in `models`.

```json
{ "default_model": "claude-sonnet-4-6" }
```

**Model switching** = change `default_model` to the target key, save the file, send `config.reload`.

### 2.3 Context limit (per-model — not a setting)

The token budget for an entry agent's **main context** (the shared Guide / Problem Solver conversation) is **not** a global setting. It is the **current model's context window**, defined per model as `context_window` in `kodo/llms/_registry.py` (e.g. Claude Opus/Sonnet 4.x = 1,000,000; Haiku 4.5 = 200,000; local Qwen3 = 262,144; local Gemma = 131,072). After every entry-agent turn the engine measures the context (last call's input + cache + output tokens); once it reaches **90%** of the current model's window it automatically runs the `compactor` sub-agent, which summarises the conversation and resets the live context in place (a `compaction` marker is written to `session.jsonl`; the full log is kept as audit). The user can also trigger this at any idle moment via the header's **Compact now** button (`compact.now`).

Because the limit follows the model, **switching the model changes it immediately** (`config.reload` notifies every live session). Switching to a model whose window is **smaller than the live context** triggers an auto-compaction *using the outgoing model* before the switch takes effect (see STATE_AND_LIFECYCLE.md §4.5). The legacy `context_limit` setting was **removed**; to change the budget, change the model or edit its `context_window` in the registry.

### 2.4 `models`

A dictionary of all model definitions available to this Kodo installation.  Each entry describes one model; any entry can be selected as the `default_model` (§2.2).  There is no `active` flag — selection happens through `default_model`.

#### Cloud model entry

```json
"claude-sonnet-4-6": {
  "local": false,
  "module": "kodo.llms.anthropic",
  "model_id": "claude-sonnet-4-6",
  "description": "Anthropic Claude Sonnet 4.6 — cloud, balanced speed and intelligence"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `local` | bool | yes | Must be `false` for cloud models |
| `module` | string | yes | Python module that provides the plugin. The last component (e.g. `"anthropic"` from `"kodo.llms.anthropic"`) is also the credential store key used to look up the API key. Supported value: `"kodo.llms.anthropic"` |
| `model_id` | string | yes | Exact model identifier sent to the cloud API. Falls back to the dict key if omitted |
| `description` | string | no | Human-readable label shown in the VSIX model picker |

#### Local model entry (llama.cpp)

```json
"llamacpp-qwen36-27b": {
  "local": true,
  "module": "kodo.llms.llamacpp",
  "description": "Qwen 3.6 27B UD-Q4_K_XL — local inference via llama-server",
  "repo_id": "unsloth/Qwen3.6-27B-MTP-GGUF",
  "filename": "Qwen3.6-27B-UD-Q4_K_XL.gguf",
  "base_url": "http://127.0.0.1:8080/v1"
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `local` | bool | yes | Must be `true` for local models |
| `module` | string | yes | Must be `"kodo.llms.llamacpp"` |
| `description` | string | no | Human-readable label shown in the VSIX model picker |
| `repo_id` | string | yes | HuggingFace repository ID of the GGUF (used by `kodo.llm_utils` for download) |
| `filename` | string | yes | Specific GGUF filename inside the repository |
| `base_url` | string | no | URL of the running `llama-server` `/v1` endpoint. Defaults to `http://127.0.0.1:8080/v1` |

**Adding a model** is equivalent to adding an entry to this dict and saving the file.  Set `default_model` to the new key when you want to activate it.

---

## 3. Default user settings

`~/.kodo/settings.json` is written automatically on first server startup if it does not exist:

```json
{
  "log_level": "INFO",
  "default_model": "claude-sonnet-4-6",
  "models": {
    "claude-opus-4-8": {
      "local": false,
      "module": "kodo.llms.anthropic",
      "model_id": "claude-opus-4-8",
      "description": "Anthropic Claude Opus 4.8 — cloud, highest capability"
    },
    "claude-opus-4-7": {
      "local": false,
      "module": "kodo.llms.anthropic",
      "model_id": "claude-opus-4-7",
      "description": "Anthropic Claude Opus 4.7 — cloud, high capability"
    },
    "claude-sonnet-4-6": {
      "local": false,
      "module": "kodo.llms.anthropic",
      "model_id": "claude-sonnet-4-6",
      "description": "Anthropic Claude Sonnet 4.6 — cloud, balanced speed and intelligence"
    },
    "claude-opus-4-6": {
      "local": false,
      "module": "kodo.llms.anthropic",
      "model_id": "claude-opus-4-6",
      "description": "Anthropic Claude Opus 4.6 — cloud, high capability"
    },
    "llamacpp-qwen36-27b": {
      "local": true,
      "module": "kodo.llms.llamacpp",
      "description": "Qwen 3.6 27B UD-Q4_K_XL — local inference via llama-server",
      "repo_id": "unsloth/Qwen3.6-27B-MTP-GGUF",
      "filename": "Qwen3.6-27B-UD-Q4_K_XL.gguf"
    }
  }
}
```

---

## 4. Example: project override to use a local model

`<project>/.kodo/settings.json`:

```json
{
  "default_model": "llamacpp-qwen36-27b"
}
```

The project file only needs to override `default_model`; the full model registry is inherited from `~/.kodo/settings.json`.  After saving, send `config.reload` from the VSIX.
