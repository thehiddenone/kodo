# Web Search — How the `web_search` Tool Works

> From a free-text query to a themed research report: the `web_search`
> agent that plans its own discovery/read/synthesis loop, its two backends
> (Playwright browsers and `curl_cffi`), its pacing/memory tools, and its
> timeout model.

Companion to [TOOLS.md](TOOLS.md) (tool subsystem mechanics),
[INTERNALS.md](INTERNALS.md) (layering), and [READ_WEBPAGE.md](READ_WEBPAGE.md)
(the sibling single-page fetch tool — same `kodo.websearch` package and fetch
backends, but an independent, un-agent-driven path). `web_search` is
currently granted only to the shared `investigator` sub-agent (spawnable by
both entry agents, `problem_solver` and `guide`).

---

## 1. Overview

`web_search` used to be a fixed, deterministic three-phase pipeline
(discover all four engines in parallel → scrape every page → summarize with
a second silent LLM pass). It is now driven by a real agent:

```text
                 query, max_results, timeout
                              │
                              ▼
 ┌─ web_search tool (tools/_web_search.py) ─────────────────────────────────┐
 │  validates `query`, clamps max_results (≤10) and timeout (≤600s),        │
 │  delegates to EngineServices.run_web_search_agent                       │
 └───────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌─ web_search agent (subagent_web_search.md, capability: medium) ──────────┐
 │  a silent, multi-round tool-calling turn (_run_silent_tool_loop_turn)    │
 │  the agent itself decides, round by round:                              │
 │   - query_search_engine(engine, query, browser?) — one engine per call  │
 │   - read_webpage(url, browser?, content_filter?) — read a promising page│
 │   - get_web_search_state / update_web_search_state — pacing memory      │
 │   - wait — space out requests                                          │
 │   - remaining_time — check its own clock                                │
 │  until it calls return_result with {themes, note}                       │
 └───────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                     {themes: [...], note: "..."}
```

Design stance: **best effort**, with one deliberate, bounded exception to the
project's historical "no anti-bot circumvention" posture. `browser: "curl"`
(§7) impersonates a real browser's TLS/HTTP2 fingerprint via `curl_cffi` —
that is network-layer impersonation, not JS-fingerprint spoofing, a proxy, or
CAPTCHA solving, and it is the agent's own explicit choice per call, not a
silent default. Whatever the agent could not do — an engine wall it hit, a
page it could not read, running out of time — is *reported* in `note`, never
raised: the tool always returns a schema-compliant `{themes, note}`, and an
empty `themes` list with an explanatory `note` means "this search couldn't be
completed", never an error surfaced to the run.

## 2. The pieces and where they live

| Piece | Location | Layer |
|---|---|---|
| Tool spec (`WEB_SEARCH`) | [toolspecs/_web_search.py](../src/kodo/toolspecs/_web_search.py) | T2 |
| Tool handler (`WebSearchTool`) | [tools/_web_search.py](../src/kodo/tools/_web_search.py) | T3 (`kodo.tools`) |
| Discovery tool spec/handler (`QUERY_SEARCH_ENGINE`) | [toolspecs/_query_search_engine.py](../src/kodo/toolspecs/_query_search_engine.py), [tools/_query_search_engine.py](../src/kodo/tools/_query_search_engine.py) | T2/T3 |
| Pacing tool specs/handlers | `toolspecs/_get_web_search_state.py`, `_update_web_search_state.py`, `_wait.py`, `_remaining_time.py` + matching `tools/_*.py` | T2/T3 |
| Fetch backends (browser + curl) | [kodo/websearch/](../src/kodo/websearch/) | **T0 leaf** — imports nothing from `kodo` |
| Engine definitions (URL templates, JS extractors) | [kodo/websearch/_engines.py](../src/kodo/websearch/_engines.py) | T0 leaf |
| Browser-backed single-engine query | [kodo/websearch/_enginequery.py](../src/kodo/websearch/_enginequery.py) | T0 leaf |
| `curl` backend fetch | [kodo/websearch/_curlfetch.py](../src/kodo/websearch/_curlfetch.py) | T0 leaf |
| `curl` backend extraction (no live DOM) | [kodo/websearch/_htmlextract.py](../src/kodo/websearch/_htmlextract.py), [_engines_static.py](../src/kodo/websearch/_engines_static.py) | T0 leaf |
| Agent-managed pacing/memory store | [kodo/websearch/_state.py](../src/kodo/websearch/_state.py) (`WebSearchStateStore`) | T0 leaf |
| `web_search` agent prompt | [subagents/subagent_web_search.md](../src/kodo/subagents/subagent_web_search.md) | T3 (`kodo.subagents`) |
| `web_search` agent spec | [subagents/specs/_web_search_agent.py](../src/kodo/subagents/specs/_web_search_agent.py) | T3 |
| Silent tool-loop turn primitive | [runtime/_engine/_llm.py](../src/kodo/runtime/_engine/_llm.py) (`_run_silent_tool_loop_turn`) | T4 |
| Engine service (`run_web_search_agent`) | [runtime/_engine/_subagents.py](../src/kodo/runtime/_engine/_subagents.py) (`_run_web_search_agent`) | T4 |
| Agent-memory state file | `~/.kodo/websearch/agent_state.json` | on disk |
| Browser-lifecycle cache | `~/.kodo/websearch/browser_state.json` | on disk |

