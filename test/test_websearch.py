"""Tests for the ``web_search`` pipeline: ``kodo.websearch`` + the tool handler.

The Playwright-driven parts (real browsing) are not exercised here — they need
a live browser and the open web. Covered instead: the pure logic (cooldown
store, hit merging) and the ``WebSearchTool`` orchestration with phases 1–2
stubbed out and a fake ``run_web_summarizer`` service for phase 3.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import TracebackType
from typing import Any
from unittest.mock import MagicMock

import pytest

from kodo.runtime import SessionState
from kodo.tools import ProjectPathResolver, ToolContext, WebSearchTool
from kodo.websearch import (
    MAX_SOURCES,
    SEARCH_ENGINES,
    BrowserUnavailableError,
    CooldownStore,
    DiscoveryOutcome,
    PageText,
    ScrapeOutcome,
    SearchHit,
    merge_hits,
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


# ---------------------------------------------------------------------------
# CooldownStore
# ---------------------------------------------------------------------------


def test_cooldown_untripped_engine_has_no_remaining(tmp_path: Path) -> None:
    store = CooldownStore(tmp_path / "cooldowns.json")
    assert store.remaining("google") == 0.0


def test_cooldown_trip_blocks_engine_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "cooldowns.json"
    CooldownStore(path).trip("google", seconds=1800)
    # A fresh store instance reads the persisted state (survives "restarts").
    remaining = CooldownStore(path).remaining("google")
    assert 1790 < remaining <= 1800
    assert CooldownStore(path).remaining("bing") == 0.0


def test_cooldown_expires(tmp_path: Path) -> None:
    path = tmp_path / "cooldowns.json"
    store = CooldownStore(path)
    store.trip("bing", seconds=0.0)
    assert store.remaining("bing") == 0.0


def test_cooldown_corrupt_file_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "cooldowns.json"
    path.write_text("{not json", encoding="utf-8")
    store = CooldownStore(path)
    assert store.remaining("google") == 0.0
    # And a trip through the corrupt file still works (rewrites it cleanly).
    store.trip("google")
    assert store.remaining("google") > 0
    assert json.loads(path.read_text(encoding="utf-8")).keys() == {"google"}


def test_cooldown_ignores_malformed_entries(tmp_path: Path) -> None:
    path = tmp_path / "cooldowns.json"
    path.write_text(json.dumps({"google": "soon", "bing": time.time() + 600}), encoding="utf-8")
    store = CooldownStore(path)
    assert store.remaining("google") == 0.0
    assert store.remaining("bing") > 0


# ---------------------------------------------------------------------------
# merge_hits — rank-interleaving, dedupe, cap
# ---------------------------------------------------------------------------


def _hit(engine: str, rank: int, url: str) -> SearchHit:
    return SearchHit(engine=engine, rank=rank, url=url, title=f"{engine}#{rank}", snippet="")


def test_merge_interleaves_by_rank() -> None:
    merged = merge_hits(
        [
            [_hit("google", 1, "https://a.example"), _hit("google", 2, "https://b.example")],
            [_hit("bing", 1, "https://c.example"), _hit("bing", 2, "https://d.example")],
        ],
        max_links=15,
    )
    assert [h.url for h in merged] == [
        "https://a.example",
        "https://c.example",
        "https://b.example",
        "https://d.example",
    ]


def test_merge_dedupes_on_normalized_url() -> None:
    merged = merge_hits(
        [
            [_hit("google", 1, "https://Example.com/page/")],
            [_hit("bing", 1, "https://example.com/page#frag")],
        ],
        max_links=15,
    )
    assert len(merged) == 1
    assert merged[0].engine == "google"  # first engine wins


def test_merge_caps_total_links() -> None:
    hits = [[_hit("google", i, f"https://example.com/{i}") for i in range(1, 21)]]
    assert len(merge_hits(hits, max_links=MAX_SOURCES)) == 16


def test_merge_handles_uneven_lists() -> None:
    merged = merge_hits(
        [
            [_hit("google", 1, "https://a.example")],
            [
                _hit("bing", 1, "https://b.example"),
                _hit("bing", 2, "https://c.example"),
            ],
        ],
        max_links=15,
    )
    assert [h.url for h in merged] == [
        "https://a.example",
        "https://b.example",
        "https://c.example",
    ]


# ---------------------------------------------------------------------------
# WebSearchTool — pipeline orchestration with phases 1–2 stubbed
# ---------------------------------------------------------------------------


class _FakeBrowserSession:
    """Stands in for ``BrowserSession``: no Playwright, no browser."""

    installed_now = False
    browser = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeBrowserSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _SummarizerServices:
    """EngineServices stub capturing the phase-3 delegation."""

    def __init__(self, result: dict[str, object] | None = None, error: bool = False) -> None:
        self.result = result if result is not None else {"themes": []}
        self.error = error
        self.task_input: dict[str, object] | None = None

    async def run_web_summarizer(self, task_input: dict[str, object]) -> dict[str, object]:
        if self.error:
            raise RuntimeError("summarizer exploded")
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


_HITS = [
    _hit("google", 1, "https://a.example/post"),
    _hit("bing", 1, "https://b.example/doc"),
]
_PAGES = [
    PageText(url="https://a.example/post", title="A", text="alpha " * 100),
    PageText(url="https://b.example/doc", title="B", text="beta " * 100),
]


def _stub_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    discovery: DiscoveryOutcome,
    scrape: ScrapeOutcome,
) -> None:
    monkeypatch.setattr("kodo.tools._web_search.BrowserSession", _FakeBrowserSession)

    async def _fake_discover(browser: Any, query: str, cooldowns: Any) -> DiscoveryOutcome:
        return discovery

    async def _fake_scrape(browser: Any, hits: Any) -> ScrapeOutcome:
        return scrape

    monkeypatch.setattr("kodo.tools._web_search.discover", _fake_discover)
    monkeypatch.setattr("kodo.tools._web_search.scrape_pages", _fake_scrape)


@pytest.mark.asyncio
async def test_web_search_requires_query(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path, _SummarizerServices())
    parsed = json.loads(await tool.handle({}))
    assert "error" in parsed


@pytest.mark.asyncio
async def test_web_search_happy_path_returns_themes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    themes = [
        {
            "summary": "Use approach A.",
            "details": "Sources agree A works.",
            "links": ["https://a.example/post"],
        }
    ]
    services = _SummarizerServices(result={"themes": themes})
    _stub_pipeline(
        monkeypatch,
        DiscoveryOutcome(hits=list(_HITS), queried=["google", "bing"]),
        ScrapeOutcome(pages=list(_PAGES)),
    )
    tool = _make_tool(tmp_path, services)

    parsed = json.loads(await tool.handle({"query": "how to A", "max_results": 3}))

    assert parsed["themes"] == themes
    assert "Queried google, bing." in parsed["note"]
    assert "Scraped 2 of 2 page(s)." in parsed["note"]
    assert "Generated 1 theme(s)." in parsed["note"]
    # Phase 3 received the scraped sources and the theme cap.
    assert services.task_input is not None
    assert services.task_input["max_themes"] == 3
    sources = services.task_input["sources"]
    assert isinstance(sources, list)
    assert [s["url"] for s in sources] == [p.url for p in _PAGES]


@pytest.mark.asyncio
async def test_web_search_no_links_reports_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pipeline(
        monkeypatch,
        DiscoveryOutcome(
            queried=["google"],
            skipped={"bing": "anti-bot cooldown, ~10m left"},
            tripped=["duckduckgo"],
        ),
        ScrapeOutcome(),
    )
    tool = _make_tool(tmp_path, _SummarizerServices())

    parsed = json.loads(await tool.handle({"query": "anything"}))

    assert parsed["themes"] == []
    assert "Skipped bing" in parsed["note"]
    assert "duckduckgo served an anti-bot wall" in parsed["note"]
    assert "No web pages could be discovered" in parsed["note"]


@pytest.mark.asyncio
async def test_web_search_summarizer_failure_degrades_to_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pipeline(
        monkeypatch,
        DiscoveryOutcome(hits=list(_HITS), queried=["google", "bing"]),
        ScrapeOutcome(pages=list(_PAGES)),
    )
    tool = _make_tool(tmp_path, _SummarizerServices(error=True))

    parsed = json.loads(await tool.handle({"query": "how to A"}))

    assert parsed["themes"] == []
    assert "summarization failed" in parsed["note"].lower()
    # The scraped URLs are still surfaced so the caller has something to follow.
    assert "https://a.example/post" in parsed["note"]


@pytest.mark.asyncio
async def test_web_search_browser_unavailable_degrades_to_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _NoBrowser:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _NoBrowser:
            raise BrowserUnavailableError("install failed")

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            return None

    monkeypatch.setattr("kodo.tools._web_search.BrowserSession", _NoBrowser)
    tool = _make_tool(tmp_path, _SummarizerServices())

    parsed = json.loads(await tool.handle({"query": "anything"}))

    assert parsed["themes"] == []
    assert "unavailable" in parsed["note"].lower()
    assert "install failed" in parsed["note"]
