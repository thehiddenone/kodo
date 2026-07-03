# Read Webpage — How the `read_webpage` Tool Works

> From a URL to Markdown: the single-page fetch-and-convert behind the
> `read_webpage` tool, its SSRF guard, and its anti-bot failure behavior.

Companion to [WEB_SEARCH.md](WEB_SEARCH.md) (the sibling `web_search`
pipeline, which shares the same `kodo.websearch` package and browser
lifecycle but nothing else) and [TOOLS.md](TOOLS.md) (tool subsystem
mechanics). The tool is currently granted only to the Problem Solver's
`investigator` sub-agent.

---

## 1. Overview

One `read_webpage` call does one thing: fetch a caller-given URL and return
its main content as Markdown.

```text
                url
                 │
                 ▼
 ┌─ Validate ──────────────────────────────────────────────────────────────┐
 │  scheme must be http/https; host must NOT resolve to a private/         │
 │  loopback/link-local/reserved address (SSRF guard — §3)                 │
 └───────────────────────────────────────────────────────────────────────┘
                 │
                 ▼
 ┌─ Fetch + convert (kodo.websearch.read_page) ─────────────────────────────┐
 │  one headless-browser page, 20s nav budget                              │
 │  strip nav/header/footer/aside/ads/scripts/images/video in-page          │
 │  walk the remaining DOM → Markdown: headings, tables, plain lists,       │
 │  embedded links kept                                                     │
 └───────────────────────────────────────────────────────────────────────┘
                 │
                 ▼
        {"markdown": "# Title\n\n..."}     or     {"error": "..."}
```

