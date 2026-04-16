"""Tests for OpenTelemetry semantic conventions module."""

from __future__ import annotations

from bernstein.core.observability.otel_conventions import (
    ATTR_AGENT_MODEL,
    ATTR_AGENT_NAME,
    ATTR_AGENT_ROLE,
    ATTR_COST_USD,
    ATTR_GATE_NAME,
    ATTR_GATE_RESULT,
    ATTR_MERGE_BRANCH,
    ATTR_MERGE_SHA,
    ATTR_RUN_ID,
    ATTR_STEP_COUNT,
    ATTR_TASK_COMPLEXITY,
    ATTR_TASK_GOAL,
    ATTR_TASK_ID,
    SPAN_ASSIGN,
    SPAN_EXECUTE,
    SPAN_GATE,
    SPAN_MERGE,
    SPAN_PLAN,
    SPAN_RUN,
    SPAN_SPAWN,
    SPAN_TICK,
    SpanAttributes,
)


class TestAttributeConstants:
    """All ATTR_* constants must be non-empty strings."""

    def test_all_attr_constants_are_nonempty_strings(self) -> None:
        attrs = [
            ATTR_AGENT_NAME,
            ATTR_AGENT_MODEL,
            ATTR_AGENT_ROLE,
            ATTR_TASK_ID,
            ATTR_TASK_GOAL,
            ATTR_TASK_COMPLEXITY,
            ATTR_COST_USD,
            ATTR_GATE_NAME,
            ATTR_GATE_RESULT,
            ATTR_MERGE_BRANCH,
            ATTR_MERGE_SHA,
            ATTR_RUN_ID,
            ATTR_STEP_COUNT,
        ]
        for attr in attrs:
            assert isinstance(attr, str)
            assert len(attr) > 0


class TestSpanConstants:
    """All SPAN_* constants must be defined and non-empty."""

    def test_all_span_constants_defined(self) -> None:
        spans = [
            SPAN_RUN,
            SPAN_TICK,
            SPAN_PLAN,
            SPAN_ASSIGN,
            SPAN_SPAWN,
            SPAN_EXECUTE,
            SPAN_GATE,
            SPAN_MERGE,
        ]
        for span in spans:
            assert isinstance(span, str)
            assert len(span) > 0

    def test_span_hierarchy_naming(self) -> None:
        assert SPAN_RUN.startswith("run.")
        assert SPAN_TICK.startswith("run.")
        assert SPAN_PLAN.startswith("task.")
        assert SPAN_ASSIGN.startswith("task.")
        assert SPAN_SPAWN.startswith("agent.")
        assert SPAN_EXECUTE.startswith("agent.")
        assert SPAN_GATE.startswith("quality.")
        assert SPAN_MERGE.startswith("git.")


class TestSpanAttributesForTask:
    """SpanAttributes.for_task produces correct keys."""

    def test_minimal(self) -> None:
        sa = SpanAttributes.for_task(task_id="t-1")
        assert sa.attrs[ATTR_TASK_ID] == "t-1"
        assert ATTR_TASK_GOAL not in sa.attrs
        assert ATTR_TASK_COMPLEXITY not in sa.attrs
        assert ATTR_AGENT_ROLE not in sa.attrs

    def test_full(self) -> None:
        sa = SpanAttributes.for_task(
            task_id="t-2",
            goal="Implement feature X",
            complexity="medium",
            role="backend",
        )
        assert sa.attrs[ATTR_TASK_ID] == "t-2"
        assert sa.attrs[ATTR_TASK_GOAL] == "Implement feature X"
        assert sa.attrs[ATTR_TASK_COMPLEXITY] == "medium"
        assert sa.attrs[ATTR_AGENT_ROLE] == "backend"

    def test_goal_truncation_at_200_chars(self) -> None:
        long_goal = "x" * 300
        sa = SpanAttributes.for_task(task_id="t-3", goal=long_goal)
        assert len(str(sa.attrs[ATTR_TASK_GOAL])) == 200

    def test_goal_exactly_200_not_truncated(self) -> None:
        goal = "y" * 200
        sa = SpanAttributes.for_task(task_id="t-4", goal=goal)
        assert sa.attrs[ATTR_TASK_GOAL] == goal


class TestSpanAttributesForAgent:
    """SpanAttributes.for_agent produces correct keys."""

    def test_all_keys_present(self) -> None:
        sa = SpanAttributes.for_agent(
            agent_name="agent-1",
            model="claude-sonnet-4-20250514",
            role="backend",
            task_id="t-10",
        )
        assert sa.attrs[ATTR_AGENT_NAME] == "agent-1"
        assert sa.attrs[ATTR_AGENT_MODEL] == "claude-sonnet-4-20250514"
        assert sa.attrs[ATTR_AGENT_ROLE] == "backend"
        assert sa.attrs[ATTR_TASK_ID] == "t-10"


class TestSpanAttributesForGate:
    """SpanAttributes.for_gate with pass/fail results."""

    def test_pass_result(self) -> None:
        sa = SpanAttributes.for_gate(gate_name="lint", result="pass", task_id="t-20")
        assert sa.attrs[ATTR_GATE_NAME] == "lint"
        assert sa.attrs[ATTR_GATE_RESULT] == "pass"
        assert sa.attrs[ATTR_TASK_ID] == "t-20"

    def test_fail_result(self) -> None:
        sa = SpanAttributes.for_gate(gate_name="tests", result="fail", task_id="t-21")
        assert sa.attrs[ATTR_GATE_RESULT] == "fail"


class TestSpanAttributesForMerge:
    """SpanAttributes.for_merge produces correct keys."""

    def test_merge_attrs(self) -> None:
        sa = SpanAttributes.for_merge(
            branch="feat/foo",
            commit_sha="abc123",
            task_id="t-30",
        )
        assert sa.attrs[ATTR_MERGE_BRANCH] == "feat/foo"
        assert sa.attrs[ATTR_MERGE_SHA] == "abc123"
        assert sa.attrs[ATTR_TASK_ID] == "t-30"


class TestChaining:
    """with_cost and with_steps chain correctly."""

    def test_with_cost(self) -> None:
        sa = SpanAttributes.for_task(task_id="t-40").with_cost(0.123456789)
        assert sa.attrs[ATTR_COST_USD] == 0.123457  # rounded to 6 decimals

    def test_with_steps(self) -> None:
        sa = SpanAttributes.for_task(task_id="t-41").with_steps(42)
        assert sa.attrs[ATTR_STEP_COUNT] == 42

    def test_chaining_both(self) -> None:
        sa = (
            SpanAttributes.for_agent(
                agent_name="a",
                model="m",
                role="r",
                task_id="t-42",
            )
            .with_cost(1.5)
            .with_steps(10)
        )
        assert sa.attrs[ATTR_COST_USD] == 1.5
        assert sa.attrs[ATTR_STEP_COUNT] == 10
        # Original keys still present
        assert sa.attrs[ATTR_AGENT_NAME] == "a"
