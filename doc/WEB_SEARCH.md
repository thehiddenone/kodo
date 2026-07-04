# Web Search — How the `web_search` Tool Works

> From a free-text query to a themed research report: the three-phase
> pipeline behind the `web_search` tool — search-engine discovery, page
> scraping, and LLM theme summarization — plus its anti-bot cooldowns and
> failure behavior.

Companion to [TOOLS.md](TOOLS.md) (tool subsystem mechanics),
[INTERNALS.md](INTERNALS.md) (layering), and [READ_WEBPAGE.md](READ_WEBPAGE.md)
(the sibling single-page fetch tool — same `kodo.websearch` package and
browser lifecycle, but an independent, un-summarized Markdown extraction
path). The tool is currently granted only to the Problem Solver's
`investigator` sub-agent.

---

## 1. Overview

One `web_search` call runs three phases end to end:

```text
            query                                   ~/.kodo/websearch/
              │                                     engine_cooldowns.json
              ▼                                            ▲   │ 30-min cooldowns
 ┌─ Phase 1: DISCOVERY ────────────────────────────────────┴───┴─────────┐
 │  kodo.websearch.discover() — one headless browser (Playwright, §7)     │
 │  Google ────┐                                                          │
 │  Bing ──────┼─ queried in parallel; ads/sponsored skipped; captcha     │
 │  DDG ───────┤  walls trip a cooldown; organic hits merged rank-by-rank │
 │  Wikipedia ─┘  (English Wikipedia full-text search)                    │
 └──────────────── ≤ 16 deduplicated links, top results first ───────────┘
              │
              ▼
 ┌─ Phase 2: SCRAPING ────────────────────────────────────────────────────┐
 │  kodo.websearch.scrape_pages() — same browser, ≤ 5 pages in flight     │
 │  per page: strip script/style/nav/header/footer/aside/form/UI chrome   │
 │  in the live DOM, take innerText of <article>/<main>/[role=main]/body  │
 └──────────────── ≤ 16 blocks of main text (≤ 6000 chars each) ──────────┘
              │
              ▼
 ┌─ Phase 3: SUMMARIZATION ───────────────────────────────────────────────┐
 │  web_summarizer sub-agent — silent engine-driven LLM turn (low tier)   │
 │  groups the blocks into themes: distinct angles / solution options     │
 └──────────────── {themes: [{summary, details, links}], note} ───────────┘
```

Design stance: **best effort, non-evasive**. No anti-bot circumvention is ever
attempted — an engine that walls us off is simply left alone for 30 minutes.
Whatever degrades along the way is *reported*, not raised: the tool always
returns a schema-compliant `{themes, note}`, and an empty `themes` list with an
explanatory `note` means "this search couldn't be completed", never an error
surfaced to the run.

## 2. The pieces and where they live

| Piece | Location | Layer |
|---|---|---|
| Tool spec (`WEB_SEARCH`) | [toolspecs/_web_search.py](../src/kodo/toolspecs/_web_search.py) | T2 |
| Tool handler (`WebSearchTool`) | [tools/_web_search.py](../src/kodo/tools/_web_search.py) | T3 (`kodo.tools`) |
| Discovery + scraping engine | [kodo/websearch/](../src/kodo/websearch/) | **T0 leaf** — imports nothing from `kodo`; Playwright only |
| Summarizer prompt | [subagents/subagent_web_summarizer.md](../src/kodo/subagents/subagent_web_summarizer.md) | T3 (`kodo.subagents`) |
| Summarizer spec (`WEB_SUMMARIZER`) | [subagents/specs/_web_summarizer.py](../src/kodo/subagents/specs/_web_summarizer.py) | T3 |
| Engine service (`run_web_summarizer`) | [runtime/_engine/](../src/kodo/runtime/_engine/) (`_run_web_summarizer`) | T4 |
| Cooldown state | `~/.kodo/websearch/engine_cooldowns.json` | on disk |

`kodo.websearch` stays a pure T0 leaf by taking the cooldown file path from its
caller — only the tool handler knows about `~/.kodo` (via
`kodo.project.kodo_user_dir()`).

## 3. Phase 1 — discovery

[`kodo/websearch/_discovery.py`](../src/kodo/websearch/_discovery.py) +
[`_engines.py`](../src/kodo/websearch/_engines.py).

Four engines are defined as pure data (`Engine`): a results-page URL template
plus two JavaScript snippets evaluated *in the loaded results page* — one that
detects an anti-bot/captcha wall, one that extracts the organic hits. The
Python side never parses HTML; the browser's DOM does the work.