Unlike `web_search`, there is no discovery phase (the URL is given), no
summarization phase (the whole page's content is the point), and **no
cooldown**: a page that walls the tool off just returns an `error` for that
one call — repeating the call will fail the same way.

## 2. The pieces and where they live

| Piece | Location | Layer |
|---|---|---|
| Tool spec (`READ_WEBPAGE`) | [toolspecs/_read_webpage.py](../src/kodo/toolspecs/_read_webpage.py) | T2 |
| Tool handler (`ReadWebpageTool`) | [tools/_read_webpage.py](../src/kodo/tools/_read_webpage.py) | T3 (`kodo.tools`) |
| Fetch + Markdown conversion (`read_page`) | [kodo/websearch/_readpage.py](../src/kodo/websearch/_readpage.py) | **T0 leaf** — imports nothing from `kodo`; Playwright + stdlib only |
| Shared value object (`PageMarkdown`) | [kodo/websearch/_models.py](../src/kodo/websearch/_models.py) | T0 leaf |
| Browser lifecycle (`BrowserSession`) | [kodo/websearch/_browser.py](../src/kodo/websearch/_browser.py) | T0 leaf — **shared with `web_search`** |

`_readpage.py` is deliberately independent of `_scrape.py` (`web_search`'s
plain-text extractor): they solve different problems — plain text for an LLM
summarizer vs. structured Markdown for the calling agent to read directly —
and keeping them separate means a change to one cannot regress the other.
The only thing the two tools share is `BrowserSession` (Playwright's
host-first, bundled-fallback browser lifecycle — WEB_SEARCH.md §7) and the
`kodo.websearch` package boundary.

## 3. The SSRF guard

`web_search` never validates the URLs it scrapes because they all come from a
search engine's own results page — the caller (the agent) never names a raw
URL. `read_webpage` breaks that assumption: the agent hands it an arbitrary
URL directly, which on a tool that runs a real browser on the user's machine
is a direct SSRF vector (probing `localhost`, a LAN service, or a cloud
metadata endpoint like `169.254.169.254`).

Before any navigation, `_validate_public_url`:

1. Rejects any scheme other than `http`/`https`.
2. Resolves the hostname (`socket.getaddrinfo`) and rejects the URL if **any**
   resolved address is loopback, private, link-local, multicast, reserved, or
   unspecified (`ipaddress.ip_address(...).is_*`).

A rejection raises `InvalidUrlError`. The tool (`ReadWebpageTool.handle`) calls
`validate_public_url` **before** opening a `BrowserSession` at all, so a bad
URL never pays for a browser launch; `read_page` re-runs the same check
internally regardless, so it stays safe for any other caller. This is a
best-effort guard (a single DNS check, not re-validated against the IP the
browser actually connects to), matching the project's general non-paranoid
security stance — it stops casual misuse, not a determined DNS-rebinding
attacker.

## 4. Fetch + Markdown conversion

[`kodo/websearch/_readpage.py`](../src/kodo/websearch/_readpage.py).

One page, one navigation (20s budget, `domcontentloaded`), in a fresh
browser context:

1. **Status/wall check.** An HTTP 403/429/503 response, or a generic
   anti-bot/captcha heuristic evaluated in-page (`_BLOCKED_JS` — looks for
   reCAPTCHA/hCaptcha iframes, Cloudflare challenge markup, and common wall
   phrases like "verify you are human" / "just a moment..." / "ddos
   protection by"), raises `AntiBotWallError`. This is vendor-agnostic by
   necessity: `read_webpage` visits arbitrary sites, not the four known
   search engines `web_search`'s `_engines.py` has bespoke detectors for.
2. **Chrome removal.** The same category of elements `_scrape.py` strips for
   `web_search` (`script`/`style`/`nav`/`header`/`footer`/`aside`/`form`/
   navigation-role ARIA chrome/`[aria-hidden]`), **plus** `img`/`picture`/
   `video`/`audio`/`source`/`track` — images and video are dropped entirely,
   per the tool's contract. (Duplicated rather than imported from
   `_scrape.py` so the two extractors stay fully decoupled.)
3. **Content root.** Same priority as `web_search`: `<article>` → `<main>` →
   `[role=main]` → `<body>`.
4. **Markdown walk.** A small recursive DOM walker (not innerText) converts
   the remaining tree:
   - `h1`–`h6` → `#`…`######` headings.
   - `ul`/`ol` → **plain, non-numbered** `-` bullets at any nesting depth
     (ordered lists are deliberately flattened to bullets too — the tool's
     contract calls for "simple non-numbered lists").
   - `table` → a Markdown pipe table (first row as header, `---` separator).
   - `a[href]` → inline `[text](url)`, resolved to an absolute URL via
     `document.baseURI`; anchors with no href, a `#` fragment, or a
     `javascript:` target are inlined as plain text.
   - `pre` → a fenced ```` ``` ```` code block (its rendered `innerText`, so
     internal line breaks survive).
   - `blockquote` → `>` prefix.
   - `hr` → `---`.
   - Everything else is flattened to prose and joined with blank lines
     between blocks.
5. **Python-side normalization.** Trailing whitespace trimmed per line, runs
   of 3+ blank lines collapsed to one, then truncated to **20,000 characters**
   (`_MAX_MARKDOWN_CHARS` — a single-page budget, much larger than
   `web_search`'s per-source 6,000-char cap since this tool's whole point is
   one page in full).
6. **Too-thin check.** Markdown under 40 characters (`_MIN_MARKDOWN_CHARS`)
   after normalization also raises `AntiBotWallError` — in practice this is
   usually a wall or login gate the heuristics above didn't recognize, or a
   JavaScript-only app shell this tool can't render (no `wait_until:
   "networkidle"`, matching `web_search`'s `domcontentloaded`-only stance).

On success the tool prepends the page's `document.title` as an `#` heading
(when non-empty) ahead of the extracted Markdown body.

## 5. The tool's contract

Input: `url` (required, absolute `http`/`https`).

Output on success:

```json
{"markdown": "# Page Title\n\n## Section\n\nSome text with a [link](https://example.com).\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n"}
```

Output on failure (the universal `{"error": "..."}` envelope — no separate
`note` field, unlike `web_search`):

| Situation | `error` message advises |
|---|---|
| Bad scheme / private-network host | Names the problem (`InvalidUrlError`); no request was made. |
| HTTP 403/429/503, captcha/anti-bot markup detected, or too-thin residual content | Explains what was detected, then: *"Do not retry this exact URL — unlike web_search there is no cooldown here, so an immediate retry will fail the same way; try a different source or ask the user."* |
| No browser available (host Chrome/Edge and bundled Firefox/Chromium fallback all failed) | `"read_webpage is unavailable: ..."` (same `BrowserUnavailableError` as `web_search`). |
| Any other navigation/JS failure | `"Could not read {url}: {exc}"` — never raised past the tool boundary. |

## 6. Security posture

`read_webpage` is `SecurityImpact.LOW` and available in autonomous mode: like
`web_search`, its only side effect toward the user's machine is Playwright's
own browser cache, and its SSRF guard (§3) keeps it from being used to probe
the user's local network. Fetched page content is untrusted input handed
straight back to the calling agent as data (there is no LLM summarization
step in this tool to harden, unlike `web_search`'s `web_summarizer`).
