"""Single-page fetch + HTML→Markdown extraction for the ``read_webpage`` tool.

Independent of the ``web_search`` pipeline (:mod:`kodo.websearch._scrape`):
that module extracts *plain text* for the summarizer and is left untouched
here so this addition cannot regress it. :func:`read_page` instead converts
one page's main content root to Markdown — headings, tables, plain
(non-numbered) lists, and links preserved; images/video dropped — using the
same live-DOM-mutation philosophy (strip chrome, then let the browser's own
layout stand in for a full HTML parser).

Two failure modes are raised as distinct exceptions so the tool can shape its
``error`` message:

- :class:`InvalidUrlError` — the URL fails validation *before* any request is
  made (bad scheme, or the host resolves to a private/loopback/link-local
  address — an SSRF guard, since this tool fetches whatever URL the agent
  hands it, unlike ``web_search`` which only ever visits engine-discovered
  links).
- :class:`AntiBotWallError` — the request went out but the page is a
  captcha/anti-bot wall (or, after stripping chrome, yielded essentially no
  content — usually the same thing wearing a different hat: a JS-only app
  shell, a login wall, a rate-limit page). Unlike ``web_search``'s per-engine
  cooldown, there is no persisted backoff here: the caller is simply told not
  to retry the same URL.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from playwright.async_api import Browser
from playwright.async_api import Error as PlaywrightError

from ._models import PageMarkdown

__all__ = ["AntiBotWallError", "InvalidUrlError", "read_page", "validate_public_url"]

_log = logging.getLogger(__name__)

# Navigation budget for the target page.
_NAV_TIMEOUT_MS = 20_000
# Per-page character budget (keeps the tool result bounded).
_MAX_MARKDOWN_CHARS = 20_000
# Pages with less residual Markdown than this carry no usable content.
_MIN_MARKDOWN_CHARS = 40
# Schemes read_page will navigate to.
_ALLOWED_SCHEMES = frozenset({"http", "https"})
# HTTP statuses that mean "you are rate-limited / blocked" without a captcha page.
_BLOCKED_STATUSES = frozenset({403, 429, 503})


class InvalidUrlError(Exception):
    """Raised when *url* fails validation before any request is made."""


class AntiBotWallError(Exception):
    """Raised when the page is an anti-bot wall or yields no usable content."""


# Chrome stripped before extraction — the same categories _scrape.py removes
# for the web_search pipeline, plus media elements (images/video are dropped
# from read_webpage's output entirely, per the tool's contract).
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

# Converts the page's main content root to Markdown in-page: strips chrome,
# then walks the remaining DOM converting headings/tables/lists/links to
# Markdown syntax while flattening everything else to prose. Deliberately
# simple — no bold/italic/code-span handling — matching the tool's contract
# of "plain text with a few H-styles, simple non-numbered lists, tables, and
# embedded links."
_EXTRACT_MARKDOWN_JS = (
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

  function inline(node) {
    let out = '';
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        out += child.textContent;
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        if (isHidden(child)) continue;
        const tag = child.tagName;
        if (tag === 'BR') {
          out += ' ';
        } else if (tag === 'A') {
          const href = child.getAttribute('href');
          const text = inline(child).replace(/\\s+/g, ' ').trim();
          if (href && !href.startsWith('javascript:') && !href.startsWith('#') && text) {
            let abs;
            try {
              abs = new URL(href, document.baseURI).href;
            } catch (e) {
              abs = href;
            }
            out += `[${text}](${abs})`;
          } else {
            out += text;
          }
        } else {
          out += inline(child);
        }
      }
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
          // Wrapping <p>/<div> and plain inline elements flow into the
          // bullet's own text.
          inlineText += inline(child);
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
        buffer += inline(child);
      }
    }
    flush();
    return parts.join('\\n\\n');
  }

  return { title: (document.title || '').trim(), markdown: root ? walk(root) : '' };
}
"""
)


async def read_page(browser: Browser, url: str) -> PageMarkdown:
    """Fetch *url* and return its main content as Markdown.

    Callers that can validate *url* before paying for a browser launch (the
    ``read_webpage`` tool does, via :func:`validate_public_url`) should do so;
    this function re-validates regardless, so it stays safe to call directly.

    Args:
        browser: The caller's shared headless browser.
        url: Absolute ``http``/``https`` URL to fetch.

    Returns:
        PageMarkdown: The page's title and Markdown content.

    Raises:
        InvalidUrlError: *url* has a disallowed scheme, or its host resolves
            to a private/loopback/link-local/reserved address.
        AntiBotWallError: The page is a captcha/anti-bot wall, was blocked
            (HTTP 403/429/503), or yielded no usable content after stripping.
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
            raw = await page.evaluate(_EXTRACT_MARKDOWN_JS)
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
    markdown = _normalize_markdown(markdown)
    if len(markdown) < _MIN_MARKDOWN_CHARS:
        raise AntiBotWallError(
            "The page yielded almost no readable content after stripping "
            "navigation/ads/scripts; it may be gated behind an anti-bot check, "
            "a login wall, or a JavaScript-only app shell this tool can't render."
        )
    return PageMarkdown(url=url, title=title, markdown=markdown[:_MAX_MARKDOWN_CHARS])


async def validate_public_url(url: str) -> None:
    """Reject non-http(s) schemes and hosts resolving to a non-public address.

    A standalone SSRF guard (DNS lookup only, no browser) so callers like the
    ``read_webpage`` tool can reject a bad URL before launching Chromium.

    Raises:
        InvalidUrlError: *url* has a disallowed scheme, its host cannot be
            resolved, or a resolved address is private/loopback/link-local/
            reserved/unspecified.
    """
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise InvalidUrlError(
            f"Unsupported URL scheme {parts.scheme!r}; only http/https are allowed."
        )
    host = parts.hostname
    if not host:
        raise InvalidUrlError(f"URL {url!r} has no host.")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror as exc:
        raise InvalidUrlError(f"Could not resolve host {host!r}: {exc}") from exc
    for info in infos:
        try:
            addr = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError as exc:
            raise InvalidUrlError(f"Could not parse resolved address for {host!r}: {exc}") from exc
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise InvalidUrlError(
                f"URL host {host!r} resolves to a private/internal address ({addr}); "
                "read_webpage only fetches public internet pages."
            )


def _parse_extraction(raw: object) -> tuple[str, str]:
    """Pull ``(title, markdown)`` out of the extractor's payload, defensively."""
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
