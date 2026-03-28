"""Web research module for evolve mode.

Uses Tavily search API (via bernstein.core.llm) to gather market
intelligence that informs the manager agent when planning improvements.
Results are cached to avoid redundant API calls.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Maximum Tavily searches per evolve cycle to control costs.
MAX_SEARCHES_PER_CYCLE = 10

# Cache TTL in seconds (1 hour).
_CACHE_TTL_S = 3600

# Pre-defined research queries by category.
_COMPETITOR_QUERIES = [
    "multi-agent orchestration framework CLI coding",
    "AI agent orchestrator developer tools 2025",
]
_USER_NEEDS_QUERIES = [
    "AI coding agent pain points developer experience",
    "multi-agent framework problems issues GitHub",
]
_TRENDING_QUERIES = [
    "trending AI developer tools agent framework 2025",
    "new features AI coding assistant 2025",
]


@dataclass
class ResearchResult:
    """Holds the output of a single research query.

    Attributes:
        query: The search query that produced these results.
        content: Formatted markdown of search results.
        timestamp: Unix timestamp when the search was performed.
    """

    query: str
    content: str
    timestamp: float


@dataclass
class ResearchReport:
    """Aggregated research report for an evolve cycle.

    Attributes:
        competitors: Results about competing tools.
        user_needs: Results about developer pain points.
        trending: Results about trending features.
        searches_performed: Number of API calls made this cycle.
    """

    competitors: list[ResearchResult] = field(default_factory=list)
    user_needs: list[ResearchResult] = field(default_factory=list)
    trending: list[ResearchResult] = field(default_factory=list)
    searches_performed: int = 0


class ResearchCache:
    """File-based cache for research results.

    Stores results in ``.sdd/research/auto/`` as JSON files keyed by
    a sanitised version of the query string. Results older than
    ``_CACHE_TTL_S`` are considered stale.

    Args:
        cache_dir: Directory for cached results.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _key_path(self, query: str) -> Path:
        """Return the cache file path for a query."""
        safe = "".join(c if c.isalnum() else "_" for c in query.lower())[:80]
        return self._dir / f"{safe}.json"

    def get(self, query: str) -> ResearchResult | None:
        """Return a cached result if fresh, else None.

        Args:
            query: The search query.

        Returns:
            Cached ResearchResult or None if missing/stale.
        """
        path = self._key_path(query)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ts = data.get("timestamp", 0)
            if time.time() - ts > _CACHE_TTL_S:
                return None
            return ResearchResult(
                query=data["query"],
                content=data["content"],
                timestamp=ts,
            )
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def put(self, result: ResearchResult) -> None:
        """Write a result to the cache.

        Args:
            result: Research result to cache.
        """
        path = self._key_path(result.query)
        try:
            path.write_text(
                json.dumps({
                    "query": result.query,
                    "content": result.content,
                    "timestamp": result.timestamp,
                }),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Failed to cache research result: %s", exc)


async def _search(query: str, cache: ResearchCache) -> ResearchResult | None:
    """Execute a single Tavily search, using cache when available.

    Args:
        query: Search query string.
        cache: Research cache instance.

    Returns:
        ResearchResult, or None if the search failed and no cache exists.
    """
    cached = cache.get(query)
    if cached is not None:
        logger.debug("Research cache hit for: %r", query)
        return cached

    from bernstein.core.llm import tavily_search

    content = await tavily_search(query, max_results=5)
    if not content:
        return None

    result = ResearchResult(query=query, content=content, timestamp=time.time())
    cache.put(result)
    return result


async def run_research(workdir: Path) -> ResearchReport:
    """Execute all research queries for an evolve cycle.

    Runs up to ``MAX_SEARCHES_PER_CYCLE`` Tavily searches across three
    categories (competitors, user needs, trending). Uses a file cache in
    ``.sdd/research/auto/`` to avoid redundant calls.

    Args:
        workdir: Project working directory (for cache storage).

    Returns:
        Populated ResearchReport.
    """
    cache_dir = workdir / ".sdd" / "research" / "auto"
    cache = ResearchCache(cache_dir)
    report = ResearchReport()
    searches_left = MAX_SEARCHES_PER_CYCLE

    async def _do_queries(
        queries: list[str],
        target: list[ResearchResult],
    ) -> None:
        nonlocal searches_left
        for q in queries:
            if searches_left <= 0:
                break
            # Check cache BEFORE calling _search so we can track API calls accurately
            was_cached = cache.get(q) is not None
            result = await _search(q, cache)
            if result is not None:
                target.append(result)
            # Only count non-cached as an actual API search
            if not was_cached:
                searches_left -= 1
                report.searches_performed += 1

    await _do_queries(_COMPETITOR_QUERIES, report.competitors)
    await _do_queries(_USER_NEEDS_QUERIES, report.user_needs)
    await _do_queries(_TRENDING_QUERIES, report.trending)

    logger.info(
        "Research complete: %d competitor, %d user-needs, %d trending results (%d API calls)",
        len(report.competitors),
        len(report.user_needs),
        len(report.trending),
        report.searches_performed,
    )
    return report


def format_research_context(report: ResearchReport) -> str:
    """Format a ResearchReport into markdown context for the manager prompt.

    Args:
        report: The research report to format.

    Returns:
        Markdown string suitable for injection into the manager task description.
        Empty string if no results.
    """
    sections: list[str] = []

    if report.competitors:
        lines = "\n\n".join(r.content for r in report.competitors)
        sections.append(f"### Competitor landscape\n{lines}")

    if report.user_needs:
        lines = "\n\n".join(r.content for r in report.user_needs)
        sections.append(f"### User pain points\n{lines}")

    if report.trending:
        lines = "\n\n".join(r.content for r in report.trending)
        sections.append(f"### Trending features\n{lines}")

    if not sections:
        return ""

    return (
        "\n\n## Market Research (auto-generated)\n"
        + "\n\n".join(sections)
        + "\n\nBased on this research, create tasks that:\n"
        "1. Build features no competitor has\n"
        "2. Solve real pain points developers face\n"
        "3. Adopt trending patterns early\n"
    )


def run_research_sync(workdir: Path) -> ResearchReport:
    """Synchronous wrapper for ``run_research``.

    Safe to call from the orchestrator's synchronous tick loop.

    Args:
        workdir: Project working directory.

    Returns:
        Populated ResearchReport.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Already inside an event loop — run in a new thread
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, run_research(workdir))
                return future.result(timeout=60)
        return loop.run_until_complete(run_research(workdir))
    except RuntimeError:
        return asyncio.run(run_research(workdir))
