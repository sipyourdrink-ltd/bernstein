"""Capability-based addressing for agents — find by skill, not by name.

Instead of assigning tasks to specific adapters/models, tasks specify
required capabilities: ``requires: [python, testing, refactoring]``.
The router matches capabilities to available agents, decoupling task
definitions from specific providers.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.agent_discovery import AgentCapabilities, DiscoveryResult

logger = logging.getLogger(__name__)

# Canonical capability names and their synonyms
_CAPABILITY_ALIASES: dict[str, str] = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "jsx": "javascript",
    "react": "frontend",
    "vue": "frontend",
    "svelte": "frontend",
    "css": "frontend",
    "html": "frontend",
    "ui": "frontend",
    "api": "backend",
    "database": "backend",
    "sql": "backend",
    "db": "backend",
    "rest": "backend",
    "graphql": "backend",
    "test": "testing",
    "tests": "testing",
    "pytest": "testing",
    "jest": "testing",
    "unittest": "testing",
    "ci": "devops",
    "cd": "devops",
    "docker": "devops",
    "k8s": "devops",
    "kubernetes": "devops",
    "terraform": "devops",
    "infra": "devops",
    "infrastructure": "devops",
    "deploy": "devops",
    "deployment": "devops",
    "sec": "security",
    "auth": "security",
    "crypto": "security",
    "encryption": "security",
    "vulnerability": "security",
    "refactor": "refactoring",
    "cleanup": "refactoring",
    "restructure": "refactoring",
    "review": "code-review",
    "code_review": "code-review",
    "lint": "code-review",
    "architecture": "design",
    "architect": "design",
    "design-patterns": "design",
    "docs": "documentation",
    "readme": "documentation",
    "docstring": "documentation",
    "markdown": "documentation",
    "ml": "machine-learning",
    "ai": "machine-learning",
    "model-training": "machine-learning",
    "data-science": "machine-learning",
}

# Maps canonical capabilities to the agent best_for tags they match
_CAPABILITY_TO_BEST_FOR: dict[str, set[str]] = {
    "python": {"code-generation", "complex-refactoring", "code-modification"},
    "javascript": {"frontend", "full-stack", "code-generation"},
    "typescript": {"frontend", "full-stack", "code-generation"},
    "frontend": {"frontend", "full-stack", "multimodal"},
    "backend": {"code-generation", "full-stack", "complex-refactoring"},
    "testing": {"test-writing", "code-review", "quick-fixes"},
    "devops": {"automation", "headless-runs"},
    "security": {"security-review", "architecture", "tool-use"},
    "refactoring": {"complex-refactoring", "code-modification", "refactoring"},
    "code-review": {"code-review", "security-review"},
    "design": {"architecture", "tool-use", "complex-refactoring"},
    "documentation": {"frontend", "full-stack"},
    "machine-learning": {"code-generation", "reasoning-tasks"},
    "long-context": {"long-context"},
    "tool-use": {"tool-use"},
    "reasoning": {"reasoning-tasks"},
    "fast": {"quick-fixes", "fast-tasks"},
    "cheap": {"free-tier"},
    "headless": {"headless-runs"},
    "sandbox": set(),
    "mcp": set(),
}


def normalize_capability(cap: str) -> str:
    """Normalize a capability name to its canonical form."""
    cleaned = cap.strip().lower().replace(" ", "-").replace("_", "-")
    return _CAPABILITY_ALIASES.get(cleaned, cleaned)


def infer_capabilities_from_description(description: str) -> list[str]:
    """Infer required capabilities from a task description using keyword analysis."""
    text = description.lower()
    tokens = set(re.findall(r"\b\w+\b", text))
    inferred: set[str] = set()

    keyword_map: dict[str, list[str]] = {
        "python": ["python", "pytest", "pip", "pyright", "ruff", "mypy", "django", "flask", "fastapi"],
        "javascript": ["javascript", "node", "npm", "yarn", "webpack", "eslint"],
        "typescript": ["typescript", "tsx", "tsc"],
        "frontend": ["react", "vue", "svelte", "css", "html", "tailwind", "component", "ui", "ux"],
        "backend": ["api", "endpoint", "database", "sql", "migration", "server", "route", "handler"],
        "testing": ["test", "tests", "spec", "coverage", "assert", "mock", "fixture"],
        "devops": ["docker", "kubernetes", "ci", "cd", "pipeline", "deploy", "terraform", "ansible"],
        "security": ["security", "vulnerability", "auth", "oauth", "jwt", "encryption", "xss", "csrf"],
        "refactoring": ["refactor", "cleanup", "restructure", "rename", "extract", "simplify"],
        "code-review": ["review", "lint", "quality", "standards"],
        "design": ["architecture", "design", "pattern", "interface", "abstraction"],
        "documentation": ["docs", "readme", "document", "docstring", "changelog"],
        "machine-learning": ["model", "training", "inference", "embedding", "neural", "ml", "ai"],
    }

    for cap, keywords in keyword_map.items():
        if tokens & set(keywords):
            inferred.add(cap)

    return sorted(inferred)


@dataclass(frozen=True)
class CapabilityMatch:
    """Result of matching a task's required capabilities to an agent."""

    agent_name: str
    model: str
    match_score: float  # 0.0 to 1.0
    matched_capabilities: list[str]
    missing_capabilities: list[str]
    reason: str


