"""The four search-engine adapters: query URL + in-page extraction JS.

Each :class:`Engine` is pure data — a results-page URL template plus two
JavaScript snippets evaluated *in the results page*: one that detects an
anti-bot / captcha wall, and one that extracts the organic hits (skipping
sponsored results and ads). Keeping the page knowledge as JS means the Python
side never parses HTML: the browser's own DOM does the work.

The extractors return ``[{url, title, snippet}]`` in on-page order (top result
first). They are deliberately conservative: unrecognized layouts yield an empty
list, which discovery reports as an engine *error*, not a block.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus, urlsplit

__all__ = ["SEARCH_ENGINES", "SearchEngine", "is_engine_internal"]

# Hosts that are engine-internal (support pages, image/maps verticals, the
# DDG redirector) — never useful as a query_search_engine hit. wikipedia.org
# is deliberately NOT here: the wikipedia engine's own hits (and plenty of
# legitimate hits from the other engines) ARE wikipedia.org articles.
_ENGINE_HOST_MARKERS = ("google.", "bing.com", "duckduckgo.com", "microsoft.com/en-us/bing")


def is_engine_internal(url: str) -> bool:
    """``True`` for links pointing back into a search engine's own properties."""
    host = urlsplit(url).netloc.lower()
    return any(marker in host for marker in _ENGINE_HOST_MARKERS)


@dataclass(frozen=True)
class SearchEngine:
    """One search engine the discovery phase can query.

    Attributes:
        name: Short identifier (also the cooldown-store key).
        url_template: Results-page URL with a ``{query}`` placeholder
            (URL-encoded query is substituted in).
        ready_selector: CSS selector that appears once organic results are in
            the DOM. Awaited (briefly, tolerating a timeout) before extraction,
            because some engines hydrate results *after* ``domcontentloaded``
            — Bing observably serves ``li.b_algo`` entries a beat later.
        blocked_js: JS evaluated on the loaded results page; returns ``true``
            when the page is an anti-bot / captcha wall.
        extract_js: JS evaluated on the loaded results page; returns the
            organic hits as ``[{url, title, snippet}]``, ads excluded.
    """

    name: str
    url_template: str
    ready_selector: str
    blocked_js: str
    extract_js: str

    def search_url(self, query: str) -> str:
        """The results-page URL for *query*."""
        return self.url_template.format(query=quote_plus(query))


_GOOGLE = SearchEngine(
    name="google",
    url_template="https://www.google.com/search?q={query}&num=20&hl=en",
    ready_selector="#search h3, #rso h3",
    # Google's wall is the /sorry/ interstitial or an embedded reCAPTCHA form.
    blocked_js="""
() => {
  if (location.pathname.startsWith('/sorry')) return true;
  return !!document.querySelector(
    '#captcha-form, form[action*="/sorry/"], iframe[src*="recaptcha"], #recaptcha'
  );
}
""",
    # Organic hits are the h3 headings inside the results container; anything
    # inside an ad block (#tads/#bottomads/[data-text-ad]) is skipped. /url?
    # redirect wrappers are unwrapped to the target URL.
    extract_js="""
() => {
  const out = [];
  const seen = new Set();
  for (const h3 of document.querySelectorAll('#search h3, #rso h3')) {
    const a = h3.closest('a');
    if (!a || !a.href) continue;
    if (a.closest('#tads, #bottomads, [data-text-ad], .commercial-unit-desktop-top')) continue;
    let href = a.href;
    try {
      const u = new URL(href);
      if (u.pathname === '/url' && u.searchParams.get('q')) href = u.searchParams.get('q');
    } catch (e) { continue; }
    if (!/^https?:\\/\\//.test(href) || seen.has(href)) continue;
    seen.add(href);
    const box = a.closest('[data-hveid], .g, .MjjYud') || a;
    const snip = box.querySelector('[data-sncf], .VwiC3b');
    out.push({
      url: href,
      title: (h3.innerText || h3.textContent || '').trim(),
      snippet: snip ? (snip.innerText || snip.textContent || '').trim() : '',
    });
  }
  return out;
}
""",
)