| Engine | Endpoint | Ads skipped by | Wall detected by |
|---|---|---|---|
| `google` | `google.com/search?q=…&num=20&hl=en` | excluding `#tads`/`#bottomads`/`[data-text-ad]` containers | `/sorry/` interstitial, reCAPTCHA form/iframe |
| `bing` | `bing.com/search?q=…&count=20` | only `li.b_algo` entries are read (ads are `li.b_ad`) | `#b_captcha`, "verify you are human" text |
| `duckduckgo` | `html.duckduckgo.com/html/?q=…` (plain-HTML endpoint) | excluding `.result--ad` blocks; `uddg=` redirects decoded | anomaly page ("bots use DuckDuckGo too") |
| `wikipedia` | `en.wikipedia.org/w/index.php?search=…&fulltext=1&ns0=1&limit=20` (English full-text search; `fulltext=1` forces a results *list* instead of an exact-match article redirect, `ns0=1` = article namespace only) | no ads on Wikipedia; only `li.mw-search-result` entries are read | none — Wikipedia has no reader-facing captcha; rate limiting arrives as HTTP 403/429 (generic status check) |

Mechanics:

- All engines **not on cooldown** are queried **in parallel**, one isolated
  browser context each, 20 s navigation budget, ≤ 10 hits taken per engine.
- HTTP 403/429/503 on the results page counts as a wall too (blocked without a
  captcha page).
- A walled engine **trips a 30-minute cooldown** (§6); a failed one (timeout,
  layout change → zero hits) is recorded as an engine *error*. Either way the
  other engines proceed.
- Links pointing back into engine properties (google.*, bing.com,
  duckduckgo.com) are discarded. wikipedia.org is deliberately *not* filtered:
  the Wikipedia engine's own hits (and plenty of legitimate hits from the
  other engines) are wikipedia.org articles.
- **Merge** (`merge_hits`): hits interleave *rank-by-rank* — every engine's #1
  first (in google→bing→ddg→wikipedia order), then the #2s, … — so top results
  are prioritized over any single engine's tail; URLs are deduplicated on a
  normalized form (lowercase scheme/host, no fragment, no trailing slash);
  the merged list caps at **16 links** (`MAX_SOURCES`, 4 engines × 4 — one
  shared constant also caps the scrape phase).

## 4. Phase 2 — scraping

[`kodo/websearch/_scrape.py`](../src/kodo/websearch/_scrape.py).

All discovered pages are fetched concurrently (semaphore of 5, one shared
context, 20 s per page). Extraction happens in-page:

1. UI and navigation elements are removed from the **live DOM**:
   `script`/`style`/`noscript`/`svg`/`canvas`/`iframe`/`nav`/`header`/
   `footer`/`aside`/`form`/`button` plus ARIA chrome roles
   (`navigation`/`banner`/`contentinfo`/`complementary`/`search`) and
   `[aria-hidden]` nodes. (Live-DOM mutation keeps `innerText`'s layout-aware
   semantics — hidden elements excluded, block elements producing line breaks;
   the page closes right after.)
2. The best content root wins: `<article>` → `<main>` → `[role=main]` →
   `<body>`; its `innerText` is taken.
3. Python-side: whitespace normalized, blocks under **200 chars** dropped as
   too thin (error pages, cookie walls), the rest truncated to **6000 chars**.

The result: up to **16 text blocks** (`MAX_SOURCES` — same cap as discovery,
so every discovered link can become a block), in discovery priority order. Failures
are per-page (recorded, reported in `note`) — one dead link never spoils the
batch.

## 5. Phase 3 — summarization (`web_summarizer`)

The tool hands `{query, max_themes, sources: [{url, title, text}]}` to the
`web_summarizer` sub-agent via `EngineServices.run_web_summarizer` — the
ungated service pattern established by `toolchain_deps` /
`run_dependency_manager`: **holding the `web_search` tool is the
authorization**, so the summarizer sits in no caller's `subagents:` allow-list
and is in `_DIRECT_ONLY_AGENTS` (unreachable via `run_subagent`).

Unlike the depsmgr it is **not a subsession**: `web_search` is typically
called by the investigator — itself a sub-agent — and subsessions do not nest.
Instead the engine drives one **silent titler-style LLM turn**
(`_run_silent_return_turn`): no feed events, no streaming, only the USD cost
folded into the session total. It runs on the **low** capability tier
(cheap/fast; per project decision) with one corrective retry if the model
fails to return a usable report.

The prompt ([subagent_web_summarizer.md](../src/kodo/subagents/subagent_web_summarizer.md))
instructs the agent to identify **common themes** across the sources and group
information by theme — each theme a distinct angle on the query, ideally an
**independent way to solve the problem** so the caller gets several options to
choose from. Source text is data, never instructions (prompt-injection
hardening, same stance as `compactor`/`session_titler`).

