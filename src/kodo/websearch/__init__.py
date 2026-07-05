"""Playwright- and ``curl_cffi``-backed web access for ``read_webpage`` and
``query_search_engine``.

A **T0 leaf package** (imports nothing from ``kodo``). Three fetch backends
share the SSRF guard (:mod:`._validate`) but are otherwise fully decoupled —
a change to one cannot regress another:

- **Browser** (:mod:`._browser`, :mod:`._readpage`, :mod:`._engines`) —
  Playwright, any of ``firefox``/``chrome``/``edge``/``webkit``/``chromium``.
  :class:`BrowserSession` launches exactly the requested kind, no cascade —
  the caller (an agent) picks deliberately, so silent substitution would
  defeat that choice.
- **curl** (:mod:`._curlfetch`) — ``curl_cffi`` TLS/HTTP2 fingerprint
  impersonation, no browser process. Passes some anti-bot checks a real
  headless browser doesn't (doc/hidden/WEB_SEARCH_TOOL_REPORT.md); the one
  deliberate exception to this project's historical "no anti-bot
  circumvention" stance — network-layer impersonation, not JS-fingerprint
  spoofing or CAPTCHA solving.
- **Static HTML extraction** (:mod:`._htmlextract`) — a from-scratch Python
  port of the browser path's in-page JS extraction/wall-detection, used only
  when the curl backend has no live DOM to evaluate against.

``read_webpage`` (doc/READ_WEBPAGE.md) fetches one caller-given URL and
returns content shaped by ``content_filter`` (``off``/``html``/``text``).
``query_search_engine`` (doc/WEB_SEARCH.md) queries one of four engines
(:data:`SEARCH_ENGINES`) and returns organic hits — the ``web_search``
agent's discovery primitive, replacing the old deterministic
discover-all-four-in-parallel pipeline.
"""

from __future__ import annotations

from . import _curlfetch as curlfetch
from . import _engines_static as engines_static
from . import _htmlextract as htmlextract
from ._browser import BrowserKind, BrowserSession, BrowserUnavailableError
from ._enginequery import query_via_browser
from ._engines import SEARCH_ENGINES, SearchEngine, is_engine_internal
from ._readpage import BrowserContent, ContentFilter, fetch_via_browser
from ._state import TIME_MARK, TTL_SECONDS, WebSearchStateStore
from ._validate import AntiBotWallError, InvalidUrlError, validate_public_url

__all__ = [
    "SEARCH_ENGINES",
    "TIME_MARK",
    "TTL_SECONDS",
    "AntiBotWallError",
    "BrowserContent",
    "BrowserKind",
    "BrowserSession",
    "BrowserUnavailableError",
    "ContentFilter",
    "InvalidUrlError",
    "SearchEngine",
    "WebSearchStateStore",
    "curlfetch",
    "engines_static",
    "fetch_via_browser",
    "htmlextract",
    "is_engine_internal",
    "query_via_browser",
    "validate_public_url",
]
