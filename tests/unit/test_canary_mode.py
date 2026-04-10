"""Tests for orchestrator canary mode (ORCH-021)."""

from __future__ import annotations

from typing import Any

from bernstein.core.canary_mode import (
    build_canary_report,
    compare_decisions,
    format_canary_report,
    simulate_routing,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str = "t-1",
    role: str = "backend",
    complexity: str = "medium",
    scope: str = "medium",
    priority: int = 3,
    **extra: Any,
) -> dict[str, Any]:
    """Build a minimal task dict for testing."""
    d: dict[str, Any] = {
        "id": task_id,
        "role": role,
        "complexity": complexity,
        "scope": scope,
        "priority": priority,
    }
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# simulate_routing
# ---------------------------------------------------------------------------


class TestSimulateRouting:
    """Tests for simulate_routing."""

    def test_default_adapter(self) -> None:
        """Default adapter is 'claude' when config has none."""
        decision = simulate_routing(_task(), {})
        assert decision.adapter == "claude"

    def test_config_adapter_override(self) -> None:
        """Config can override the adapter."""
        decision = simulate_routing(_task(), {"adapter": "codex"})
        assert decision.adapter == "codex"

    def test_config_model_override(self) -> None:
        """Config-level model/effort overrides take highest precedence."""
        decision = simulate_routing(_task(), {"model": "gpt-4.1", "effort": "low"})
        assert decision.model == "gpt-4.1"
        assert decision.effort == "low"
        assert decision.reason == "config override"

    def test_task_model_override(self) -> None:
        """Task-level model override takes precedence over heuristics."""
        task = _task(model="custom-model", effort="normal")
        decision = simulate_routing(task, {})
        assert decision.model == "custom-model"
        assert decision.effort == "normal"
        assert decision.reason == "task override"

    def test_critical_priority(self) -> None:
        """Priority 1 tasks get opus/max."""
        decision = simulate_routing(_task(priority=1), {})
        assert decision.model == "opus"
        assert decision.effort == "max"
        assert decision.reason == "critical priority"

    def test_role_manager(self) -> None:
        """Manager role gets opus/max."""
        decision = simulate_routing(_task(role="manager"), {})
        assert decision.model == "opus"
        assert decision.effort == "max"
        assert "role=manager" in decision.reason

    def test_role_architect(self) -> None:
        """Architect role gets opus/max."""
        decision = simulate_routing(_task(role="architect"), {})
        assert decision.model == "opus"
        assert decision.effort == "max"

    def test_role_security(self) -> None:
        """Security role gets opus/max."""
        decision = simulate_routing(_task(role="security"), {})
        assert decision.model == "opus"
        assert decision.effort == "max"

    def test_large_scope(self) -> None:
        """Large scope tasks get opus/max."""
        decision = simulate_routing(_task(scope="large"), {})
        assert decision.model == "opus"
        assert decision.effort == "max"
        assert "scope=large" in decision.reason

    def test_high_complexity_fallback(self) -> None:
        """High complexity tasks get sonnet/high via heuristic."""
        decision = simulate_routing(_task(complexity="high"), {})
        assert decision.model == "sonnet"
        assert decision.effort == "high"

    def test_low_complexity_fallback(self) -> None:
        """Low complexity tasks get haiku/low via heuristic."""
        decision = simulate_routing(_task(complexity="low"), {})
        assert decision.model == "haiku"
        assert decision.effort == "low"

    def test_would_spawn_is_true(self) -> None:
        """Simulated tasks always report would_spawn=True."""
        decision = simulate_routing(_task(), {})
        assert decision.would_spawn is True

    def test_task_id_preserved(self) -> None:
        """Task ID is carried through to the decision."""
        decision = simulate_routing(_task(task_id="abc-123"), {})
        assert decision.task_id == "abc-123"

    def test_missing_task_id(self) -> None:
        """Missing task ID defaults to 'unknown'."""
        decision = simulate_routing({}, {})
        assert decision.task_id == "unknown"

    def test_frozen_dataclass(self) -> None:
        """CanaryDecision is immutable."""
        decision = simulate_routing(_task(), {})
        try:
            decision.model = "changed"  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised


