"""Tests for capability-based agent addressing and routing.

Covers the original CapabilityRouter (discovery-based) *and* the new
CapabilityRegistry with typed Capability/CapabilityLevel (issue #647).
"""

from __future__ import annotations

import pytest
from bernstein.core.agent_discovery import AgentCapabilities, DiscoveryResult

from bernstein.core.routing.capability_router import (
    AgentProfile,
    Capability,
    CapabilityLevel,
    CapabilityMatch,
    CapabilityRegistry,
    CapabilityRouter,
    RegistryMatch,
    build_default_profiles,
    infer_capabilities_from_description,
    normalize_capability,
    populate_registry_defaults,
)


@pytest.fixture()
def claude_agent() -> AgentCapabilities:
    return AgentCapabilities(
        name="claude",
        binary="/usr/bin/claude",
        version="1.0.0",
        logged_in=True,
        login_method="API key",
        available_models=["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        default_model="claude-sonnet-4-6",
        supports_headless=True,
        supports_sandbox=False,
        supports_mcp=True,
        max_context_tokens=200_000,
        reasoning_strength="very_high",
        best_for=["architecture", "complex-refactoring", "security-review", "tool-use"],
        cost_tier="moderate",
    )


@pytest.fixture()
def codex_agent() -> AgentCapabilities:
    return AgentCapabilities(
        name="codex",
        binary="/usr/bin/codex",
        version="1.0.0",
        logged_in=True,
        login_method="API key",
        available_models=["gpt-5.4", "gpt-5.4-mini", "o3", "o4-mini"],
        default_model="gpt-5.4",
        supports_headless=True,
        supports_sandbox=True,
        supports_mcp=True,
        max_context_tokens=200_000,
        reasoning_strength="high",
        best_for=["quick-fixes", "code-review", "test-writing", "reasoning-tasks"],
        cost_tier="cheap",
    )


@pytest.fixture()
def gemini_agent() -> AgentCapabilities:
    return AgentCapabilities(
        name="gemini",
        binary="/usr/bin/gemini",
        version="1.0.0",
        logged_in=True,
        login_method="gcloud",
        available_models=["gemini-3-pro", "gemini-3-flash", "gemini-3.1-pro"],
        default_model="gemini-3-pro",
        supports_headless=True,
        supports_sandbox=True,
        supports_mcp=True,
        max_context_tokens=1_000_000,
        reasoning_strength="very_high",
        best_for=["frontend", "long-context", "multimodal", "free-tier"],
        cost_tier="free",
    )


@pytest.fixture()
def logged_out_agent() -> AgentCapabilities:
    return AgentCapabilities(
        name="aider",
        binary="/usr/bin/aider",
        version="1.0.0",
        logged_in=False,
        login_method="",
        available_models=["gpt-4"],
        default_model="gpt-4",
        supports_headless=True,
        supports_sandbox=False,
        supports_mcp=False,
        max_context_tokens=128_000,
        reasoning_strength="medium",
        best_for=["interactive-editing"],
        cost_tier="cheap",
    )


@pytest.fixture()
def discovery(
    claude_agent: AgentCapabilities,
    codex_agent: AgentCapabilities,
    gemini_agent: AgentCapabilities,
    logged_out_agent: AgentCapabilities,
) -> DiscoveryResult:
    return DiscoveryResult(
        agents=[claude_agent, codex_agent, gemini_agent, logged_out_agent],
        warnings=[],
        scan_time_ms=10.0,
    )


class TestNormalizeCapability:
    def test_python_aliases(self) -> None:
        assert normalize_capability("py") == "python"
        assert normalize_capability("python3") == "python"
        assert normalize_capability("Python") == "python"

    def test_javascript_aliases(self) -> None:
        assert normalize_capability("js") == "javascript"
        assert normalize_capability("jsx") == "javascript"

    def test_typescript_aliases(self) -> None:
        assert normalize_capability("ts") == "typescript"
        assert normalize_capability("tsx") == "typescript"

    def test_testing_aliases(self) -> None:
        assert normalize_capability("test") == "testing"
        assert normalize_capability("tests") == "testing"
        assert normalize_capability("pytest") == "testing"

    def test_devops_aliases(self) -> None:
        assert normalize_capability("docker") == "devops"
        assert normalize_capability("k8s") == "devops"
        assert normalize_capability("ci") == "devops"

    def test_passthrough_unknown(self) -> None:
        assert normalize_capability("custom-skill") == "custom-skill"

    def test_whitespace_handling(self) -> None:
        assert normalize_capability("  python  ") == "python"

    def test_underscore_to_hyphen(self) -> None:
        assert normalize_capability("code_review") == "code-review"


class TestInferCapabilities:
    def test_python_keywords(self) -> None:
        caps = infer_capabilities_from_description("Fix pytest test failures in the Python module")
        assert "python" in caps
        assert "testing" in caps

    def test_frontend_keywords(self) -> None:
        caps = infer_capabilities_from_description("Update the React component CSS styles")
        assert "frontend" in caps

    def test_security_keywords(self) -> None:
        caps = infer_capabilities_from_description("Fix JWT authentication vulnerability")
        assert "security" in caps

    def test_devops_keywords(self) -> None:
        caps = infer_capabilities_from_description("Update Docker CI pipeline for deployment")
        assert "devops" in caps

    def test_empty_description(self) -> None:
        caps = infer_capabilities_from_description("")
        assert caps == []

    def test_multiple_capabilities(self) -> None:
        caps = infer_capabilities_from_description("Write pytest tests for the API endpoint with JWT auth")
        assert "testing" in caps
        assert "backend" in caps
        assert "security" in caps


class TestCapabilityRouter:
    def test_basic_match(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        matches = router.match(["python", "testing"])
        assert len(matches) > 0
        assert all(isinstance(m, CapabilityMatch) for m in matches)

    def test_excludes_logged_out(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        matches = router.match(["python"])
        agent_names = [m.agent_name for m in matches]
        assert "aider" not in agent_names

    def test_security_prefers_strong_reasoning(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        matches = router.match(["security", "code-review"])
        assert len(matches) > 0
        # Claude and gemini have very_high reasoning
        top = matches[0]
        assert top.agent_name in ("claude", "gemini")

    def test_long_context_prefers_gemini(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        matches = router.match(["long-context"])
        top_names = [m.agent_name for m in matches if "long-context" in m.matched_capabilities]
        assert "gemini" in top_names

    def test_cheap_capability(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        matches = router.match(["cheap", "fast"])
        assert len(matches) > 0
        top = matches[0]
        assert top.agent_name in ("codex", "gemini")

    def test_preferred_agent_boost(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        matches_no_pref = router.match(["python"])
        matches_pref = router.match(["python"], preferred_agent="codex")
        codex_score_no_pref = next((m.match_score for m in matches_no_pref if m.agent_name == "codex"), 0.0)
        codex_score_pref = next((m.match_score for m in matches_pref if m.agent_name == "codex"), 0.0)
        assert codex_score_pref >= codex_score_no_pref

    def test_best_match_returns_single(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        result = router.best_match(["python", "testing"])
        assert result is not None
        assert isinstance(result, CapabilityMatch)

    def test_best_match_none_when_no_agents(self) -> None:
        empty = DiscoveryResult(agents=[], warnings=[], scan_time_ms=0.0)
        router = CapabilityRouter(discovery=empty)
        result = router.best_match(["python"])
        assert result is None

    def test_empty_requires_returns_all(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        matches = router.match([])
        assert len(matches) == 3  # 3 logged-in agents

    def test_min_score_filters(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        all_matches = router.match(["python", "testing"], min_score=0.0)
        high_matches = router.match(["python", "testing"], min_score=0.9)
        assert len(high_matches) <= len(all_matches)

    def test_match_score_bounded(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        matches = router.match(["python", "testing", "security"])
        for m in matches:
            assert 0.0 <= m.match_score <= 1.0

    def test_model_selection_strong_caps(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        match = router.best_match(["security", "design"])
        assert match is not None
        # Should pick strongest model — non-empty string
        assert match.model

    def test_model_selection_cheap_caps(self, discovery: DiscoveryResult) -> None:
        router = CapabilityRouter(discovery=discovery)
        matches = router.match(["cheap", "fast"])
        for m in matches:
            if m.agent_name == "codex":
                assert "mini" in m.model.lower() or m.model == "gpt-5.4"


# ===================================================================
# Issue #647 — CapabilityRegistry with typed capabilities
# ===================================================================

# ---------------------------------------------------------------------------
# Helpers for registry tests
# ---------------------------------------------------------------------------


def _cap(name: str, level: CapabilityLevel = CapabilityLevel.BASIC) -> Capability:
    """Shorthand for creating a Capability."""
    return Capability(name=name, level=level)


def _expert(name: str) -> Capability:
    return Capability(name=name, level=CapabilityLevel.EXPERT)


def _advanced(name: str) -> Capability:
    return Capability(name=name, level=CapabilityLevel.ADVANCED)


def _basic(name: str) -> Capability:
    return Capability(name=name, level=CapabilityLevel.BASIC)


# ---------------------------------------------------------------------------
# Capability dataclass
# ---------------------------------------------------------------------------


class TestCapabilityDataclass:
    """Tests for the Capability frozen dataclass."""

    def test_frozen(self) -> None:
        cap = _cap("python")
        with pytest.raises(AttributeError):
            cap.name = "java"  # type: ignore[misc]

    def test_defaults(self) -> None:
        cap = Capability(name="python")
        assert cap.level == CapabilityLevel.BASIC
        assert cap.description == ""

    def test_equality(self) -> None:
        a = Capability(name="python", level=CapabilityLevel.EXPERT)
        b = Capability(name="python", level=CapabilityLevel.EXPERT)
        assert a == b

    def test_hash_in_frozenset(self) -> None:
        caps = frozenset({_expert("python"), _expert("python")})
        assert len(caps) == 1

    def test_description_stored(self) -> None:
        cap = Capability(name="python", description="CPython expertise")
        assert cap.description == "CPython expertise"


# ---------------------------------------------------------------------------
# CapabilityLevel ordering
# ---------------------------------------------------------------------------


class TestCapabilityLevelOrdering:
    """Tests for CapabilityLevel enum ordering."""

    def test_expert_ge_advanced(self) -> None:
        assert CapabilityLevel.EXPERT >= CapabilityLevel.ADVANCED

    def test_advanced_ge_basic(self) -> None:
        assert CapabilityLevel.ADVANCED >= CapabilityLevel.BASIC

    def test_basic_not_ge_advanced(self) -> None:
        assert not (CapabilityLevel.BASIC >= CapabilityLevel.ADVANCED)

    def test_expert_gt_basic(self) -> None:
        assert CapabilityLevel.EXPERT > CapabilityLevel.BASIC

    def test_basic_lt_expert(self) -> None:
        assert CapabilityLevel.BASIC < CapabilityLevel.EXPERT

    def test_same_level_ge(self) -> None:
        level = CapabilityLevel.ADVANCED
        assert level >= CapabilityLevel.ADVANCED

    def test_same_level_not_gt(self) -> None:
        level = CapabilityLevel.ADVANCED
        assert not (level > CapabilityLevel.ADVANCED)

    def test_le_ordering(self) -> None:
        assert CapabilityLevel.BASIC <= CapabilityLevel.ADVANCED
        level = CapabilityLevel.ADVANCED
        assert level <= CapabilityLevel.ADVANCED
        assert not (CapabilityLevel.EXPERT <= CapabilityLevel.BASIC)


# ---------------------------------------------------------------------------
# AgentProfile dataclass
# ---------------------------------------------------------------------------


class TestAgentProfileDataclass:
    """Tests for the AgentProfile frozen dataclass."""

    def test_frozen(self) -> None:
        profile = AgentProfile(adapter_name="claude", model="opus")
        with pytest.raises(AttributeError):
            profile.adapter_name = "codex"  # type: ignore[misc]

    def test_default_capabilities_empty(self) -> None:
        profile = AgentProfile(adapter_name="claude", model="opus")
        assert profile.capabilities == frozenset()


# ---------------------------------------------------------------------------
# RegistryMatch dataclass
# ---------------------------------------------------------------------------


class TestRegistryMatchDataclass:
    """Tests for the RegistryMatch frozen dataclass."""

    def test_frozen(self) -> None:
        profile = AgentProfile(adapter_name="claude", model="opus")
        match = RegistryMatch(
            agent=profile,
            score=0.8,
            matched_capabilities=frozenset({_expert("python")}),
            missing_capabilities=frozenset(),
        )
        with pytest.raises(AttributeError):
            match.score = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# CapabilityRegistry — registration
# ---------------------------------------------------------------------------


class TestRegistryRegistration:
    """Tests for agent registration and unregistration."""

    def test_register_returns_profile(self) -> None:
        reg = CapabilityRegistry()
        caps = frozenset({_expert("python")})
        profile = reg.register("claude", "opus", caps)
        assert profile.adapter_name == "claude"
        assert profile.model == "opus"
        assert profile.capabilities == caps

    def test_agents_property(self) -> None:
        reg = CapabilityRegistry()
        reg.register("claude", "opus", frozenset({_expert("python")}))
        reg.register("codex", "gpt-4", frozenset({_advanced("python")}))
        assert len(reg.agents) == 2

    def test_register_replaces_existing(self) -> None:
        reg = CapabilityRegistry()
        reg.register("claude", "opus", frozenset({_expert("python")}))
        reg.register("claude", "opus", frozenset({_basic("python")}))
        assert len(reg.agents) == 1
        profile = reg.agents[0]
        cap_levels = {c.level for c in profile.capabilities}
        assert CapabilityLevel.BASIC in cap_levels

    def test_unregister_existing(self) -> None:
        reg = CapabilityRegistry()
        reg.register("claude", "opus", frozenset())
        assert reg.unregister("claude", "opus") is True
        assert len(reg.agents) == 0

    def test_unregister_missing(self) -> None:
        reg = CapabilityRegistry()
        assert reg.unregister("nonexistent", "model") is False


# ---------------------------------------------------------------------------
# CapabilityRegistry — find_agents
# ---------------------------------------------------------------------------


class TestRegistryFindAgents:
    """Tests for find_agents scoring and ranking."""

    def test_perfect_match_scores_1(self) -> None:
        reg = CapabilityRegistry()
        reg.register("claude", "opus", frozenset({_expert("python"), _expert("testing")}))
        results = reg.find_agents([_basic("python"), _basic("testing")])
        assert len(results) == 1
        assert results[0].score == pytest.approx(1.0)
        assert len(results[0].missing_capabilities) == 0

    def test_partial_match(self) -> None:
        reg = CapabilityRegistry()
        reg.register("claude", "opus", frozenset({_expert("python")}))
        results = reg.find_agents([_basic("python"), _basic("testing")])
        assert len(results) == 1
        assert results[0].score == pytest.approx(0.5)
        assert len(results[0].matched_capabilities) == 1
        assert len(results[0].missing_capabilities) == 1

    def test_level_too_low_counts_as_missing(self) -> None:
        reg = CapabilityRegistry()
        reg.register("claude", "haiku", frozenset({_basic("python")}))
        results = reg.find_agents([_expert("python")])
        assert results[0].score == pytest.approx(0.0)
        assert len(results[0].missing_capabilities) == 1

    def test_higher_level_satisfies_lower_requirement(self) -> None:
        reg = CapabilityRegistry()
        reg.register("claude", "opus", frozenset({_expert("python")}))
        results = reg.find_agents([_basic("python")])
        assert results[0].score == pytest.approx(1.0)

    def test_ranking_by_score(self) -> None:
        reg = CapabilityRegistry()
        reg.register(
            "claude",
            "opus",
            frozenset({_expert("python"), _expert("testing"), _expert("security")}),
        )
        reg.register("codex", "gpt-4", frozenset({_advanced("python")}))
        results = reg.find_agents([_basic("python"), _basic("testing"), _basic("security")])
        assert results[0].agent.adapter_name == "claude"
        assert results[0].score == pytest.approx(1.0)
        assert results[1].agent.adapter_name == "codex"

    def test_deterministic_tiebreak_by_adapter_name(self) -> None:
        reg = CapabilityRegistry()
        reg.register("beta", "m1", frozenset({_expert("python")}))
        reg.register("alpha", "m1", frozenset({_expert("python")}))
        results = reg.find_agents([_basic("python")])
        assert results[0].agent.adapter_name == "alpha"
        assert results[1].agent.adapter_name == "beta"

    def test_min_score_filter(self) -> None:
        reg = CapabilityRegistry()
        reg.register("claude", "opus", frozenset({_expert("python"), _expert("testing")}))
        reg.register("codex", "gpt-4", frozenset({_basic("python")}))
        results = reg.find_agents(
            [_basic("python"), _basic("testing")],
            min_score=0.8,
        )
        assert len(results) == 1
        assert results[0].agent.adapter_name == "claude"

    def test_empty_requirements_returns_all(self) -> None:
        reg = CapabilityRegistry()
        reg.register("claude", "opus", frozenset({_expert("python")}))
        reg.register("codex", "gpt-4", frozenset())
        results = reg.find_agents([])
        assert len(results) == 2
        assert all(m.score == pytest.approx(1.0) for m in results)

    def test_empty_registry_returns_empty(self) -> None:
        reg = CapabilityRegistry()
        results = reg.find_agents([_basic("python")])
        assert results == []

    def test_no_capability_match(self) -> None:
        reg = CapabilityRegistry()
        reg.register("claude", "opus", frozenset({_expert("python")}))
        results = reg.find_agents([_basic("quantum-computing")])
        assert results[0].score == pytest.approx(0.0)
        assert len(results[0].missing_capabilities) == 1

    def test_multi_capability_scoring(self) -> None:
        reg = CapabilityRegistry()
        reg.register(
            "claude",
            "opus",
            frozenset({_expert("python"), _expert("testing"), _advanced("devops")}),
        )
        results = reg.find_agents(
            [
                _basic("python"),
                _basic("testing"),
                _expert("devops"),
                _basic("security"),
            ]
        )
        # 2 of 4 match (python + testing), devops is advanced but expert required
        assert results[0].score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# CapabilityRegistry — best_match
# ---------------------------------------------------------------------------


class TestRegistryBestMatch:
    """Tests for best_match convenience method."""

    def test_returns_best(self) -> None:
        reg = CapabilityRegistry()
        reg.register("claude", "opus", frozenset({_expert("python"), _expert("testing")}))
        reg.register("codex", "gpt-4", frozenset({_basic("python")}))
        result = reg.best_match([_basic("python"), _basic("testing")])
        assert result is not None
        assert result.agent.adapter_name == "claude"

    def test_none_when_empty_registry(self) -> None:
        reg = CapabilityRegistry()
        assert reg.best_match([_basic("python")]) is None


# ---------------------------------------------------------------------------
# Default profiles (opus / sonnet / haiku)
# ---------------------------------------------------------------------------


class TestDefaultProfiles:
    """Tests for build_default_profiles and populate_registry_defaults."""

    def test_build_returns_three_profiles(self) -> None:
        profiles = build_default_profiles()
        assert len(profiles) == 3
        names = {p.model for p in profiles}
        assert "claude-opus-4-0520" in names
        assert "claude-sonnet-4-0520" in names
        assert "claude-haiku" in names

    def test_opus_all_expert(self) -> None:
        profiles = build_default_profiles()
        opus = next(p for p in profiles if "opus" in p.model)
        for cap in opus.capabilities:
            assert cap.level == CapabilityLevel.EXPERT, f"{cap.name} should be expert"

    def test_sonnet_has_advanced_capabilities(self) -> None:
        profiles = build_default_profiles()
        sonnet = next(p for p in profiles if "sonnet" in p.model)
        advanced_count = sum(1 for c in sonnet.capabilities if c.level == CapabilityLevel.ADVANCED)
        assert advanced_count > 0

    def test_haiku_all_basic(self) -> None:
        profiles = build_default_profiles()
        haiku = next(p for p in profiles if "haiku" in p.model)
        for cap in haiku.capabilities:
            assert cap.level == CapabilityLevel.BASIC, f"{cap.name} should be basic"

    def test_haiku_fewer_capabilities_than_opus(self) -> None:
        profiles = build_default_profiles()
        opus = next(p for p in profiles if "opus" in p.model)
        haiku = next(p for p in profiles if "haiku" in p.model)
        assert len(haiku.capabilities) < len(opus.capabilities)

    def test_populate_registry_defaults(self) -> None:
        reg = CapabilityRegistry()
        populate_registry_defaults(reg)
        assert len(reg.agents) == 3

    def test_opus_beats_haiku_for_expert_python(self) -> None:
        reg = CapabilityRegistry()
        populate_registry_defaults(reg)
        result = reg.best_match([_expert("python")])
        assert result is not None
        assert "opus" in result.agent.model
