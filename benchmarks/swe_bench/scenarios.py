"""Evaluation scenario definitions for the SWE-Bench scaffolding thesis.

Four configurations are compared:
  solo-sonnet      — single Claude Sonnet agent, cheap baseline
  solo-opus        — single Claude Opus agent, expensive baseline
  bernstein-sonnet — Bernstein 3-agent pipeline, all Sonnet
  bernstein-mixed  — Bernstein 3-agent pipeline, cost-optimised model mix

The Bernstein scenarios use an analyst → implementer → qa pipeline:
  - analyst   (Haiku/Sonnet): reads the issue, identifies files, writes a plan
  - implementer (Sonnet):     applies the code change following the plan
  - qa          (Haiku):      reviews the diff and writes a one-line verdict

Cost estimates (USD per 1 k output tokens, 2025 rack rates):
  haiku   $0.00125
  sonnet  $0.015
  opus    $0.075
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Model cost constants (USD / 1 k tokens, blended input+output at ~1:3 ratio)
# ---------------------------------------------------------------------------
_HAIKU_COST_PER_1K: float = 0.00125
_SONNET_COST_PER_1K: float = 0.015
_OPUS_COST_PER_1K: float = 0.075


@dataclass(frozen=True)
class AgentRole:
    """One agent slot in a scenario pipeline."""

    role: str  # analyst | implementer | qa
    model: str  # haiku | sonnet | opus
    effort: str  # low | high | max
    cost_per_1k_tokens: float

    def estimate_cost(self, estimated_tokens: int) -> float:
        """Estimate cost for this agent given a token budget."""
        return (estimated_tokens / 1000.0) * self.cost_per_1k_tokens


@dataclass(frozen=True)
class Scenario:
    """A complete evaluation configuration."""

    name: str
    description: str
    agents: list[AgentRole]
    # Rough token budget per agent per instance (used for cost projection only)
    tokens_per_agent: int = 4_000

    @property
    def estimated_cost_per_instance(self) -> float:
        """Projected USD cost for one SWE-Bench instance."""
        return sum(a.estimate_cost(self.tokens_per_agent) for a in self.agents)

    @property
    def agent_count(self) -> int:
        return len(self.agents)


# ---------------------------------------------------------------------------
# The four canonical scenarios
# ---------------------------------------------------------------------------

SOLO_SONNET = Scenario(
    name="solo-sonnet",
    description="Single Claude Sonnet agent — cheap baseline.",
    agents=[
        AgentRole(
            role="implementer",
            model="sonnet",
            effort="high",
            cost_per_1k_tokens=_SONNET_COST_PER_1K,
        )
    ],
    tokens_per_agent=8_000,
)

SOLO_OPUS = Scenario(
    name="solo-opus",
    description="Single Claude Opus agent — expensive baseline.",
    agents=[
        AgentRole(
            role="implementer",
            model="opus",
            effort="max",
            cost_per_1k_tokens=_OPUS_COST_PER_1K,
        )
    ],
    tokens_per_agent=8_000,
)

BERNSTEIN_SONNET = Scenario(
    name="bernstein-sonnet",
    description="Bernstein 3-agent pipeline, all Sonnet — core thesis.",
    agents=[
        AgentRole(
            role="analyst",
            model="sonnet",
            effort="high",
            cost_per_1k_tokens=_SONNET_COST_PER_1K,
        ),
        AgentRole(
            role="implementer",
            model="sonnet",
            effort="high",
            cost_per_1k_tokens=_SONNET_COST_PER_1K,
        ),
        AgentRole(
            role="qa",
            model="sonnet",
            effort="high",
            cost_per_1k_tokens=_SONNET_COST_PER_1K,
        ),
    ],
    tokens_per_agent=4_000,
)

BERNSTEIN_MIXED = Scenario(
    name="bernstein-mixed",
    description=("Bernstein 3-agent pipeline, cost-optimised: Haiku analyst, Sonnet implementer, Haiku QA."),
    agents=[
        AgentRole(
            role="analyst",
            model="haiku",
            effort="high",
            cost_per_1k_tokens=_HAIKU_COST_PER_1K,
        ),
        AgentRole(
            role="implementer",
            model="sonnet",
            effort="high",
            cost_per_1k_tokens=_SONNET_COST_PER_1K,
        ),
        AgentRole(
            role="qa",
            model="haiku",
            effort="high",
            cost_per_1k_tokens=_HAIKU_COST_PER_1K,
        ),
    ],
    tokens_per_agent=4_000,
)

# Ordered list for the evaluation loop
ALL_SCENARIOS: list[Scenario] = [
    SOLO_SONNET,
    SOLO_OPUS,
    BERNSTEIN_SONNET,
    BERNSTEIN_MIXED,
]

SCENARIO_BY_NAME: dict[str, Scenario] = {s.name: s for s in ALL_SCENARIOS}
