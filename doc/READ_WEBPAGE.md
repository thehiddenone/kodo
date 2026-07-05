# Read Webpage — How the `read_webpage` Tool Works

> From a URL to content: the single-page fetch behind the `read_webpage`
> tool, its two backends (Playwright browsers and `curl_cffi`), its
> `content_filter` levels, its SSRF guard, and its anti-bot failure behavior.

Companion to [WEB_SEARCH.md](WEB_SEARCH.md) (the sibling `query_search_engine`/
`web_search` tools, which share the same `kodo.websearch` fetch backends but
serve a different purpose — querying a search engine vs. reading a known
page) and [TOOLS.md](TOOLS.md) (tool subsystem mechanics). The tool is
currently granted only to the shared `investigator` sub-agent (spawnable by
both entry agents, `problem_solver` and `guide`) and the `web_search` agent.

---

## 1. Overview

One `read_webpage` call fetches a caller-given URL and returns its content,
shaped by `content_filter`:

```text
                url, browser?, headed?, content_filter?
                              │
                              ▼
 ┌─ Validate (kodo.websearch.validate_public_url) ───────────────────────────┐
 │  scheme must be http/https; host must NOT resolve to a private/          │
 │  loopback/link-local/reserved address (SSRF guard — §3)                  │
 └────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌─ Fetch (backend picked by `browser`, §2) ─────────────────────────────────┐
 │  Playwright kind (default `firefox`) → kodo.websearch.fetch_via_browser  │
 │  `curl` → kodo.websearch.curlfetch + htmlextract                        │
 └────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
 ┌─ Shape per content_filter (§4), cap length, "too thin" gate (text only) ──┐
 └────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                {"content": "..."}     or     {"error": "..."}
```

Unlike `web_search`/`query_search_engine`, there is no discovery phase (the
URL is given) and no cooldown: a page that walls the tool off just returns an
`error` for that one call — repeating the exact same call (same URL, same
`browser`) will fail the same way, though a *different* `browser` choice may
succeed where another was walled.

## 2. The pieces and where they live

| Piece | Location | Layer |
|---|---|---|
| Tool spec (`READ_WEBPAGE`) | [toolspecs/_read_webpage.py](../src/kodo/toolspecs/_read_webpage.py) | T2 |
| Tool handler (`ReadWebpageTool`) | [tools/_read_webpage.py](../src/kodo/tools/_read_webpage.py) | T3 (`kodo.tools`) |
| Browser lifecycle (`BrowserSession`) | [kodo/websearch/_browser.py](../src/kodo/websearch/_browser.py) | T0 leaf — shared with `query_search_engine` |
| Browser-path fetch + extraction (`fetch_via_browser`) | [kodo/websearch/_readpage.py](../src/kodo/websearch/_readpage.py) | T0 leaf |
| `curl` backend fetch (`curlfetch.fetch`) | [kodo/websearch/_curlfetch.py](../src/kodo/websearch/_curlfetch.py) | T0 leaf |
| `curl` backend extraction (`htmlextract`) | [kodo/websearch/_htmlextract.py](../src/kodo/websearch/_htmlextract.py) | T0 leaf |
| SSRF guard + shared exceptions | [kodo/websearch/_validate.py](../src/kodo/websearch/_validate.py) | T0 leaf |

`_readpage.py` is deliberately independent of `_htmlextract.py`: they solve
the same problem (extract content per `content_filter`) for two different
data sources — a live browser DOM vs. raw fetched HTML — and keeping them
separate means a change to one cannot regress the other. Both share only
`_validate.py`'s SSRF guard/exceptions.

## 3. The SSRF guard

The agent hands `read_webpage` an arbitrary URL directly, which on a tool
that can launch a real browser or make an HTTP request on the user's machine
is a direct SSRF vector (probing `localhost`, a LAN service, or a cloud
metadata endpoint like `169.254.169.254`).

Before any request, `validate_public_url` ([`_validate.py`](../src/kodo/websearch/_validate.py)):

1. Rejects any scheme other than `http`/`https`.
2. Resolves the hostname (`socket.getaddrinfo`) and rejects the URL if **any**
   resolved address is loopback, private, link-local, multicast, reserved, or
   unspecified (`ipaddress.ip_address(...).is_*`).

A rejection raises `InvalidUrlError`. `ReadWebpageTool.handle` calls this
**before** touching either backend (a bad URL never pays for a browser
launch or a `curl_cffi` request); both `fetch_via_browser` and
`curlfetch.fetch` re-run the same check internally regardless, so the guard
stays safe for any other caller. This is a best-effort guard (a single DNS
check, not re-validated against the IP the backend actually connects to),
matching the project's general non-paranoid security stance.

## 4. `browser` — picking a fetch backend

| Value | What it is |
|---|---|
| `firefox` (default) | Bundled Playwright Firefox — the browser least likely to be flagged as a bot (doc/hidden/WEB_SEARCH_TOOL_REPORT.md). |
| `chrome` / `edge` | The host's own install, launched via a Chromium channel. **Errors immediately if not installed** — no fallback to any other kind. |
| `webkit` / `chromium` | Bundled Playwright browsers. |
| `curl` | `curl_cffi` — impersonates a real browser's TLS/HTTP2 fingerprint with no browser process at all. Often the fastest and least-detected choice for a page that doesn't need JavaScript. |

