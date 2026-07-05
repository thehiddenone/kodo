"""Tests for the web-search subsystem: ``kodo.websearch`` + the tool handlers.

The Playwright/``curl_cffi``-driven parts (real browsing/network) are not
exercised here — they need a live browser or the open web. Covered instead:
the pure logic (engine roster, the ``WebSearchStateStore`` TTL/time_mark
memory, the static HTML extraction used by the ``curl`` backend) and the
``WebSearchTool`` orchestration with the agent-driving service stubbed out.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kodo.runtime import SessionState
from kodo.tools import ProjectPathResolver, ToolContext, WebSearchTool
from kodo.websearch import (
    SEARCH_ENGINES,
    TIME_MARK,
    TTL_SECONDS,
    WebSearchStateStore,
    engines_static,
    htmlextract,
    is_engine_internal,
)

# ---------------------------------------------------------------------------
# Engine roster
# ---------------------------------------------------------------------------


def test_engine_roster_and_merge_order() -> None:
    assert [e.name for e in SEARCH_ENGINES] == ["google", "bing", "duckduckgo", "wikipedia"]


def test_wikipedia_queries_english_fulltext_search() -> None:
    wikipedia = next(e for e in SEARCH_ENGINES if e.name == "wikipedia")
    url = wikipedia.search_url("rust borrow checker")
    assert url.startswith("https://en.wikipedia.org/w/index.php?search=rust+borrow+checker")
    # fulltext=1 forces a results list (no exact-match article redirect);
    # ns0=1 keeps it to the article namespace.
    assert "fulltext=1" in url
    assert "ns0=1" in url


def test_is_engine_internal_flags_engine_properties_not_wikipedia() -> None:
    assert is_engine_internal("https://www.google.com/search?q=x")
    assert is_engine_internal("https://www.bing.com/search?q=x")
    assert is_engine_internal("https://duckduckgo.com/y.js")
    # Wikipedia is deliberately not filtered: it is a legitimate hit source.
    assert not is_engine_internal("https://en.wikipedia.org/wiki/Rust")
    assert not is_engine_internal("https://example.com/page")


# ---------------------------------------------------------------------------
# WebSearchStateStore — TTL key-value memory, <time_mark> semantics
# ---------------------------------------------------------------------------


def test_state_store_starts_empty(tmp_path: Path) -> None:
    store = WebSearchStateStore(tmp_path / "state.json")
    assert store.get_all() == {}


def test_state_store_set_and_get(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    WebSearchStateStore(path).update("google_status", "blocked: captcha")
    # A fresh store instance reads the persisted state (survives "restarts").
    assert WebSearchStateStore(path).get_all() == {"google_status": "blocked: captcha"}


def test_state_store_empty_value_deletes_key(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = WebSearchStateStore(path)
    store.update("a", "1")
    store.update("b", "2")
    store.update("a", "")
    assert store.get_all() == {"b": "2"}


def test_state_store_time_mark_returns_elapsed_seconds(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = WebSearchStateStore(path)
    store.update("google_last_query", TIME_MARK)
    time.sleep(0.05)
    state = store.get_all()
    elapsed = float(state["google_last_query"])
    assert 0.0 < elapsed < 5.0


def test_state_store_time_mark_recomputed_fresh_each_read(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = WebSearchStateStore(path)
    store.update("k", TIME_MARK)
    first = float(store.get_all()["k"])
    time.sleep(0.05)
    second = float(store.get_all()["k"])
    assert second > first


def test_state_store_ttl_refreshes_on_every_write(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    raw = {"k": {"kind": "value", "value": "old", "ts": time.time() - TTL_SECONDS - 10}}
    path.write_text(json.dumps(raw), encoding="utf-8")
    # A fresh write refreshes ts, so the key survives despite the stale entry
    # on disk having been past its TTL.
    store = WebSearchStateStore(path)
    store.update("k", "new")
    assert store.get_all() == {"k": "new"}


def test_state_store_evicts_expired_entries_silently(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    raw = {
        "stale": {"kind": "value", "value": "old", "ts": time.time() - TTL_SECONDS - 10},
        "fresh": {"kind": "value", "value": "keep", "ts": time.time()},
    }
    path.write_text(json.dumps(raw), encoding="utf-8")
    store = WebSearchStateStore(path)
    assert store.get_all() == {"fresh": "keep"}
    # The eviction is persisted, not just filtered in memory.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert set(on_disk) == {"fresh"}


def test_state_store_corrupt_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = WebSearchStateStore(path)
    assert store.get_all() == {}
    # And an update through the corrupt file still works (rewrites it cleanly).
    store.update("k", "v")
    assert WebSearchStateStore(path).get_all() == {"k": "v"}


def test_state_store_ignores_malformed_entries(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "good": {"kind": "value", "value": "ok", "ts": time.time()},
                "bad_kind": {"kind": "nonsense", "value": "x", "ts": time.time()},
                "no_ts": {"kind": "value", "value": "x"},
                "not_a_dict": "oops",
            }
        ),
        encoding="utf-8",
    )
    store = WebSearchStateStore(path)
    assert store.get_all() == {"good": "ok"}


# ---------------------------------------------------------------------------
# htmlextract — static (curl-backend) content_filter extraction
# ---------------------------------------------------------------------------

_SAMPLE_HTML = """
<html><head><title>My Page</title><style>a{color:red}</style><script>bad()</script></head>
<body><nav>NAVBAR</nav>
<article>
<h1>Title Here</h1>
<p>Hello <a href="/rel">relative link</a> and <a href="https://x.com">abs</a> world.</p>
<ul><li>one</li><li>two with <a href="/y">link</a></li></ul>
<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>
</article>
</body></html>
"""


def test_extract_off_is_completely_untouched() -> None:
    assert htmlextract.extract_off(_SAMPLE_HTML) == _SAMPLE_HTML


def test_extract_html_strips_only_script_style() -> None:
    out = htmlextract.extract_html(_SAMPLE_HTML)
    assert "<script>" not in out
    assert "<style>" not in out
    assert "<nav>" in out  # everything else (nav, article, etc.) survives


def test_extract_text_converts_content_root_to_markdown() -> None:
    title, markdown = htmlextract.extract_text(_SAMPLE_HTML, "https://example.com/base/")
    assert title == "My Page"
    assert "# Title Here" in markdown
    assert "[relative link](https://example.com/rel)" in markdown
    assert "[abs](https://x.com)" in markdown
    assert "- one" in markdown
    assert "- two with [link](https://example.com/y)" in markdown
    assert "| A | B |" in markdown
    assert "NAVBAR" not in markdown  # nav chrome stripped in text mode


def test_is_blocked_detects_wall_patterns() -> None:
    assert htmlextract.is_blocked("<html><body>Please verify you are human</body></html>")
    assert not htmlextract.is_blocked(_SAMPLE_HTML)


# ---------------------------------------------------------------------------
# engines_static — curl-backend per-engine hit extraction
# ---------------------------------------------------------------------------


def test_engines_static_search_url_matches_engines_py() -> None:
    for engine in SEARCH_ENGINES:
        assert engines_static.search_url(engine.name, "q") == engine.search_url("q")
    with pytest.raises(ValueError):
        engines_static.search_url("altavista", "q")


_DUCKDUCKGO_RESULTS_HTML = """
<html><body>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Result A</a>
  <a class="result__snippet">Snippet A</a>
