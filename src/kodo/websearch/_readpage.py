"""Single-page fetch + extraction for the ``read_webpage`` tool, browser path.

Independent of the ``query_search_engine`` pipeline (:mod:`kodo.websearch._engines`)
and of the ``curl`` backend (:mod:`kodo.websearch._curlfetch` /
``_htmlextract``): this module is the Playwright-driven path used for every
``browser`` choice except ``"curl"``. It supports all three
``content_filter`` modes:

- ``"off"`` — the live DOM's current ``outerHTML``, unmodified (note: for a
  browser this is *after* the page's own scripts have run, not the exact
  network response bytes — pick ``browser: "curl"`` if byte-for-byte source
  matters).
- ``"html"`` — the same, with ``<script>``/``<style>``/``<noscript>``
  removed.
- ``"text"`` — content-root selection (``<article>``→``<main>``→
  ``[role=main]``→``<body>``), chrome stripped, DOM→Markdown walk (headings,
  tables, plain lists, links preserved) — this is the tool's original,
  still-default behavior.

Two failure modes are raised as distinct exceptions so the tool can shape its
``error`` message:

- :class:`~kodo.websearch._validate.InvalidUrlError` — the URL fails
  validation *before* any request is made (bad scheme, or the host resolves
  to a private/loopback/link-local address — an SSRF guard, since this tool
  fetches whatever URL the agent hands it).
- :class:`~kodo.websearch._validate.AntiBotWallError` — the request went out
  but the page is a captcha/anti-bot wall. Unlike ``query_search_engine``'s
  per-engine cooldown bookkeeping, this module raises and lets the caller
  (the ``web_search`` agent, or the tool handler for a direct
  ``read_webpage`` call) decide what to do about it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from playwright.async_api import Browser
from playwright.async_api import Error as PlaywrightError

from ._validate import AntiBotWallError, validate_public_url

__all__ = ["BrowserContent", "ContentFilter", "fetch_via_browser"]

_log = logging.getLogger(__name__)

ContentFilter = Literal["off", "html", "text"]

# Navigation budget for the target page.
_NAV_TIMEOUT_MS = 20_000
# HTTP statuses that mean "you are rate-limited / blocked" without a captcha page.
_BLOCKED_STATUSES = frozenset({403, 429, 503})

# Chrome stripped before "text"-mode extraction — script/style/UI/navigation
# chrome, plus media (images/video are dropped from read_webpage's text
# output entirely, per the tool's contract).
_REMOVE_SELECTORS = """[
    'script', 'style', 'noscript', 'template', 'svg', 'canvas', 'iframe',
    'nav', 'header', 'footer', 'aside', 'form', 'button', 'select', 'dialog',
    'img', 'picture', 'video', 'audio', 'source', 'track',
    '[role="navigation"]', '[role="banner"]', '[role="contentinfo"]',
    '[role="complementary"]', '[role="search"]', '[aria-hidden="true"]',
  ]"""

# Generic anti-bot / captcha wall detector. Unlike the per-engine `blocked_js`
# in _engines.py (which knows each search engine's specific wall markup),
# read_webpage visits arbitrary sites, so this looks for the vendor-agnostic
# signatures common to Cloudflare/reCAPTCHA/hCaptcha/PerimeterX-style walls.
# Mirrored (not shared) in _htmlextract.py's is_blocked() for the curl path.
_BLOCKED_JS = """
() => {
  if (document.querySelector(
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], #cf-challenge-running, ' +
    '.cf-browser-verification, #challenge-running, #challenge-form, .px-captcha-container'
  )) return true;
  const body = document.body ? document.body.innerText.slice(0, 1000) : '';
  const t = ((document.title || '') + ' ' + body).toLowerCase();
  const patterns = [
    'verify you are human',
    'verify that you are a human',
    'checking your browser before accessing',
    'unusual traffic from your computer',
    'are you a robot',
    'bot detection',
    'just a moment...',
    'ddos protection by',
    'attention required! | cloudflare',
    'access denied',
    'please enable javascript and cookies',
    'to continue, please verify',
  ];
  return patterns.some((p) => t.includes(p));
}
"""

# content_filter: "off" — the live DOM's current outerHTML, untouched.
_EXTRACT_OFF_JS = "() => document.documentElement.outerHTML"

# content_filter: "html" — same, with script/style/noscript removed.
_EXTRACT_HTML_JS = """
() => {
  for (const el of document.querySelectorAll('script, style, noscript')) el.remove();
  return document.documentElement.outerHTML;
}
"""

# content_filter: "text" — converts the page's main content root to Markdown
# in-page: strips chrome, then walks the remaining DOM converting
# headings/tables/lists/links to Markdown syntax while flattening everything
# else to prose. Deliberately simple — no bold/italic/code-span handling —
# matching the tool's contract of "plain text with a few H-styles, simple
# non-numbered lists, tables, and embedded links."
_EXTRACT_TEXT_JS = (
    """
