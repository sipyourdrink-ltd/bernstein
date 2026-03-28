"""Tests for CatalogRegistry.match() — role matching with catalog fallback."""

from __future__ import annotations

from bernstein.agents.catalog import CatalogAgent, CatalogRegistry


def _make_agent(
    name: str,
    role: str,
    description: str = "",
    system_prompt: str = "You are a specialist.",
    priority: int = 100,
    source: str = "catalog",
) -> CatalogAgent:
    return CatalogAgent(
        name=name,
        role=role,
        description=description,
        system_prompt=system_prompt,
        id=f"test:{name}",
        priority=priority,
        source=source,
    )


# ---------------------------------------------------------------------------
# Empty registry
# ---------------------------------------------------------------------------


def test_match_empty_registry_returns_none() -> None:
    registry = CatalogRegistry()
    assert registry.match("backend", "Implement REST API") is None


# ---------------------------------------------------------------------------
# Exact role match
# ---------------------------------------------------------------------------


def test_match_exact_role() -> None:
    registry = CatalogRegistry()
    agent = _make_agent("BackendBot", "backend", "Backend engineer.")
    registry.register_agent(agent)

    result = registry.match("backend", "Implement REST API")
    assert result is not None
    assert result.name == "BackendBot"


def test_match_exact_role_no_match_returns_none_without_fuzzy_candidates() -> None:
    registry = CatalogRegistry()
    registry.register_agent(_make_agent("SecurityBot", "security", "Security engineer."))

    # "frontend" has no exact match and no keyword overlap with "Security engineer."
    result = registry.match("frontend", "Design login page")
    assert result is None


def test_match_exact_role_picks_lowest_priority() -> None:
    """Among multiple exact matches, the agent with the lowest priority value wins."""
    registry = CatalogRegistry()
    high_priority = _make_agent("QA-Pro", "qa", "Quality assurance.", priority=10)
    low_priority = _make_agent("QA-Basic", "qa", "Quality assurance.", priority=200)
    registry.register_agent(low_priority)
    registry.register_agent(high_priority)

    result = registry.match("qa", "Write integration tests")
    assert result is not None
    assert result.name == "QA-Pro"


# ---------------------------------------------------------------------------
# Fuzzy match (no exact role, keyword overlap in description)
# ---------------------------------------------------------------------------


def test_match_fuzzy_by_description_keywords() -> None:
    registry = CatalogRegistry()
    agent = _make_agent(
        "SecurityReviewer",
        "security",
        "Reviews security vulnerabilities and authentication flows.",
    )
    registry.register_agent(agent)

    # Role is "analyst" — no exact match; task description shares keywords
    result = registry.match("analyst", "Review authentication vulnerabilities in API")
    assert result is not None
    assert result.name == "SecurityReviewer"


def test_match_fuzzy_no_keyword_overlap_returns_none() -> None:
    registry = CatalogRegistry()
    registry.register_agent(_make_agent("MLBot", "ml-engineer", "Machine learning pipelines."))

    result = registry.match("devops", "Deploy kubernetes cluster")
    assert result is None


def test_match_fuzzy_picks_highest_overlap() -> None:
    """When multiple agents have keyword overlap, the one with most shared words wins."""
    registry = CatalogRegistry()
    weak = _make_agent("WeakMatch", "other", "General backend service developer.")
    strong = _make_agent("StrongMatch", "specialist", "Backend REST API service developer.")
    registry.register_agent(weak)
    registry.register_agent(strong)

    result = registry.match("backend", "Build backend REST API service")
    assert result is not None
    assert result.name == "StrongMatch"


def test_match_fuzzy_ignores_short_words() -> None:
    """Words of 3 chars or fewer are ignored in fuzzy matching."""
    registry = CatalogRegistry()
    # Description contains only short words that should be filtered
    registry.register_agent(_make_agent("TinyBot", "foo", "API fix the bug."))

    # Task description only has short words too
    result = registry.match("bar", "Do it now")
    assert result is None


# ---------------------------------------------------------------------------
# system_prompt usage
# ---------------------------------------------------------------------------


def test_matched_agent_has_system_prompt() -> None:
    registry = CatalogRegistry()
    agent = _make_agent(
        "BackendSpec",
        "backend",
        "Backend engineer.",
        system_prompt="You are a backend expert focused on performance.",
    )
    registry.register_agent(agent)

    result = registry.match("backend", "Optimize database queries")
    assert result is not None
    assert "backend expert" in result.system_prompt


# ---------------------------------------------------------------------------
# Source / priority ordering
# ---------------------------------------------------------------------------


def test_match_returns_catalog_source() -> None:
    registry = CatalogRegistry()
    registry.register_agent(_make_agent("AgencyAgent", "backend", "Backend.", source="agency"))

    result = registry.match("backend", "Anything")
    assert result is not None
    assert result.source == "agency"


def test_match_exact_beats_fuzzy() -> None:
    """Exact role match should win over a fuzzy match even if fuzzy has more keyword overlap."""
    registry = CatalogRegistry()
    exact = _make_agent("ExactBackend", "backend", "Simple backend.", priority=100)
    fuzzy = _make_agent("FuzzyBackend", "other", "Backend REST API developer build anything.", priority=1)
    registry.register_agent(exact)
    registry.register_agent(fuzzy)

    result = registry.match("backend", "Backend REST API developer build anything")
    assert result is not None
    assert result.name == "ExactBackend"


# ---------------------------------------------------------------------------
# load_from_agency integration
# ---------------------------------------------------------------------------


def test_load_from_agency_and_match() -> None:
    """Agents loaded via load_from_agency() should be matchable."""
    from unittest.mock import MagicMock

    mock_agent = MagicMock()
    mock_agent.name = "AgencySecurityBot"
    mock_agent.role = "security"
    mock_agent.description = "Agency security specialist."
    mock_agent.prompt_body = "You are a security specialist from Agency."

    registry = CatalogRegistry()
    loaded = registry.load_from_agency({"security-bot": mock_agent})

    assert loaded == 1
    result = registry.match("security", "Scan for vulnerabilities")
    assert result is not None
    assert result.name == "AgencySecurityBot"
    assert result.source == "agency"


def test_load_from_agency_skips_agents_without_prompt() -> None:
    from unittest.mock import MagicMock

    mock_agent = MagicMock()
    mock_agent.name = "EmptyAgent"
    mock_agent.role = "backend"
    mock_agent.description = "No prompt."
    mock_agent.prompt_body = None

    registry = CatalogRegistry()
    loaded = registry.load_from_agency({"empty": mock_agent})

    assert loaded == 0
    assert registry.match("backend", "anything") is None