</div>
<div class="result result--ad">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fads.example.com%2F">Ad</a>
</div>
</body></html>
"""


def test_engines_static_extract_hits_unwraps_redirect_and_skips_ads() -> None:
    hits = engines_static.extract_hits(
        "duckduckgo", _DUCKDUCKGO_RESULTS_HTML, "https://html.duckduckgo.com/html/"
    )
    assert len(hits) == 1
    assert hits[0]["url"] == "https://example.com/a"
    assert hits[0]["title"] == "Result A"


def test_engines_static_is_blocked_detects_anomaly_page() -> None:
    html = "<html><body>Unfortunately, bots use DuckDuckGo too.</body></html>"
    assert engines_static.is_blocked("duckduckgo", html)
    assert not engines_static.is_blocked("duckduckgo", _DUCKDUCKGO_RESULTS_HTML)


# ---------------------------------------------------------------------------
# WebSearchTool — thin wrapper over the web_search agent
# ---------------------------------------------------------------------------


class _AgentServices:
    """EngineServices stub capturing the delegation to ``run_web_search_agent``."""

    def __init__(self, result: dict[str, object] | None = None, error: bool = False) -> None:
        self.result = result if result is not None else {"themes": [], "note": "ok"}
        self.error = error
        self.task_input: dict[str, object] | None = None

    async def run_web_search_agent(self, task_input: dict[str, object]) -> dict[str, object]:
        if self.error:
            raise RuntimeError("agent turn exploded")
        self.task_input = task_input
        return self.result


def _make_tool(tmp_path: Path, services: object) -> WebSearchTool:
    context = ToolContext(
        resolver=ProjectPathResolver(tmp_path),
        gate=MagicMock(),
        session=SessionState(),
        services=services,  # type: ignore[arg-type]
        agent_name="investigator",
        session_id="sess-test",
    )
    return WebSearchTool(context)


@pytest.mark.asyncio
async def test_web_search_requires_query(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path, _AgentServices())
    parsed = json.loads(await tool.handle({}))
    assert "error" in parsed


@pytest.mark.asyncio
async def test_web_search_delegates_to_agent_and_returns_result_verbatim(tmp_path: Path) -> None:
    themes = [{"summary": "Use A.", "details": "Sources agree.", "links": ["https://a.example"]}]
    services = _AgentServices(result={"themes": themes, "note": "Queried 2 engines."})
    tool = _make_tool(tmp_path, services)

    parsed = json.loads(await tool.handle({"query": "how to A", "max_results": 3, "timeout": 60}))

    assert parsed["themes"] == themes
    assert parsed["note"] == "Queried 2 engines."
    assert services.task_input == {"query": "how to A", "max_themes": 3, "timeout": 60.0}


@pytest.mark.asyncio
async def test_web_search_clamps_max_results_and_timeout(tmp_path: Path) -> None:
    services = _AgentServices()
    tool = _make_tool(tmp_path, services)

    await tool.handle({"query": "q", "max_results": 999, "timeout": 999999})

    assert services.task_input is not None
    assert services.task_input["max_themes"] == 10  # hard cap
    assert services.task_input["timeout"] == 600.0  # hard cap


@pytest.mark.asyncio
async def test_web_search_defaults_when_omitted(tmp_path: Path) -> None:
    services = _AgentServices()
    tool = _make_tool(tmp_path, services)

    await tool.handle({"query": "q"})

    assert services.task_input == {"query": "q", "max_themes": 5, "timeout": 180.0}


@pytest.mark.asyncio
async def test_web_search_agent_failure_degrades_to_note(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path, _AgentServices(error=True))

    parsed = json.loads(await tool.handle({"query": "how to A"}))

    assert parsed["themes"] == []
    assert "failed" in parsed["note"].lower()