() => {
  const REMOVE = """
    + _REMOVE_SELECTORS
    + """;
  for (const sel of REMOVE) {
    for (const el of document.querySelectorAll(sel)) el.remove();
  }

  const root =
    document.querySelector('article') ||
    document.querySelector('main') ||
    document.querySelector('[role="main"]') ||
    document.body;

  const BLOCK_TAGS = new Set([
    'P', 'DIV', 'SECTION', 'ARTICLE', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
    'UL', 'OL', 'TABLE', 'BLOCKQUOTE', 'PRE', 'HR', 'LI',
  ]);

  function isHidden(el) {
    if (!(el instanceof Element)) return false;
    const style = window.getComputedStyle(el);
    return style.display === 'none' || style.visibility === 'hidden';
  }

  // Renders one non-block node (and its subtree) as inline text/markdown.
  // Handling `<a>`/`<br>` here -- not only when nested a level deeper inside
  // a wrapper -- matters because a link with no wrapping inline element
  // (e.g. `<p>Hello <a href="/x">link</a></p>`, the common case) is passed
  // to this function *as the node itself* from walk()'s non-block branch.
  function inlineOne(node) {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    if (isHidden(node)) return '';
    const tag = node.tagName;
    if (tag === 'BR') return ' ';
    if (tag === 'A') {
      const href = node.getAttribute('href');
      const text = inline(node).replace(/\\s+/g, ' ').trim();
      if (href && !href.startsWith('javascript:') && !href.startsWith('#') && text) {
        let abs;
        try {
          abs = new URL(href, document.baseURI).href;
        } catch (e) {
          abs = href;
        }
        return `[${text}](${abs})`;
      }
      return text;
    }
    return inline(node);
  }

  function inline(node) {
    let out = '';
    for (const child of node.childNodes) {
      out += inlineOne(child);
    }
    return out;
  }

  function cellText(cell) {
    return inline(cell).replace(/\\s+/g, ' ').trim().replace(/\\|/g, '\\\\|');
  }

  function tableToMarkdown(table) {
    const rows = Array.from(table.rows).filter((r) => !isHidden(r));
    if (!rows.length) return '';
    const grid = rows.map((r) => Array.from(r.cells).filter((c) => !isHidden(c)).map(cellText));
    const cols = Math.max(...grid.map((r) => r.length));
    if (!cols) return '';
    const pad = (r) => {
      while (r.length < cols) r.push('');
      return r;
    };
    const lines = [];
    lines.push('| ' + pad(grid[0]).join(' | ') + ' |');
    lines.push('| ' + Array(cols).fill('---').join(' | ') + ' |');
    for (let i = 1; i < grid.length; i++) {
      lines.push('| ' + pad(grid[i]).join(' | ') + ' |');
    }
    return lines.join('\\n');
  }

  function listToMarkdown(list, depth) {
    const indent = '  '.repeat(depth);
    const entries = [];
    let hasBlocks = false;
    for (const li of list.children) {
      if (li.tagName !== 'LI' || isHidden(li)) continue;
      let inlineText = '';
      const blocks = [];
      for (const child of li.childNodes) {
        if (child.nodeType === Node.TEXT_NODE) {
          inlineText += child.textContent;
          continue;
        }
        if (child.nodeType !== Node.ELEMENT_NODE) continue;
        if (isHidden(child)) continue;
        const tag = child.tagName;
        if (tag === 'UL' || tag === 'OL') {
          const nested = listToMarkdown(child, depth + 1);
          if (nested) blocks.push(nested);
        } else if (tag === 'PRE' || tag === 'TABLE' || tag === 'BLOCKQUOTE') {
          const md = blockToMarkdown(child);
          if (md) blocks.push(md);
        } else if (child.querySelector('pre, table, blockquote, ul, ol')) {
          // A wrapper (e.g. a syntax-highlighter's <div class="highlight">)
          // hiding one of the above deeper in its subtree — recurse block-
          // aware instead of flattening it into the bullet's inline text.
          const md = walk(child);
          if (md) blocks.push(md);
        } else {
          // Wrapping <p>/<div> and plain inline elements (including a bare
          // <a>) flow into the bullet's own text.
          inlineText += inlineOne(child);
        }
      }
      inlineText = inlineText.replace(/\\s+/g, ' ').trim();
      if (!inlineText && !blocks.length) continue;
      let entry = inlineText ? `${indent}- ${inlineText}` : `${indent}-`;
      if (blocks.length) {
        hasBlocks = true;
        entry += '\\n\\n' + blocks.join('\\n\\n');
      }
      entries.push(entry);
    }
    return entries.join(hasBlocks ? '\\n\\n' : '\\n');
  }

  function blockToMarkdown(el) {
    if (isHidden(el)) return '';
    const tag = el.tagName;
    if (/^H[1-6]$/.test(tag)) {
      const level = Number(tag[1]);
      const text = inline(el).replace(/\\s+/g, ' ').trim();
      return text ? '#'.repeat(level) + ' ' + text : '';
    }
    if (tag === 'TABLE') return tableToMarkdown(el);
    if (tag === 'UL' || tag === 'OL') return listToMarkdown(el, 0);
    if (tag === 'PRE') {
      const text = (el.innerText || el.textContent || '').trim();
      return text ? '```\\n' + text + '\\n```' : '';
    }
    if (tag === 'BLOCKQUOTE') {
      const text = inline(el).replace(/\\s+/g, ' ').trim();
      return text ? '> ' + text : '';
    }
    if (tag === 'HR') return '---';
    return walk(el);
  }

  function walk(node) {
    const parts = [];
    let buffer = '';
    const flush = () => {
      const text = buffer.replace(/\\s+/g, ' ').trim();
      if (text) parts.push(text);
      buffer = '';
    };
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        buffer += child.textContent;
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) continue;
      if (isHidden(child)) continue;
      const tag = child.tagName;
      if (BLOCK_TAGS.has(tag)) {
        flush();
        const md = blockToMarkdown(child);
        if (md) parts.push(md);
      } else {
        buffer += inlineOne(child);
      }
    }
    flush();
    return parts.join('\\n\\n');
  }

  return { title: (document.title || '').trim(), markdown: root ? walk(root) : '' };
}
"""
)


@dataclass(frozen=True)
class BrowserContent:
    """One page fetched + extracted via a Playwright browser.

    Attributes:
        title: The page's ``document.title`` — only meaningful for
            ``content_filter: "text"`` (``""`` for ``"off"``/``"html"``,
            whose ``content`` already carries the page's own ``<title>``).
        content: The extracted content, shaped per the requested
            ``content_filter`` (raw/stripped HTML, or Markdown body without
            a synthesized title heading — the caller prepends one).
    """

    title: str
    content: str


async def fetch_via_browser(
    browser: Browser, url: str, content_filter: ContentFilter
) -> BrowserContent:
    """Fetch *url* and extract its content per *content_filter*.

    Callers that can validate *url* before paying for a browser launch (the
    ``read_webpage`` tool does, via
    :func:`~kodo.websearch._validate.validate_public_url`) should do so; this
    function re-validates regardless, so it stays safe to call directly.

    Args:
        browser: The caller's already-launched browser (any kind but curl —
            curl never touches this module).
        url: Absolute ``http``/``https`` URL to fetch.
        content_filter: Which extraction to run.

    Returns:
        BrowserContent: Unbounded — the caller applies its own length cap
            and "too thin" quality gate (policy that's shared with the curl
            backend, so it lives once in the tool handler, not duplicated
            per backend).

    Raises:
        InvalidUrlError: *url* has a disallowed scheme, or its host resolves
            to a private/loopback/link-local/reserved address.
        AntiBotWallError: The page is a captcha/anti-bot wall, or was
            blocked (HTTP 403/429/503).
    """
    await validate_public_url(url)

    context = await browser.new_context(locale="en-US")
    try:
        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
            if response is not None and response.status in _BLOCKED_STATUSES:
                raise AntiBotWallError(
                    f"The page responded with HTTP {response.status}, typical of an "
                    "anti-bot or rate-limit wall."
                )
            if bool(await page.evaluate(_BLOCKED_JS)):
                raise AntiBotWallError(
                    "The page appears to be an anti-bot/captcha wall (e.g. a "
                    "Cloudflare, reCAPTCHA, or hCaptcha challenge)."
                )
            if content_filter == "off":
                raw_html = await page.evaluate(_EXTRACT_OFF_JS)
                return BrowserContent(
                    title="", content=raw_html if isinstance(raw_html, str) else ""
                )
            if content_filter == "html":
                raw_html = await page.evaluate(_EXTRACT_HTML_JS)
                return BrowserContent(
                    title="", content=raw_html if isinstance(raw_html, str) else ""
                )
            raw = await page.evaluate(_EXTRACT_TEXT_JS)
        finally:
            try:
                await page.close()
            except PlaywrightError:
                _log.debug("Page close failed for %s", url, exc_info=True)
    finally:
        try:
            await context.close()
        except PlaywrightError:
            _log.debug("Context close failed for %s", url, exc_info=True)

    title, markdown = _parse_extraction(raw)
    return BrowserContent(title=title, content=_normalize_markdown(markdown))


def _parse_extraction(raw: object) -> tuple[str, str]:
    """Pull ``(title, markdown)`` out of the "text"-mode extractor's payload."""
    if not isinstance(raw, dict):
        return "", ""
    title = raw.get("title")
    markdown = raw.get("markdown")
    return (
        title if isinstance(title, str) else "",
        markdown if isinstance(markdown, str) else "",
    )


def _normalize_markdown(markdown: str) -> str:
    """Trim trailing whitespace per line and collapse runs of 3+ blank lines."""
    lines = [line.rstrip() for line in markdown.splitlines()]
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line:
            blank_run = 0
            out.append(line)
        else:
            blank_run += 1
            if blank_run <= 1:
                out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)