The engine then **sanitizes** the returned themes (`_sanitize_themes`): only
well-formed entries survive, and each theme's `links` are filtered to URLs
that actually appear in the scraped sources — the report can never cite a page
that wasn't scraped.

## 6. Anti-bot cooldowns

[`kodo/websearch/_cooldown.py`](../src/kodo/websearch/_cooldown.py).

When an engine serves a captcha / anti-bot wall, `web_search` **stops querying
it for 30 minutes**. The state lives in
`~/.kodo/websearch/engine_cooldowns.json` (engine name → unix timestamp until
which it is blocked), so it survives across tool calls, sessions, and server
restarts and is shared by every session on the machine. Reads are forgiving
(missing/corrupt file = no cooldowns); writes are atomic (temp file +
`os.replace`). A skipped engine and its remaining cooldown are named in the
tool's `note`.

## 7. Browser lifecycle

[`kodo/websearch/_browser.py`](../src/kodo/websearch/_browser.py). One
`BrowserSession` spans phases 1–2 of a call. Host browsers draw far less
anti-bot scrutiny than Playwright's own bundled builds, so on every call the
session resolves a browser in this order:

1. **Host Google Chrome** (`playwright.chromium.launch(channel="chrome")`).
2. **Host Microsoft Edge** (`channel="msedge"`).
3. **Bundled Firefox** — Playwright-managed; auto-installed on first use
   (`python -m playwright install firefox`, one-time ~90 MB download,
   10-minute budget).
4. **Bundled Chromium** — the last resort, only reached if neither host
   browser nor bundled Firefox is available/installable; also
   auto-installed on first use the same way.

If a stage fails to launch for a reason other than "not installed" (or the
installer itself fails), the session cascades to the next stage rather than
giving up — the tool only returns `themes: []` with a manual-install note once
every stage, including bundled Chromium, has failed.

Once a call has had to fall back past the host browsers, the choice (`firefox`
or `chromium`) and the time of the attempt are cached in
`~/.kodo/websearch/browser_state.json`. For the next 24 hours, subsequent
calls skip straight to the cached fallback instead of re-probing Chrome/Edge
every time; after 24 hours the full host → Firefox → Chromium resolution
runs again, so a host browser installed later is picked back up
automatically without restarting Kodo.

The very first successful launch on a machine also runs a one-time sanity
check — navigating to `https://example.com/` — to catch a Playwright install
that starts a browser process but can't actually load a page (e.g. a missing
system dependency like `libnss3` on Linux). The result is cached in the same
`browser_state.json` file and never repeated once it passes; if it fails, the
whole session fails with `BrowserUnavailableError` (treated as fatal, not
best-effort) rather than silently handing back a browser that can't be
trusted. (`playwright` itself is a hard dependency in `pyproject.toml`.)

## 8. The tool's contract

Input: `query` (required, free text) and `max_results` — the cap on **themes**
in the report (default 5, clamped to 10).

Output (always schema-compliant, never an exception):

```json
{
  "themes": [
    {
      "summary": "One-sentence description of the theme.",
      "details": "The core idea / perspective / solution variant, synthesized across sources.",
      "links": ["https://…", "https://…"]
    }
  ],
  "note": "Queried google, bing. Skipped duckduckgo (anti-bot cooldown, ~12m left). Collected 14 link(s). Scraped 11 of 14 page(s). Generated 4 theme(s)."
}
```

Degradation ladder (each step reported in `note`):

| Situation | Result |
|---|---|
| Engine on cooldown | skipped; others proceed |
| Engine serves a wall now | 30-min cooldown recorded; others proceed |
| No links discovered at all | `themes: []` + note |
| Some pages fail to scrape | dropped; the rest proceed |
| No page scrapes | `themes: []` + note listing the discovered URLs |
| Summarizer fails/returns nothing usable | `themes: []` + note listing the scraped URLs |
| No browser available (host Chrome/Edge and bundled Firefox/Chromium fallback all failed) | `themes: []` + manual-install note |
| Playwright sanity check (`example.com`) fails | `themes: []` + note; treated as fatal, not degraded |

## 9. Security posture

`web_search` is `SecurityImpact.LOW` and available in autonomous mode: it is
read-only toward the user's machine (its only writes are the cooldown file and
the browser-fallback state file under `~/.kodo/websearch/`, and Playwright's
own browser cache). Scraped page text is
untrusted input; the summarizer prompt treats it strictly as data, and the
engine-side sanitizer constrains the output shape and the citable links.