# ---------------------------------------------------------------------------
# compare_decisions
# ---------------------------------------------------------------------------


class TestCompareDecisions:
    """Tests for compare_decisions."""

    def test_matching_decisions(self) -> None:
        """Identical routing produces matches=True."""
        task = _task()
        primary = [simulate_routing(task, {})]
        canary = [simulate_routing(task, {})]
        diffs = compare_decisions(primary, canary)
        assert len(diffs) == 1
        assert diffs[0].matches is True

    def test_differing_adapter(self) -> None:
        """Different adapters produce matches=False."""
        task = _task()
        primary = [simulate_routing(task, {"adapter": "claude"})]
        canary = [simulate_routing(task, {"adapter": "codex"})]
        diffs = compare_decisions(primary, canary)
        assert len(diffs) == 1
        assert diffs[0].matches is False
        assert diffs[0].primary_adapter == "claude"
        assert diffs[0].canary_adapter == "codex"

    def test_differing_model(self) -> None:
        """Different models produce matches=False."""
        task = _task()
        primary = [simulate_routing(task, {"model": "opus"})]
        canary = [simulate_routing(task, {"model": "sonnet"})]
        diffs = compare_decisions(primary, canary)
        assert diffs[0].matches is False
        assert diffs[0].primary_model == "opus"
        assert diffs[0].canary_model == "sonnet"

    def test_multiple_tasks(self) -> None:
        """Compare works across multiple tasks."""
        tasks = [_task(task_id=f"t-{i}") for i in range(3)]
        primary = [simulate_routing(t, {}) for t in tasks]
        canary = [simulate_routing(t, {"adapter": "codex"}) for t in tasks]
        diffs = compare_decisions(primary, canary)
        assert len(diffs) == 3
        # adapter differs, model is the same
        for d in diffs:
            assert d.matches is False

    def test_empty_lists(self) -> None:
        """Empty input produces empty output."""
        diffs = compare_decisions([], [])
        assert diffs == []

    def test_unequal_lengths(self) -> None:
        """When lists differ in length, comparison stops at the shorter."""
        task = _task()
        primary = [simulate_routing(task, {}), simulate_routing(task, {})]
        canary = [simulate_routing(task, {})]
        diffs = compare_decisions(primary, canary)
        assert len(diffs) == 1

    def test_diff_is_frozen(self) -> None:
        """CanaryDiff is immutable."""
        task = _task()
        diffs = compare_decisions(
            [simulate_routing(task, {})],
            [simulate_routing(task, {})],
        )
        try:
            diffs[0].matches = False  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised


# ---------------------------------------------------------------------------
# build_canary_report
# ---------------------------------------------------------------------------


