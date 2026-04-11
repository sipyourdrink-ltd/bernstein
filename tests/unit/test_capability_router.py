"""Tests for capability-based agent addressing and routing."""

from __future__ import annotations

import pytest

from bernstein.core.agent_discovery import AgentCapabilities, DiscoveryResult
from bernstein.core.capability_router import (
    CapabilityMatch,
    CapabilityRouter,
    infer_capabilities_from_description,
    normalize_capability,
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