@dataclass
class CapabilityRouter:
    """Routes tasks to agents based on required capabilities.

    Attributes:
        discovery: Cached agent discovery result.
        _agent_caps: Precomputed capability sets per agent.
    """

    discovery: DiscoveryResult
    _agent_caps: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._build_agent_capability_index()

    @staticmethod
    def _caps_for_agent(agent: object) -> set[str]:
        """Derive capabilities for a single agent based on its properties."""
        caps: set[str] = set()

        # Feature-based capabilities
        if getattr(agent, "supports_headless", False):
            caps.add("headless")
        if getattr(agent, "supports_sandbox", False):
            caps.add("sandbox")
        if getattr(agent, "supports_mcp", False):
            caps.update(("mcp", "tool-use"))

        # Reasoning strength
        reasoning = getattr(agent, "reasoning_strength", "")
        if reasoning in ("high", "very_high"):
            caps.update(("reasoning", "design", "security", "refactoring"))
        if reasoning == "very_high":
            caps.add("code-review")

        # Cost tier
        if getattr(agent, "cost_tier", "") in ("free", "cheap"):
            caps.update(("cheap", "fast"))

        # Context window
        if getattr(agent, "max_context_tokens", 0) >= 500_000:
            caps.add("long-context")

        # best_for tags → capabilities (reverse mapping)
        for bf_tag in getattr(agent, "best_for", ()):
            caps.add(bf_tag)
            for cap, bf_set in _CAPABILITY_TO_BEST_FOR.items():
                if bf_tag in bf_set:
                    caps.add(cap)

        # All agents can do basic coding
        caps.update(("python", "javascript", "typescript", "backend"))
        return caps

    def _build_agent_capability_index(self) -> None:
        """Build a capability set for each discovered agent."""
        for agent in self.discovery.agents:
            if not agent.logged_in:
                continue
            self._agent_caps[agent.name] = self._caps_for_agent(agent)

    def match(
        self,
        required: list[str],
        preferred_agent: str | None = None,
        min_score: float = 0.0,
    ) -> list[CapabilityMatch]:
        """Match required capabilities to available agents.

        Args:
            required: List of required capability names (will be normalized).
            preferred_agent: Optional agent name to boost in ranking.
            min_score: Minimum match score to include (0.0 to 1.0).

        Returns:
            List of CapabilityMatch sorted by score descending.
        """
        normalized = [normalize_capability(c) for c in required]
        if not normalized:
            return self._all_agents_default()

        matches: list[CapabilityMatch] = []
        for agent in self.discovery.agents:
            if not agent.logged_in:
                continue
            agent_caps = self._agent_caps.get(agent.name, set())
            matched = [c for c in normalized if c in agent_caps]
            missing = [c for c in normalized if c not in agent_caps]

            score = len(matched) / len(normalized) if normalized else 0.0

            # Boost preferred agent slightly
            if preferred_agent and agent.name == preferred_agent:
                score = min(1.0, score + 0.1)

            if score < min_score:
                continue

            # Pick best model for the required capabilities
            model = self._select_model_for_caps(agent, normalized)

            reason = self._build_reason(agent, matched, missing)
            matches.append(
                CapabilityMatch(
                    agent_name=agent.name,
                    model=model,
                    match_score=round(score, 3),
                    matched_capabilities=matched,
                    missing_capabilities=missing,
                    reason=reason,
                )
            )

        matches.sort(key=lambda m: m.match_score, reverse=True)
        return matches

    def best_match(
        self,
        required: list[str],
        preferred_agent: str | None = None,
    ) -> CapabilityMatch | None:
        """Return the single best matching agent, or None if no match."""
        matches = self.match(required, preferred_agent=preferred_agent)
        return matches[0] if matches else None

    def _select_model_for_caps(self, agent: AgentCapabilities, caps: list[str]) -> str:
        """Pick the best model on this agent for the required capabilities."""
        needs_strong = any(c in ("design", "security", "refactoring", "code-review", "reasoning") for c in caps)
        needs_cheap = any(c in ("cheap", "fast") for c in caps)
        needs_long_ctx = "long-context" in caps

        if needs_long_ctx and agent.max_context_tokens >= 500_000:
            return agent.default_model

        if needs_strong and len(agent.available_models) > 1:
            # Prefer the strongest model
            return agent.available_models[0]

        if needs_cheap and len(agent.available_models) > 1:
            # Prefer the cheapest (usually last in list or has "mini"/"flash"/"haiku")
            for m in reversed(agent.available_models):
                if any(tag in m.lower() for tag in ("mini", "flash", "haiku", "turbo", "small")):
                    return m
            return agent.available_models[-1]

        return agent.default_model

    def _all_agents_default(self) -> list[CapabilityMatch]:
        """When no capabilities specified, return all agents with default score."""
        return [
            CapabilityMatch(
                agent_name=agent.name,
                model=agent.default_model,
                match_score=0.5,
                matched_capabilities=[],
                missing_capabilities=[],
                reason="no capabilities specified, any agent can handle",
            )
            for agent in self.discovery.agents
            if agent.logged_in
        ]

    @staticmethod
    def _build_reason(
        agent: AgentCapabilities,
        matched: list[str],
        missing: list[str],
    ) -> str:
        parts: list[str] = []
        if matched:
            parts.append(f"matches: {', '.join(matched[:3])}")
        if missing:
            parts.append(f"missing: {', '.join(missing[:3])}")
        if agent.cost_tier == "free":
            parts.append("free tier")
        return "; ".join(parts) if parts else "available"