`kodo.websearch` stays a pure T0 leaf by taking every state-file path from
its caller (the tool handlers) — only they know about `~/.kodo` (via
`kodo.project.kodo_user_dir()`).

## 3. `query_search_engine` — the discovery primitive

[`toolspecs/_query_search_engine.py`](../src/kodo/toolspecs/_query_search_engine.py) /
[`tools/_query_search_engine.py`](../src/kodo/tools/_query_search_engine.py).

One engine, one call: `{engine, query, browser?, headed?}` →
`{hits: [{url, title, snippet}]}` or `{"error": "..."}` on a wall. This
replaced the old `discover()` phase that queried all four engines in
parallel and merged their hits rank-by-rank — the agent now decides which
engine to query, in what order, and how many to try, using its own judgment
(and the pacing tools below) instead of a fixed schedule.

Four engines, defined as pure data in
[`_engines.py`](../src/kodo/websearch/_engines.py) — a results-page URL
template plus wall-detection/extraction logic:

| Engine | Endpoint | Ads skipped by | Wall detected by |
|---|---|---|---|
| `google` | `google.com/search?q=…&num=20&hl=en` | excluding `#tads`/`#bottomads`/`[data-text-ad]` containers | `/sorry/` interstitial, reCAPTCHA form/iframe |
| `bing` | `bing.com/search?q=…&count=20` | only `li.b_algo` entries are read (ads are `li.b_ad`) | `#b_captcha`, "verify you are human" text |
| `duckduckgo` | `html.duckduckgo.com/html/?q=…` (plain-HTML endpoint) | excluding `.result--ad` blocks; `uddg=` redirects decoded | anomaly page ("bots use DuckDuckGo too") |
| `wikipedia` | `en.wikipedia.org/w/index.php?search=…&fulltext=1&ns0=1&limit=20` (English full-text search) | no ads on Wikipedia; only `li.mw-search-result` entries are read | none — rate limiting arrives as HTTP 403/429 |

Links pointing back into engine properties (`google.*`, `bing.com`,
`duckduckgo.com`) are discarded (`is_engine_internal`); `wikipedia.org` is
deliberately **not** filtered, since it's a legitimate hit source for every
engine. Up to 20 hits per call.

**Two backends**, chosen per call via `browser` (same choices as
`read_webpage`, §7 of READ_WEBPAGE.md):

- Any Playwright kind (`firefox` default, `chrome`, `edge`, `webkit`,
  `chromium`) — [`_enginequery.py`](../src/kodo/websearch/_enginequery.py)
  runs the same wall-detection/extraction JS the old discovery phase used,
  in a live page.
- `curl` — [`_curlfetch.py`](../src/kodo/websearch/_curlfetch.py) fetches the
  results page (TLS/browser-signature impersonation, no browser process),
  and [`_engines_static.py`](../src/kodo/websearch/_engines_static.py) — a
  from-scratch Python/`selectolax` port of the same per-engine logic —
  extracts hits from the raw HTML.

## 4. `read_webpage` — reading a chosen page

Full contract in [READ_WEBPAGE.md](READ_WEBPAGE.md). The agent uses it to
read whichever pages `query_search_engine` surfaced as promising, with
`content_filter: "text"` (the default) for reading material.

## 5. Pacing and memory: the four dedicated tools

