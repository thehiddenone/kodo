"""Static (curl-backend) organic-hit extraction for ``query_search_engine``.

A from-scratch Python port of ``_engines.py``'s per-engine wall-detection and
organic-hit JS, using :mod:`selectolax` over the raw HTML `curl_cffi` fetches
instead of evaluating JS in a live DOM. Same reasoning as ``_htmlextract.py``:
a deliberately separate implementation, not a shared abstraction, so a
change to one path can't silently regress the other — and the same caveat
applies here as there: no CSS engine means ``display:none`` elements aren't
detected, only structurally-hidden/absent markup is.

URL templates are **not** duplicated — :func:`search_url` reuses
``_engines.py``'s ``SEARCH_ENGINES`` templates, so there is exactly one
source of truth for "what URL does querying engine X look like."
"""

from __future__ import annotations

import base64
import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from selectolax.parser import HTMLParser, Node

from ._engines import SEARCH_ENGINES, is_engine_internal

__all__ = ["extract_hits", "is_blocked", "search_url"]

_HTTP_SCHEMES = ("http://", "https://")


def search_url(engine: str, query: str) -> str:
    """The results-page URL for *engine* (reuses ``_engines.py``'s template)."""
    for candidate in SEARCH_ENGINES:
        if candidate.name == engine:
            return candidate.search_url(query)
    raise ValueError(f"Unknown engine {engine!r}")


def is_blocked(engine: str, html: str) -> bool:
    """``True`` when *html* is an anti-bot/captcha wall for *engine*."""
    tree = HTMLParser(html)
    return _BLOCKED_CHECKS[engine](tree)


def extract_hits(engine: str, html: str, base_url: str) -> list[dict[str, str]]:
    """Organic hits ``[{url, title, snippet}]`` for *engine*, ads/internal links skipped."""
    tree = HTMLParser(html)
    hits: list[dict[str, str]] = []
    seen: set[str] = set()
    for url, title, snippet in _EXTRACTORS[engine](tree, base_url):
        if url in seen or is_engine_internal(url):
            continue
        seen.add(url)
        hits.append({"url": url, "title": title, "snippet": snippet})
    return hits


def _text(node: Node | None) -> str:
    return node.text(separator=" ", strip=True) if node is not None else ""


def _closest(node: Node | None, selector: str) -> Node | None:
    """Nearest ancestor-or-self matching *selector* (mirrors JS ``Element.closest``)."""
    cur = node
    while cur is not None:
        if cur.tag != "-text" and cur.css_matches(selector):
            return cur
        cur = cur.parent
    return None


def _abs_http_url(href: str | None, base_url: str) -> str | None:
    """Resolve *href* against *base_url*; ``None`` unless the result is http(s)."""
    if not href:
        return None
    resolved = urljoin(base_url, href)
    return resolved if resolved.startswith(_HTTP_SCHEMES) else None


# ---------------------------------------------------------------------------
# google
# ---------------------------------------------------------------------------


def _google_blocked(tree: HTMLParser) -> bool:
    return (
        tree.css_first(
            "#captcha-form, form[action*='/sorry/'], iframe[src*='recaptcha'], #recaptcha"
        )
        is not None
    )


