"""Tests for bernstein.core.researcher — web research for evolve mode."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from bernstein.core.researcher import (
    ResearchCache,
    ResearchReport,
    ResearchResult,
    format_research_context,
    run_research,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# ResearchResult
# ---------------------------------------------------------------------------


class TestResearchResult:
    """Basic dataclass tests."""

    def test_create(self) -> None:
        r = ResearchResult(query="test query", content="some results", timestamp=1000.0)
        assert r.query == "test query"
        assert r.content == "some results"
        assert r.timestamp == 1000.0


# ---------------------------------------------------------------------------
# ResearchCache
# ---------------------------------------------------------------------------


class TestResearchCache:
    """Tests for file-based research cache."""

    def test_put_and_get(self, tmp_path: Path) -> None:
        cache = ResearchCache(tmp_path / "cache")
        result = ResearchResult(query="ai agents", content="results here", timestamp=time.time())
        cache.put(result)
        got = cache.get("ai agents")
        assert got is not None
        assert got.content == "results here"

    def test_get_missing(self, tmp_path: Path) -> None:
        cache = ResearchCache(tmp_path / "cache")
        assert cache.get("nonexistent") is None

    def test_stale_cache_returns_none(self, tmp_path: Path) -> None:
        cache = ResearchCache(tmp_path / "cache")
        old_result = ResearchResult(query="old query", content="old", timestamp=1.0)
        cache.put(old_result)
        # Timestamp is very old, so it should be stale
        assert cache.get("old query") is None

    def test_cache_creates_directory(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "deep" / "nested" / "cache"
        ResearchCache(cache_dir)
        assert cache_dir.is_dir()

    def test_key_sanitization(self, tmp_path: Path) -> None:
        cache = ResearchCache(tmp_path / "cache")
        result = ResearchResult(
            query="AI agent $100 'weird' query!",
            content="data",
            timestamp=time.time(),
        )
        cache.put(result)
        got = cache.get("AI agent $100 'weird' query!")
        assert got is not None
        assert got.content == "data"


# ---------------------------------------------------------------------------
# format_research_context
# ---------------------------------------------------------------------------


class TestFormatResearchContext:
    """Tests for formatting research into markdown."""

    def test_empty_report(self) -> None:
        report = ResearchReport()
        assert format_research_context(report) == ""

    def test_competitors_only(self) -> None:
        report = ResearchReport(
            competitors=[
                ResearchResult(query="q", content="Competitor A is great", timestamp=1.0),
            ],
        )
        result = format_research_context(report)
        assert "## Market Research" in result
        assert "Competitor landscape" in result
        assert "Competitor A is great" in result
        assert "Build features no competitor has" in result

    def test_all_categories(self) -> None:
        report = ResearchReport(
            competitors=[ResearchResult(query="q1", content="comp data", timestamp=1.0)],
            user_needs=[ResearchResult(query="q2", content="user pain", timestamp=1.0)],
            trending=[ResearchResult(query="q3", content="trend info", timestamp=1.0)],
        )
        result = format_research_context(report)
        assert "Competitor landscape" in result
        assert "User pain points" in result
        assert "Trending features" in result
        assert "comp data" in result
        assert "user pain" in result
        assert "trend info" in result


# ---------------------------------------------------------------------------
# run_research (with mocked Tavily)
# ---------------------------------------------------------------------------


class TestRunResearch:
    """Tests for the research execution with mocked Tavily."""

    @pytest.mark.asyncio()
    async def test_runs_searches_and_caches(self, tmp_path: Path) -> None:
        with patch("bernstein.core.llm.tavily_search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = "Search result content"
            report = await run_research(tmp_path)

        # Should have results in at least one category
        total = len(report.competitors) + len(report.user_needs) + len(report.trending)
        assert total > 0
        assert mock_search.call_count > 0

    @pytest.mark.asyncio()
    async def test_uses_cache_on_second_run(self, tmp_path: Path) -> None:
        with patch("bernstein.core.llm.tavily_search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = "Search result"
            await run_research(tmp_path)

            # Second run should use cache exclusively — zero new API calls
            mock_search.reset_mock()
            await run_research(tmp_path)
            assert mock_search.call_count == 0

    @pytest.mark.asyncio()
    async def test_handles_tavily_failure_gracefully(self, tmp_path: Path) -> None:
        with patch("bernstein.core.llm.tavily_search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = ""  # Tavily returns empty on failure
            report = await run_research(tmp_path)

        # Should still return a report, just empty
        total = len(report.competitors) + len(report.user_needs) + len(report.trending)
        assert total == 0

    @pytest.mark.asyncio()
    async def test_respects_max_searches(self, tmp_path: Path) -> None:
        call_count = 0

        async def slow_search(query: str, max_results: int = 5) -> str:
            nonlocal call_count
            call_count += 1
            return f"Result for {query}"

        with patch("bernstein.core.llm.tavily_search", side_effect=slow_search):
            report = await run_research(tmp_path)

        # Should not exceed MAX_SEARCHES_PER_CYCLE (10)
        assert call_count <= 10
        # searches_performed should match actual API calls (not just failures)
        assert report.searches_performed == call_count
