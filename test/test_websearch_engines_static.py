"""Tests for :mod:`kodo.websearch.engines_static` — curl-backend hit extraction.

Every engine's wall detector and organic-hit extractor is exercised against
small inline results-page fixtures; nothing is fetched.
"""

from __future__ import annotations

import base64

import pytest

from kodo.websearch import engines_static


def _bing_ck(target: str) -> str:
    """A Bing ``/ck/a`` click-tracking URL wrapping *target* (``u=a1<base64url>``)."""
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&amp;p=abc&amp;u=a1{encoded}&amp;ntb=1"


# ---------------------------------------------------------------------------
# google
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        '<form id="captcha-form"></form>',
        '<form action="https://www.google.com/sorry/index"></form>',
        '<iframe src="https://www.google.com/recaptcha/api2/anchor"></iframe>',
        '<div id="recaptcha"></div>',
    ],
)
def test_google_wall_detected(html: str) -> None:
    assert engines_static.is_blocked("google", f"<html><body>{html}</body></html>")


def test_google_results_page_not_blocked() -> None:
    assert not engines_static.is_blocked("google", "<html><body><div id='rso'></div></body></html>")


def test_google_skips_headings_without_usable_links() -> None:
    html = """
    <div id="rso">
      <div class="g"><h3>No link at all</h3></div>
      <div class="g"><a><h3>Link without href</h3></a></div>
      <div class="g"><a href="javascript:void(0)"><h3>Script link</h3></a></div>
      <div class="g"><a href="/url?sa=U"><h3>Wrapper without target</h3></a></div>
      <div class="g">
        <a href="https://example.com/ok"><h3>Organic</h3></a>
        <div class="VwiC3b">Snippet</div>
      </div>
      <div class="g"><a href="https://example.com/ok"><h3>Duplicate</h3></a></div>
    </div>
    """
    hits = engines_static.extract_hits("google", html, "https://www.google.com/search?q=x")
    # The /url wrapper with no q= target stays a google.com URL and is dropped
    # as engine-internal; the duplicate URL is dropped too.
    assert hits == [{"url": "https://example.com/ok", "title": "Organic", "snippet": "Snippet"}]


def test_google_hit_without_result_box_has_empty_snippet() -> None:
    html = '<div id="search"><a href="https://example.com/bare"><h3>Bare</h3></a></div>'
    hits = engines_static.extract_hits("google", html, "https://www.google.com/")
    assert hits == [{"url": "https://example.com/bare", "title": "Bare", "snippet": ""}]


# ---------------------------------------------------------------------------
# bing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html",
    [
        '<html><body><div id="b_captcha"></div></body></html>',
        '<html><body><iframe src="https://challenges.example/challenge"></iframe></body></html>',
        "<html><head><title>Verify you are human</title></head><body></body></html>",
        "<html><body>One last step. Please verify that you're a human.</body></html>",
    ],
)
def test_bing_wall_detected(html: str) -> None:
    assert engines_static.is_blocked("bing", html)


def test_bing_results_page_not_blocked() -> None:
    html = "<html><head><title>rust - Search</title></head><body>Results</body></html>"
    assert not engines_static.is_blocked("bing", html)


def test_bing_extracts_organic_hits_and_unwraps_click_tracking() -> None:
    html = f"""
    <html><body><ol id="b_results">
      <li class="b_algo">
        <h2><a href="{_bing_ck("https://example.com/unwrapped")}">Wrapped</a></h2>
        <div class="b_caption"><p>Wrapped snippet</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="https://example.org/direct">Direct</a></h2>
        <p>Direct snippet</p>
      </li>
      <li class="b_algo"><p>No heading link</p></li>
      <li class="b_algo"><h2><a>No href</a></h2></li>
      <li class="b_algo"><h2><a href="{_bing_ck("ftp://example.com/file")}">Non-http</a></h2></li>
      <li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1A">Bad base64</a></h2></li>
      <li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=zz">Unknown scheme</a></h2></li>
      <li class="b_ad"><h2><a href="https://ads.example.com/">Ad</a></h2></li>
    </ol></body></html>
    """
    hits = engines_static.extract_hits("bing", html, "https://www.bing.com/search?q=x")
    assert hits == [
        {
            "url": "https://example.com/unwrapped",
            "title": "Wrapped",
            "snippet": "Wrapped snippet",
        },
        {"url": "https://example.org/direct", "title": "Direct", "snippet": "Direct snippet"},
    ]


def test_bing_leaves_non_bing_ck_paths_alone() -> None:
    html = """
    <ol id="b_results"><li class="b_algo">
      <h2><a href="https://example.com/ck/a?u=a1aHR0cHM6Ly9ldmlsLmNvbQ">Lookalike</a></h2>
    </li></ol>
    """
    hits = engines_static.extract_hits("bing", html, "https://www.bing.com/")
    assert [h["url"] for h in hits] == ["https://example.com/ck/a?u=a1aHR0cHM6Ly9ldmlsLmNvbQ"]


# ---------------------------------------------------------------------------
# duckduckgo
# ---------------------------------------------------------------------------


def test_duckduckgo_challenge_form_detected() -> None:
    html = '<html><body><form action="/challenge/submit"></form></body></html>'
    assert engines_static.is_blocked("duckduckgo", html)
    assert engines_static.is_blocked("duckduckgo", '<div class="anomaly-modal"></div>')


def test_duckduckgo_skips_ads_missing_links_and_non_http_targets() -> None:
    html = """
    <html><body>
      <div class="result"><span class="badge--ad">Ad</span>
        <a class="result__a" href="https://ads.example.com/">Badge ad</a></div>
      <div class="result"><span>No title link</span></div>
      <div class="result"><a class="result__a">No href</a></div>
      <div class="result">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=ftp%3A%2F%2Fexample.com%2F">FTP</a>
      </div>
      <div class="result">
        <a class="result__a" href="https://example.net/plain">Plain</a>
        <a class="result__snippet">Plain snippet</a>
      </div>
    </body></html>
    """
    hits = engines_static.extract_hits("duckduckgo", html, "https://html.duckduckgo.com/html/")
    assert hits == [
        {"url": "https://example.net/plain", "title": "Plain", "snippet": "Plain snippet"}
    ]


# ---------------------------------------------------------------------------
# wikipedia
# ---------------------------------------------------------------------------


def test_wikipedia_is_never_blocked() -> None:
    assert not engines_static.is_blocked("wikipedia", "<html><body>verify you are human</body>")


def test_wikipedia_extracts_search_results() -> None:
    html = """
    <ul class="mw-search-results">
      <li class="mw-search-result">
        <div class="mw-search-result-heading"><a href="/wiki/Rust_(programming_language)"
          >Rust (programming language)</a></div>
        <div class="searchresult">A <span>systems</span> language</div>
      </li>
      <li class="mw-search-result"><div class="searchresult">No heading</div></li>
      <li class="mw-search-result">
        <div class="mw-search-result-heading"><a href="mailto:x@example.com">Mail</a></div>
      </li>
    </ul>
    """
    hits = engines_static.extract_hits(
        "wikipedia", html, "https://en.wikipedia.org/w/index.php?search=rust"
    )
    assert hits == [
        {
            "url": "https://en.wikipedia.org/wiki/Rust_(programming_language)",
            "title": "Rust (programming language)",
            "snippet": "A systems language",
        }
    ]