_BING = SearchEngine(
    name="bing",
    url_template="https://www.bing.com/search?q={query}&count=20",
    ready_selector="#b_results > li.b_algo",
    # Bing's wall is a "verify you are human" challenge page.
    blocked_js="""
() => {
  if (document.querySelector('#b_captcha, .b_captcha, iframe[src*="challenge"]')) return true;
  const body = document.body ? document.body.innerText.slice(0, 500) : '';
  const t = (document.title || '') + ' ' + body;
  return /verify(ing)?\\s+(that\\s+)?you('re| are)?\\s*(a\\s+)?human/i.test(t);
}
""",
    # Organic hits are li.b_algo entries; ads live in li.b_ad and are never
    # matched by this selector. Result links are wrapped in a
    # /ck/a?…&u=a1<base64url> click-tracking redirect that must be decoded.
    extract_js="""
() => {
  const unwrap = (href) => {
    try {
      const u = new URL(href);
      if (!u.hostname.endsWith('bing.com') || !u.pathname.startsWith('/ck/')) return href;
      const p = u.searchParams.get('u') || '';
      if (!p.startsWith('a1')) return href;
      const b64 = p.slice(2).replace(/-/g, '+').replace(/_/g, '/');
      return atob(b64 + '='.repeat((4 - (b64.length % 4)) % 4));
    } catch (e) { return href; }
  };
  const out = [];
  for (const li of document.querySelectorAll('#b_results > li.b_algo')) {
    const a = li.querySelector('h2 a');
    if (!a || !a.href) continue;
    const url = unwrap(a.href);
    if (!/^https?:\\/\\//.test(url)) continue;
    const snip = li.querySelector('.b_caption p, p');
    out.push({
      url,
      title: (a.innerText || a.textContent || '').trim(),
      snippet: snip ? (snip.innerText || snip.textContent || '').trim() : '',
    });
  }
  return out;
}
""",
)

_DUCKDUCKGO = SearchEngine(
    name="duckduckgo",
    # The plain-HTML endpoint: no JS app shell, stable markup.
    url_template="https://html.duckduckgo.com/html/?q={query}",
    ready_selector="div.result",
    # DDG's wall is the "anomaly" page ("bots use DuckDuckGo too").
    blocked_js="""
() => {
  if (document.querySelector('.anomaly-modal, form[action*="challenge"]')) return true;
  const t = document.body ? document.body.innerText.slice(0, 500) : '';
  return /unfortunately,?\\s+bots\\s+use\\s+duckduckgo/i.test(t);
}
""",
    # Organic hits are div.result blocks minus the result--ad ones; result
    # links are wrapped in a /l/?uddg=<url> redirect that must be decoded.
    extract_js="""
() => {
  const out = [];
  for (const div of document.querySelectorAll('div.result')) {
    if (div.className.includes('result--ad') || div.querySelector('.badge--ad')) continue;
    const a = div.querySelector('a.result__a');
    if (!a || !a.href) continue;
    let href = a.href;
    try {
      const u = new URL(href, location.href);
      const uddg = u.searchParams.get('uddg');
      if (uddg) href = decodeURIComponent(uddg);
    } catch (e) { continue; }
    if (!/^https?:\\/\\//.test(href)) continue;
    const snip = div.querySelector('.result__snippet');
    out.push({
      url: href,
      title: (a.innerText || a.textContent || '').trim(),
      snippet: snip ? (snip.innerText || snip.textContent || '').trim() : '',
    });
  }
  return out;
}
""",
)

_WIKIPEDIA = SearchEngine(
    name="wikipedia",
    # English Wikipedia's full-text search results page. fulltext=1 forces a
    # results *list* (an exact title match would otherwise redirect straight to
    # the article); ns0=1 restricts to the article namespace.
    url_template="https://en.wikipedia.org/w/index.php?search={query}&fulltext=1&ns0=1&limit=20",
    ready_selector="li.mw-search-result, p.mw-search-nonefound",
    # Wikipedia serves no reader-facing captcha; rate limiting arrives as HTTP
    # 403/429 and is caught by the generic status check in discovery.
    blocked_js="""
() => false
""",
    # Organic hits are the li.mw-search-result entries (no ads on Wikipedia);
    # links are plain absolute article URLs, no redirect wrappers.
    extract_js="""
() => {
  const out = [];
  for (const li of document.querySelectorAll('li.mw-search-result')) {
    const a = li.querySelector('.mw-search-result-heading a');
    if (!a || !a.href) continue;
    if (!/^https?:\\/\\//.test(a.href)) continue;
    const snip = li.querySelector('.searchresult');
    out.push({
      url: a.href,
      title: (a.innerText || a.textContent || '').trim(),
      snippet: snip ? (snip.innerText || snip.textContent || '').trim() : '',
    });
  }
  return out;
}
""",
)

# Discovery queries these in parallel; the tuple order is also the merge
# (interleave) order, so it encodes a mild preference: google, bing,
# duckduckgo, wikipedia.
SEARCH_ENGINES: tuple[SearchEngine, ...] = (_GOOGLE, _BING, _DUCKDUCKGO, _WIKIPEDIA)