`headed` (default `false`) runs a visible window instead of headless;
ignored for `curl`.

### Browser lifecycle ([`_browser.py`](../src/kodo/websearch/_browser.py))

`BrowserSession` launches **exactly** the requested kind — there is no
cascade. Earlier versions of this tool tried host Chrome → host Edge →
bundled Firefox → bundled Chromium in order and fell back silently; now that
the caller names a specific kind explicitly (often for anti-bot reasons —
picking Firefox because it passes DuckDuckGo, or `curl` because it's
lighter), silently substituting a different one would defeat that choice and
could mask a broken host-browser setup. If the requested kind can't be
launched, `BrowserUnavailableError` is raised immediately.

Bundled kinds (`firefox`/`webkit`/`chromium`) auto-install on first use
(one-time download via `python -m playwright install <name>`). Each kind
gets its own one-time `example.com` sanity check (catches an install that
starts a process but can't load a page), cached **per kind** in
`~/.kodo/websearch/browser_state.json` — a kind that has already proven
itself is never re-checked, but a kind that has never been used gets its own
independent check.

### The `curl` backend

[`_curlfetch.py`](../src/kodo/websearch/_curlfetch.py) fetches with
`curl_cffi`, impersonating a current Chrome build's TLS/HTTP2 signature —
this passes some anti-bot checks a real headless browser doesn't (see
WEB_SEARCH.md §7) and needs no browser process at all. Since there is no
live DOM to evaluate JS in, [`_htmlextract.py`](../src/kodo/websearch/_htmlextract.py)
is a from-scratch Python port (using `selectolax`) of the same
wall-detection and `content_filter` extraction the browser path runs in-page
— deliberately a separate implementation, not shared, so a change to one
can't regress the other. One accepted gap: with no CSS engine, elements
hidden via `display:none`/`visibility:hidden` aren't detected, only
structurally-removed/`aria-hidden` markup is.

## 5. `content_filter` — how much to strip

| Value | Behavior |
|---|---|
| `off` | The page exactly as fetched — nothing removed. For the browser path this is the live DOM's current `outerHTML` (**after** the page's own scripts have run — pick `curl` if byte-for-byte source matters); for `curl` it's the raw response body. |
| `html` | The full page's HTML with only `<script>`/`<style>`/`<noscript>` removed — everything else (nav, forms, images, head/meta) intact. |
| `text` (default) | Content-root selection (`<article>`→`<main>`→`[role=main]`→`<body>`), chrome stripped (nav/header/footer/aside/forms/images/etc.), the remainder converted to Markdown: headings, tables, plain (non-numbered) lists, and embedded links `[text](url)` preserved. This is the tool's original, unchanged behavior. |

`text` mode prepends the page's `<title>` as a Markdown `#` heading (when
non-empty); `off`/`html` do not synthesize any heading — they return the
page's own markup as-is.

Length caps (a safety valve, not a quality signal, for `off`/`html`):
`text` is capped at 20,000 characters and gated by a "too thin" check (under
40 characters after extraction raises `AntiBotWallError` — usually a wall or
login gate the heuristics didn't recognize, or a JS-only app shell this tool
can't render); `off`/`html` are capped at 50,000 characters with no thinness
check, since the page's own content is whatever it is.

## 6. The tool's contract

Input: `url` (required, absolute `http`/`https`), `browser` (optional, see
§4), `headed` (optional boolean), `content_filter` (optional, see §5).

Output on success: `{"content": "..."}` — shaped per `content_filter`.

Output on failure (the universal `{"error": "..."}` envelope):

| Situation | `error` message advises |
|---|---|
| Bad scheme / private-network host | Names the problem (`InvalidUrlError`); no request was made. |
| Unsupported `browser`/`content_filter` value | Names the bad value; no request was made. |
| HTTP 403/429/503, captcha/anti-bot markup detected, or too-thin residual content (`text` mode only) | Explains what was detected, then: *"Do not retry this exact URL with the same browser — ... a different `browser` choice may succeed, or try a different source, or ask the user."* |
| Requested browser unavailable (host kind not installed, or a bundled kind's install failed) | `"read_webpage is unavailable: ..."` (`BrowserUnavailableError`) — no fallback to another kind. |
| Any other navigation/JS/request failure | `"Could not read {url}: {exc}"` — never raised past the tool boundary. |

## 7. Security posture

`read_webpage` is `SecurityImpact.LOW` and available in autonomous mode: it
is read-only toward the user's machine (its only writes are
`~/.kodo/websearch/browser_state.json` and Playwright's own browser cache),
and its SSRF guard (§3) keeps it from being used to probe the user's local
network. Fetched page content is untrusted input handed straight back to the
calling agent as data — there is no LLM synthesis step in this tool to
harden (unlike `web_search`'s agent, which treats fetched text strictly as
data per its own prompt).
