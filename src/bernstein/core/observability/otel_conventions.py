"""OpenTelemetry semantic conventions for Bernstein agent spans.

Defines attribute keys and span helpers following the emerging
GenAI SIG conventions for agent observability.
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Semantic attribute keys (following OTel GenAI SIG naming)
# ---------------------------------------------------------------------------

ATTR_AGENT_NAME = "gen_ai.agent.name"
ATTR_AGENT_MODEL = "gen_ai.agent.model"
ATTR_AGENT_ROLE = "gen_ai.agent.role"
ATTR_TASK_ID = "bernstein.task.id"
ATTR_TASK_GOAL = "bernstein.task.goal"
ATTR_TASK_COMPLEXITY = "bernstein.task.complexity"
ATTR_COST_USD = "bernstein.cost.usd"
ATTR_GATE_NAME = "bernstein.quality.gate"
ATTR_GATE_RESULT = "bernstein.quality.result"
ATTR_MERGE_BRANCH = "bernstein.git.branch"
ATTR_MERGE_SHA = "bernstein.git.commit_sha"
ATTR_RUN_ID = "bernstein.run.id"
ATTR_STEP_COUNT = "bernstein.agent.step_count"

# ---------------------------------------------------------------------------
# Span names following hierarchy:
# run.orchestration > run.tick > task.plan > task.assign
#   > agent.spawn > agent.execute > quality.gate > git.merge
# ---------------------------------------------------------------------------

SPAN_RUN = "run.orchestration"
SPAN_TICK = "run.tick"
SPAN_PLAN = "task.plan"
SPAN_ASSIGN = "task.assign"
SPAN_SPAWN = "agent.spawn"
SPAN_EXECUTE = "agent.execute"
SPAN_GATE = "quality.gate"
SPAN_MERGE = "git.merge"

#: Maximum length for goal text stored in span attributes.
_GOAL_MAX_LEN = 200


# ---------------------------------------------------------------------------
# Span attribute helpers
# ---------------------------------------------------------------------------


@dataclass
class SpanAttributes:
    """Helper to build consistent span attributes."""

    attrs: dict[str, str | int | float | bool]

    @classmethod
    def for_task(
        cls,
        task_id: str,
        goal: str = "",
        complexity: str = "",
        role: str = "",
    ) -> SpanAttributes:
        """Build attributes for a task-related span.

        Args:
            task_id: Unique task identifier.
            goal: Human-readable task goal (truncated to 200 chars).
            complexity: Task complexity label.
            role: Agent role assigned to the task.

        Returns:
            A ``SpanAttributes`` instance with the relevant keys populated.
        """
        attrs: dict[str, str | int | float | bool] = {ATTR_TASK_ID: task_id}
        if goal:
            attrs[ATTR_TASK_GOAL] = goal[:_GOAL_MAX_LEN]
        if complexity:
            attrs[ATTR_TASK_COMPLEXITY] = complexity
        if role:
            attrs[ATTR_AGENT_ROLE] = role
        return cls(attrs=attrs)

    @classmethod
    def for_agent(
        cls,
        agent_name: str,
        model: str,
        role: str,
        task_id: str,
    ) -> SpanAttributes:
        """Build attributes for an agent lifecycle span.

        Args:
            agent_name: Display name of the agent.
            model: Model identifier used by the agent.
            role: Role the agent is fulfilling.
            task_id: Task the agent is working on.

        Returns:
            A ``SpanAttributes`` instance with agent-specific keys.
        """
        return cls(
            attrs={
                ATTR_AGENT_NAME: agent_name,
                ATTR_AGENT_MODEL: model,
                ATTR_AGENT_ROLE: role,
                ATTR_TASK_ID: task_id,
            },
        )

    @classmethod
    def for_gate(
        cls,
        gate_name: str,
        result: str,
        task_id: str,
    ) -> SpanAttributes:
        """Build attributes for a quality gate span.

        Args:
            gate_name: Name of the quality gate.
            result: Gate outcome (e.g. ``"pass"`` or ``"fail"``).
            task_id: Task the gate evaluated.

        Returns:
            A ``SpanAttributes`` instance with gate-specific keys.
        """
        return cls(
            attrs={
                ATTR_GATE_NAME: gate_name,
                ATTR_GATE_RESULT: result,
                ATTR_TASK_ID: task_id,
            },
        )

    @classmethod
    def for_merge(
        cls,
        branch: str,
        commit_sha: str,
        task_id: str,
    ) -> SpanAttributes:
        """Build attributes for a git merge span.

        Args:
            branch: Branch being merged.
            commit_sha: Resulting commit SHA.
            task_id: Task that triggered the merge.

        Returns:
            A ``SpanAttributes`` instance with merge-specific keys.
        """
        return cls(
            attrs={
                ATTR_MERGE_BRANCH: branch,
                ATTR_MERGE_SHA: commit_sha,
                ATTR_TASK_ID: task_id,
            },
        )

    def with_cost(self, cost_usd: float) -> SpanAttributes:
        """Attach cost information to the span attributes.

        Args:
            cost_usd: Cost in US dollars.

        Returns:
            ``self`` for chaining.
        """
        self.attrs[ATTR_COST_USD] = round(cost_usd, 6)
        return self

    def with_steps(self, count: int) -> SpanAttributes:
        """Attach step count to the span attributes.

        Args:
            count: Number of steps executed by the agent.

        Returns:
            ``self`` for chaining.
        """
        self.attrs[ATTR_STEP_COUNT] = count
        return self