Exclusive to the `web_search` agent by convention (not listed in any other
agent's frontmatter `tools:`):

| Tool | Purpose |
|---|---|
| `get_web_search_state` | Read the agent's full persistent key-value memory. |
| `update_web_search_state` | Write one key: a note, a deletion (`value: ""`), or a `<time_mark>` (records `time.time()` under that key). |
| `wait` | Sleep (default 5s, capped 30s/call) — the agent's lever against bursting requests. |
| `remaining_time` | Seconds left before this run's timeout. |

### `WebSearchStateStore` ([`_state.py`](../src/kodo/websearch/_state.py))

Replaces the old deterministic 30-minute `CooldownStore`. A generic
key-value store persisted at `~/.kodo/websearch/agent_state.json`, **shared
machine-wide across sessions** (its 12-hour TTL per entry far outlives any
single, 600s-capped `web_search` call, so this memory has to survive across
calls). Same atomic-write/forgiving-read conventions as every other file
under `~/.kodo/websearch/`.

Each entry's TTL resets on every write to that key. Two kinds of value:

- A plain string — a note to itself (e.g. `google_status: "blocked: captcha"`).
- `<time_mark>` (the literal string `update_web_search_state` recognizes) —
  records `time.time()` under that key instead of storing the literal text.
  Reading it back via `get_web_search_state` returns **the number of seconds
  elapsed since it was recorded**, freshly computed on every read — not the
  timestamp, and not the string `<time_mark>`.

The agent's prompt (`subagent_web_search.md`) spells out the protocol with a
worked example: time-mark `<engine>_last_query` right before querying an
engine, so a later `get_web_search_state` call tells it how long it's been;
record a `<engine>_status` note when an engine serves a wall, so it isn't
retried this session. This replaces the old code-enforced 30-minute cooldown
with agent judgment — the model decides pacing instead of a fixed timer.

## 6. The silent tool-loop turn

Two turn-loop shapes already existed in the engine before this: the full,
feed-visible `_run_agent_turn`/`_drive_subsession` (a real subsession — but
subsessions can't nest, and `web_search` is typically called *from* a
sub-agent, the investigator), and the single-shot, no-dispatch
`_run_silent_return_turn` (`compactor`/the retired `web_summarizer` — no tool
loop at all, just one call captured for its `return_result`; session titling
used to be a third example here but is now `kodo.titling`, a local
summarization model with no LLM turn at all). Neither fits an agent that
needs a real multi-round tool loop without opening a subsession.

`_run_silent_tool_loop_turn` ([`runtime/_engine/_llm.py`](../src/kodo/runtime/_engine/_llm.py))
is the new primitive: modeled on `_run_agent_turn`'s loop, stripped of every
feed-visible/subsession side effect (no `EVT_AGENT_TOOL_CALL_PREP`/`DETAIL`,
no checkpoint prepare/commit, no tool-call markdown doc, no subsession
markers). It still dispatches every tool call through the real
`ToolDispatcher` (so the intent-check + security gate + actual `Tool.handle()`
call are unchanged), just without the UI side effects. Cost is folded into
the session total exactly like `_run_silent_return_turn`.

Bounded two ways:

- **`deadline`** (a wall-clock unix timestamp, from the tool's `timeout`) —
  once reached, the agent gets one final forced turn ("time is up, call
  return_result now") before the engine synthesizes a fallback.
- **A hard round cap** (60 rounds), independent of the clock, as a safety
  valve against a runaway loop.

`_run_web_search_agent` ([`runtime/_engine/_subagents.py`](../src/kodo/runtime/_engine/_subagents.py))
drives this: resolves the `web_search` agent (medium capability), computes
the deadline, builds a dispatcher scoped to it (`ToolContext.deadline` set,
read by `remaining_time`/`wait`), and returns the agent's `return_result`
payload — or, if it never produced one, `{"themes": [], "note": "Search
timed out before a report could be produced."}`.

`web_search` is engine-driven only (`_DIRECT_ONLY_AGENTS`) — never spawnable
via `run_subagent`, exactly like the agents it replaced.

### Live narration — the "Web Search is in progress" panel

`web_search` runs silently to the *calling agent* (the investigator never sees
intermediate steps, only the final report), but the panel shows the user a
live, collapsible "Web Search is in progress" block so a multi-minute run
doesn't look like a stall. The agent's prompt (`subagent_web_search.md`,
"Narrating Your Work") requires it to write one or two plain sentences before
each tool call (or small group of calls) explaining what it's about to do and
why — not a caption of the call, an actual decision.

Wiring: `_run_silent_tool_loop_turn` takes an optional `on_round_text`
callback, invoked with a round's assistant text (if any) right before that
round's tool calls dispatch — the loop itself stays agent-agnostic and knows
nothing about feed events or persistence. `_run_web_search_agent` is the only
caller today; it buffers every round's text and streams it live via
`EVT_WEB_SEARCH_NOTE` (`web_search.note`, `{tool_call_id, text}`,
WS_PROTOCOL.md §5.5e), correlated with the `web_search` call's own
`agent.tool_call_prep` card by `tool_call_id` (the calling agent's
`ToolContext.current_tool_use_id`, threaded through
`EngineServices.run_web_search_agent(task_input, tool_call_id)`).

Once the run ends (success, timeout, or exception), the buffered notes are
written once to a best-effort sidecar file,
`toolcalls/<tool_call_id>_websearch_notes.json`
(`TransientStore.write_web_search_notes`/`read_web_search_notes`) — keyed
purely by `tool_call_id` like the tool-call Markdown doc and diff-file pair,
so it works identically whether the call happened in the main turn or inside
any subsession. This is deliberately **not** persisted via `session.jsonl` or
the subsession log: doing so would either leak the narration into the calling
agent's own LLM context (a role-based message survives crash-resume replay) or
break replay outright (a role-less marker line isn't a valid `Message`).
Keeping it a side channel means a crash mid-run just loses whatever wasn't
flushed yet — acceptable, since this is a UI visibility aid, not part of the
agent's real conversation. `session.history`'s `tool_call` entries carry the
result as `webSearchNotes: string[]` (empty for every tool other than
`web_search`).

The client also reuses the existing `run_command` machinery for two more
pieces of visibility, gated to `web_search` alongside `run_command`: (1)
`agent.tool_call_prep` carries `timeout_seconds` (the caller's `timeout`, or
`_DEFAULT_WEB_SEARCH_TIMEOUT_S` when omitted) and (2)
`agent.tool_call_in_progress` fires once the security gate clears, so the
elapsed-vs-timeout progress bar starts at the same moment
`_run_web_search_agent` computes its own `deadline` — not at card-creation
time (`ToolDispatcher.dispatch`, SECURITY.md §6).

## 7. Anti-bot posture

Findings from a dedicated investigation
([doc/hidden/WEB_SEARCH_TOOL_REPORT.md](hidden/WEB_SEARCH_TOOL_REPORT.md))
drove two of this rework's decisions:

- **Bundled headless Chromium is the most fingerprintable config** (software
  WebGL, missing `window.chrome`, a `HeadlessChrome` UA) — yet it used to be
  the pipeline's last-resort fallback. Callers now pick a specific browser
  explicitly; there is no more cascade that could land there by accident.
- **`curl_cffi` (TLS/HTTP2 fingerprint impersonation, no browser process)
  passes DuckDuckGo/Bing/Wikipedia** — including cases where Playwright's own
  Chrome/Chromium signature gets walled — and is far cheaper for engines
  that are plain static HTML. It is now a first-class `browser` choice on
  both `query_search_engine` and `read_webpage`.
- **Google and DuckDuckGo are dominated by request-volume/IP reputation**,
  not fingerprint — no browser or stealth choice fixes a burned IP. This is
  the whole reason the pacing tools (§5) exist: the agent's own judgment
  about *when* to query, not a fixed schedule, is the actual lever.

Adopting `curl_cffi` is a deliberate, bounded exception to this project's
historical "no anti-bot circumvention" stance: it is network-layer signature
impersonation, not JS-fingerprint spoofing (no `playwright-stealth`, no
patched `navigator.webdriver`), not a residential proxy, and not CAPTCHA
solving. Every other posture — no bypass of an engine's actual wall, no
attempt to defeat rate-limiting beyond pacing — is unchanged.

## 8. Security posture

`web_search` is `SecurityImpact.MODERATE` and available in autonomous mode.
`query_search_engine` and `read_webpage` are `SecurityImpact.LOW`
(read-only network access); the four pacing tools are `SecurityImpact.NONE`
(no real-world effect beyond an ephemeral local state file or a sleep). The
only writes toward the user's machine, across the whole subsystem, are
`~/.kodo/websearch/agent_state.json`, `~/.kodo/websearch/browser_state.json`,
and Playwright's own browser cache. Fetched page text is untrusted input;
the agent's prompt treats it strictly as data (never instructions), the same
prompt-injection stance the retired `web_summarizer` used.