class TestBuildCanaryReport:
    """Tests for build_canary_report."""

    def test_all_matching(self) -> None:
        """100% match rate when configs are identical."""
        tasks = [_task(task_id=f"t-{i}") for i in range(5)]
        cfg: dict[str, Any] = {}
        primary = [simulate_routing(t, cfg) for t in tasks]
        canary = [simulate_routing(t, cfg) for t in tasks]
        report = build_canary_report(primary, canary)
        assert report.total_tasks == 5
        assert report.match_rate == 1.0
        assert all(d.matches for d in report.diffs)

    def test_none_matching(self) -> None:
        """0% match rate when every decision differs."""
        tasks = [_task(task_id=f"t-{i}") for i in range(4)]
        primary = [simulate_routing(t, {"model": "opus"}) for t in tasks]
        canary = [simulate_routing(t, {"model": "haiku"}) for t in tasks]
        report = build_canary_report(primary, canary)
        assert report.total_tasks == 4
        assert report.match_rate == 0.0

    def test_partial_matching(self) -> None:
        """Match rate is correctly calculated for partial matches."""
        t1 = _task(task_id="t-1")
        t2 = _task(task_id="t-2")
        # Same config for t1, different for t2
        primary = [
            simulate_routing(t1, {}),
            simulate_routing(t2, {"model": "opus"}),
        ]
        canary = [
            simulate_routing(t1, {}),
            simulate_routing(t2, {"model": "haiku"}),
        ]
        report = build_canary_report(primary, canary)
        assert report.total_tasks == 2
        assert report.match_rate == 0.5

    def test_empty_tasks(self) -> None:
        """Empty task lists produce 1.0 match rate (vacuously true)."""
        report = build_canary_report([], [])
        assert report.total_tasks == 0
        assert report.match_rate == 1.0

    def test_generated_at_is_iso(self) -> None:
        """generated_at is a valid ISO-8601 timestamp."""
        from datetime import datetime

        report = build_canary_report([], [])
        # Should not raise
        dt = datetime.fromisoformat(report.generated_at)
        assert dt.tzinfo is not None  # Should be timezone-aware

    def test_decisions_are_canary_config(self) -> None:
        """Report.decisions contains the canary decisions, not primary."""
        t = _task()
        primary = [simulate_routing(t, {"adapter": "claude"})]
        canary = [simulate_routing(t, {"adapter": "codex"})]
        report = build_canary_report(primary, canary)
        assert report.decisions[0].adapter == "codex"

    def test_report_is_frozen(self) -> None:
        """CanaryReport is immutable."""
        report = build_canary_report([], [])
        try:
            report.total_tasks = 99  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised


# ---------------------------------------------------------------------------
# format_canary_report
# ---------------------------------------------------------------------------


class TestFormatCanaryReport:
    """Tests for format_canary_report."""

    def test_empty_report(self) -> None:
        """Empty report mentions 'No tasks to compare'."""
        report = build_canary_report([], [])
        output = format_canary_report(report)
        assert "No tasks to compare" in output

    def test_header_present(self) -> None:
        """Output includes the report header."""
        report = build_canary_report([], [])
        output = format_canary_report(report)
        assert "Canary Report" in output

    def test_match_rate_in_output(self) -> None:
        """Match rate percentage appears in output."""
        tasks = [_task(task_id=f"t-{i}") for i in range(2)]
        cfg: dict[str, Any] = {}
        primary = [simulate_routing(t, cfg) for t in tasks]
        canary = [simulate_routing(t, cfg) for t in tasks]
        report = build_canary_report(primary, canary)
        output = format_canary_report(report)
        assert "100.0%" in output

    def test_mismatches_section(self) -> None:
        """Mismatches section shows differing task IDs."""
        t = _task(task_id="diff-task")
        primary = [simulate_routing(t, {"model": "opus"})]
        canary = [simulate_routing(t, {"model": "haiku"})]
        report = build_canary_report(primary, canary)
        output = format_canary_report(report)
        assert "Mismatches" in output
        assert "diff-task" in output

    def test_matches_section(self) -> None:
        """Matches section shows identical task IDs."""
        t = _task(task_id="same-task")
        cfg: dict[str, Any] = {}
        primary = [simulate_routing(t, cfg)]
        canary = [simulate_routing(t, cfg)]
        report = build_canary_report(primary, canary)
        output = format_canary_report(report)
        assert "Matches" in output
        assert "same-task" in output

    def test_arrow_in_mismatch(self) -> None:
        """Mismatch lines use '->' to show old->new routing."""
        t = _task(task_id="arrow-test")
        primary = [simulate_routing(t, {"adapter": "claude", "model": "opus"})]
        canary = [simulate_routing(t, {"adapter": "codex", "model": "haiku"})]
        report = build_canary_report(primary, canary)
        output = format_canary_report(report)
        assert "claude/opus" in output
        assert "codex/haiku" in output
        assert "->" in output