def _google_extract(tree: HTMLParser, base_url: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for h3 in tree.css("#search h3, #rso h3"):
        a = _closest(h3, "a")
        if a is None:
            continue
        if (
            _closest(a, "#tads, #bottomads, [data-text-ad], .commercial-unit-desktop-top")
            is not None
        ):
            continue
        href = a.attributes.get("href")
        url = _abs_http_url(href, base_url)
        if url is None:
            continue
        # Unwrap Google's /url?q=<target> redirect wrapper.
        parsed = urlsplit(url)
        if parsed.path == "/url":
            target = parse_qs(parsed.query).get("q", [""])[0]
            if target:
                url = target
        box = _closest(a, "[data-hveid], .g, .MjjYud") or a
        snippet = _text(box.css_first("[data-sncf], .VwiC3b"))
        out.append((url, _text(h3), snippet))
    return out


# ---------------------------------------------------------------------------
# bing
# ---------------------------------------------------------------------------


def _bing_blocked(tree: HTMLParser) -> bool:
    if tree.css_first("#b_captcha, .b_captcha, iframe[src*='challenge']") is not None:
        return True
    haystack = f"{_text(tree.css_first('title'))} {_text(tree.body)[:500]}"
    return bool(
        re.search(r"verify(ing)?\s+(that\s+)?you('re| are)?\s*(a\s+)?human", haystack, re.I)
    )


def _unwrap_bing(url: str) -> str:
    """Decode Bing's ``/ck/a?...&u=a1<base64url>`` click-tracking redirect."""
    parsed = urlsplit(url)
    if not parsed.netloc.endswith("bing.com") or not parsed.path.startswith("/ck/"):
        return url
    param = parse_qs(parsed.query).get("u", [""])[0]
    if not param.startswith("a1"):
        return url
    b64 = param[2:].replace("-", "+").replace("_", "/")
    b64 += "=" * ((4 - len(b64) % 4) % 4)
    try:
        return base64.b64decode(b64).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return url


def _bing_extract(tree: HTMLParser, base_url: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for li in tree.css("#b_results > li.b_algo"):
        a = li.css_first("h2 a")
        if a is None:
            continue
        href = a.attributes.get("href")
        url = _abs_http_url(href, base_url)
        if url is None:
            continue
        url = _unwrap_bing(url)
        if not url.startswith(_HTTP_SCHEMES):
            continue
        snippet = _text(li.css_first(".b_caption p, p"))
        out.append((url, _text(a), snippet))
    return out


# ---------------------------------------------------------------------------
# duckduckgo
# ---------------------------------------------------------------------------


def _duckduckgo_blocked(tree: HTMLParser) -> bool:
    if tree.css_first(".anomaly-modal, form[action*='challenge']") is not None:
        return True
    return bool(
        re.search(r"unfortunately,?\s+bots\s+use\s+duckduckgo", _text(tree.body)[:500], re.I)
    )


def _duckduckgo_extract(tree: HTMLParser, base_url: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for div in tree.css("div.result"):
        classes = div.attributes.get("class") or ""
        if "result--ad" in classes or div.css_first(".badge--ad") is not None:
            continue
        a = div.css_first("a.result__a")
        if a is None:
            continue
        href = a.attributes.get("href")
        url = _abs_http_url(href, base_url)
        if url is None:
            continue
        uddg = parse_qs(urlsplit(url).query).get("uddg", [""])[0]
        if uddg:
            url = unquote(uddg)
        if not url.startswith(_HTTP_SCHEMES):
            continue
        snippet = _text(div.css_first(".result__snippet"))
        out.append((url, _text(a), snippet))
    return out


# ---------------------------------------------------------------------------
# wikipedia
# ---------------------------------------------------------------------------


def _wikipedia_blocked(tree: HTMLParser) -> bool:  # noqa: ARG001 — no reader-facing captcha
    return False


def _wikipedia_extract(tree: HTMLParser, base_url: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for li in tree.css("li.mw-search-result"):
        a = li.css_first(".mw-search-result-heading a")
        if a is None:
            continue
        url = _abs_http_url(a.attributes.get("href"), base_url)
        if url is None:
            continue
        snippet = _text(li.css_first(".searchresult"))
        out.append((url, _text(a), snippet))
    return out


_BLOCKED_CHECKS = {
    "google": _google_blocked,
    "bing": _bing_blocked,
    "duckduckgo": _duckduckgo_blocked,
    "wikipedia": _wikipedia_blocked,
}

_EXTRACTORS = {
    "google": _google_extract,
    "bing": _bing_extract,
    "duckduckgo": _duckduckgo_extract,
    "wikipedia": _wikipedia_extract,
}
