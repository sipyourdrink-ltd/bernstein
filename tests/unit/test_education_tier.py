"""Tests for education tier classroom orchestration (road-019)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from bernstein.core.education_tier import (
    ClassroomConfig,
    ClassroomSession,
    DecisionExplanation,
    ExerciseResult,
    StudentProfile,
    _LegacyClassroomConfig,
    enforce_student_limits,
    explain_agent_decision,
    format_exercise_report,
)

# ---------------------------------------------------------------------------
# StudentProfile
# ---------------------------------------------------------------------------


class TestStudentProfile:
    """Tests for the StudentProfile frozen dataclass."""

    def test_defaults(self) -> None:
        """StudentProfile has sensible defaults."""
        p = StudentProfile(student_id="s1", name="Alice", course_id="cs101")
        assert p.max_agents == 2
        assert p.max_cost_usd == pytest.approx(1.0)
        assert p.allowed_models == ("haiku", "flash")

    def test_frozen(self) -> None:
        """StudentProfile is immutable."""
        p = StudentProfile(student_id="s1", name="Alice", course_id="cs101")
        with pytest.raises(AttributeError):
            p.name = "Bob"  # type: ignore[misc]

    def test_custom_values(self) -> None:
        """StudentProfile accepts custom limits."""
        p = StudentProfile(
            student_id="s2",
            name="Bob",
            course_id="cs201",
            max_agents=4,
            max_cost_usd=5.0,
            allowed_models=("opus", "sonnet"),
        )
        assert p.max_agents == 4
        assert p.max_cost_usd == pytest.approx(5.0)
        assert p.allowed_models == ("opus", "sonnet")


# ---------------------------------------------------------------------------
# ClassroomConfig
# ---------------------------------------------------------------------------


class TestClassroomConfig:
    """Tests for the ClassroomConfig frozen dataclass."""

    def test_defaults(self) -> None:
        """ClassroomConfig has sensible defaults."""
        cfg = ClassroomConfig(course_id="cs101", instructor_id="prof1")
        assert cfg.max_students == 30
        assert cfg.shared_plan is None
        assert cfg.explanation_mode is True

    def test_frozen(self) -> None:
        """ClassroomConfig is immutable."""
        cfg = ClassroomConfig(course_id="cs101", instructor_id="prof1")
        with pytest.raises(AttributeError):
            cfg.course_id = "cs999"  # type: ignore[misc]

    def test_with_shared_plan(self) -> None:
        """ClassroomConfig accepts an optional shared plan."""
        cfg = ClassroomConfig(
            course_id="cs101",
            instructor_id="prof1",
            shared_plan="plans/lab1.yaml",
        )
        assert cfg.shared_plan == "plans/lab1.yaml"


# ---------------------------------------------------------------------------
# ExerciseResult
# ---------------------------------------------------------------------------


class TestExerciseResult:
    """Tests for the ExerciseResult frozen dataclass."""

    def test_creation(self) -> None:
        """ExerciseResult stores all fields."""
        r = ExerciseResult(
            student_id="s1",
            task_id="t1",
            success=True,
            cost_usd=0.05,
            agent_decisions=["chose haiku", "spawned 1 agent"],
            duration_s=12.5,
        )
        assert r.student_id == "s1"
        assert r.success is True
        assert r.agent_decisions == ["chose haiku", "spawned 1 agent"]

    def test_frozen(self) -> None:
        """ExerciseResult is immutable."""
        r = ExerciseResult(
            student_id="s1",
            task_id="t1",
            success=True,
            cost_usd=0.0,
            agent_decisions=[],
            duration_s=1.0,
        )
        with pytest.raises(AttributeError):
            r.success = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DecisionExplanation
# ---------------------------------------------------------------------------


class TestDecisionExplanation:
    """Tests for the DecisionExplanation frozen dataclass."""

    def test_creation(self) -> None:
        """DecisionExplanation stores all fields."""
        d = DecisionExplanation(
            decision="Selected claude/haiku",
            reasoning="Low complexity task.",
            alternatives=["claude/flash"],
        )
        assert d.decision == "Selected claude/haiku"
        assert "Low complexity" in d.reasoning
        assert d.alternatives == ["claude/flash"]

    def test_frozen(self) -> None:
        """DecisionExplanation is immutable."""
        d = DecisionExplanation(decision="d", reasoning="r", alternatives=[])
        with pytest.raises(AttributeError):
            d.decision = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# explain_agent_decision
# ---------------------------------------------------------------------------


class TestExplainAgentDecision:
    """Tests for the explain_agent_decision function."""

    def test_low_complexity(self) -> None:
        """Low complexity tasks mention cost-efficiency."""
        exp = explain_agent_decision("claude", "haiku", "backend", "low")
        assert "cost-efficient" in exp.reasoning
        assert "claude" in exp.decision
        assert "haiku" in exp.decision

    def test_high_complexity(self) -> None:
        """High complexity tasks mention capability depth."""
        exp = explain_agent_decision("codex", "opus", "architect", "high")
        assert "capable" in exp.reasoning or "reasoning depth" in exp.reasoning
        assert "codex" in exp.decision
        assert "opus" in exp.decision

    def test_medium_complexity(self) -> None:
        """Medium complexity tasks mention trade-off."""
        exp = explain_agent_decision("claude", "sonnet", "qa", "medium")
        assert "trade-off" in exp.reasoning
        assert "sonnet" in exp.decision

    def test_trivial_complexity(self) -> None:
        """Trivial tasks are treated as low complexity."""
        exp = explain_agent_decision("claude", "flash", "docs", "trivial")
        assert "cost-efficient" in exp.reasoning

    def test_critical_complexity(self) -> None:
        """Critical tasks are treated as high complexity."""
        exp = explain_agent_decision("claude", "opus", "security", "critical")
        assert "capable" in exp.reasoning or "reasoning depth" in exp.reasoning

    def test_unknown_complexity_defaults_to_medium(self) -> None:
        """Unknown complexity labels fall back to medium-tier reasoning."""
        exp = explain_agent_decision("claude", "sonnet", "backend", "unknown")
        assert "trade-off" in exp.reasoning

    def test_alternatives_exclude_selected(self) -> None:
        """Selected model is not listed as an alternative."""
        exp = explain_agent_decision("claude", "haiku", "backend", "low")
        assert "claude/haiku" not in exp.alternatives

    def test_decision_format(self) -> None:
        """Decision string includes adapter, model, role, and complexity."""
        exp = explain_agent_decision("gemini", "flash", "frontend", "medium")
        assert "gemini/flash" in exp.decision
        assert "frontend" in exp.decision
        assert "medium" in exp.decision

    def test_return_type(self) -> None:
        """Returns a DecisionExplanation instance."""
        exp = explain_agent_decision("claude", "haiku", "qa", "low")
        assert isinstance(exp, DecisionExplanation)


# ---------------------------------------------------------------------------
# enforce_student_limits
# ---------------------------------------------------------------------------


class TestEnforceStudentLimits:
    """Tests for the enforce_student_limits function."""

    def test_no_violations(self) -> None:
        """No violations when within all limits."""
        profile = StudentProfile(student_id="s1", name="A", course_id="c1")
        violations = enforce_student_limits(profile, current_cost=0.5, active_agents=1)
        assert violations == []

    def test_cost_exceeded(self) -> None:
        """Violation when cost meets or exceeds limit."""
        profile = StudentProfile(student_id="s1", name="A", course_id="c1", max_cost_usd=1.0)
        violations = enforce_student_limits(profile, current_cost=1.0, active_agents=0)
        assert len(violations) == 1
        assert "Cost limit" in violations[0]

    def test_cost_over_limit(self) -> None:
        """Violation when cost is over limit."""
        profile = StudentProfile(student_id="s1", name="A", course_id="c1", max_cost_usd=1.0)
        violations = enforce_student_limits(profile, current_cost=1.5, active_agents=0)
        assert len(violations) == 1
        assert "Cost limit" in violations[0]

    def test_agent_limit_reached(self) -> None:
        """Violation when active agents meet or exceed max."""
        profile = StudentProfile(student_id="s1", name="A", course_id="c1", max_agents=2)
        violations = enforce_student_limits(profile, current_cost=0.0, active_agents=2)
        assert len(violations) == 1
        assert "Agent limit" in violations[0]

    def test_both_violations(self) -> None:
        """Both cost and agent violations returned simultaneously."""
        profile = StudentProfile(
            student_id="s1",
            name="A",
            course_id="c1",
            max_agents=2,
            max_cost_usd=1.0,
        )
        violations = enforce_student_limits(profile, current_cost=2.0, active_agents=3)
        assert len(violations) == 2

    def test_within_limits_zero(self) -> None:
        """Zero cost and zero agents produce no violations."""
        profile = StudentProfile(student_id="s1", name="A", course_id="c1")
        violations = enforce_student_limits(profile, current_cost=0.0, active_agents=0)
        assert violations == []


# ---------------------------------------------------------------------------
# format_exercise_report
# ---------------------------------------------------------------------------


class TestFormatExerciseReport:
    """Tests for the format_exercise_report function."""

    def test_empty_results(self) -> None:
        """Empty results produce a placeholder message."""
        report = format_exercise_report([])
        assert "No exercise results" in report

    def test_single_result(self) -> None:
        """Single result appears in both summary and detail sections."""
        results = [
            ExerciseResult(
                student_id="alice",
                task_id="t1",
                success=True,
                cost_usd=0.05,
                agent_decisions=["chose haiku"],
                duration_s=10.0,
            ),
        ]
        report = format_exercise_report(results)
        assert "Exercise Report" in report
        assert "alice" in report
        assert "1/1 passed" in report
        assert "PASS" in report
        assert "$0.0500" in report

    def test_multiple_students(self) -> None:
        """Multiple students each get a summary line."""
        results = [
            ExerciseResult("alice", "t1", True, 0.10, [], 5.0),
            ExerciseResult("bob", "t2", False, 0.20, [], 8.0),
            ExerciseResult("alice", "t3", True, 0.15, [], 7.0),
        ]
        report = format_exercise_report(results)
        assert "alice" in report
        assert "bob" in report
        # Alice: 2/2 passed
        assert "2/2 passed" in report
        # Bob: 0/1 passed
        assert "0/1 passed" in report

    def test_fail_status(self) -> None:
        """Failed tasks show FAIL in detail section."""
        results = [
            ExerciseResult("alice", "t1", False, 0.01, [], 2.0),
        ]
        report = format_exercise_report(results)
        assert "FAIL" in report

    def test_decisions_count(self) -> None:
        """Report includes decision count when decisions are present."""
        results = [
            ExerciseResult("alice", "t1", True, 0.01, ["d1", "d2", "d3"], 1.0),
        ]
        report = format_exercise_report(results)
        assert "3 decisions" in report

    def test_no_decisions(self) -> None:
        """No decision count when agent_decisions is empty."""
        results = [
            ExerciseResult("alice", "t1", True, 0.01, [], 1.0),
        ]
        report = format_exercise_report(results)
        assert "decisions" not in report

    def test_students_sorted(self) -> None:
        """Student summary is in alphabetical order."""
        results = [
            ExerciseResult("charlie", "t1", True, 0.01, [], 1.0),
            ExerciseResult("alice", "t2", True, 0.01, [], 1.0),
            ExerciseResult("bob", "t3", True, 0.01, [], 1.0),
        ]
        report = format_exercise_report(results)
        # Alice should appear before Bob, Bob before Charlie in summary.
        alice_pos = report.index("alice")
        bob_pos = report.index("bob")
        charlie_pos = report.index("charlie")
        assert alice_pos < bob_pos < charlie_pos


# ---------------------------------------------------------------------------
# Legacy ClassroomSession (backward compat)
# ---------------------------------------------------------------------------


def _make_config(students: list[str] | None = None) -> _LegacyClassroomConfig:
    """Create a standard test classroom config."""
    return _LegacyClassroomConfig(
        instructor="prof_smith",
        students=students or ["alice", "bob", "charlie"],
        max_cost_per_student=1.0,
        allowed_models=["haiku", "flash"],
        sandbox_mode=True,
    )


def test_budget_remaining_initial(tmp_path: Path) -> None:
    """All students start with full budget."""
    session = ClassroomSession(_make_config(), tmp_path)
    assert session.student_budget_remaining("alice") == pytest.approx(1.0)
    assert session.student_budget_remaining("bob") == pytest.approx(1.0)


def test_budget_remaining_unknown_student(tmp_path: Path) -> None:
    """Querying unknown student raises KeyError."""
    session = ClassroomSession(_make_config(), tmp_path)
    with pytest.raises(KeyError, match="not enrolled"):
        session.student_budget_remaining("unknown")


def test_approve_task_success(tmp_path: Path) -> None:
    """approve_task deducts cost and returns True within budget."""
    session = ClassroomSession(_make_config(), tmp_path)
    assert session.approve_task("alice", 0.30) is True
    assert abs(session.student_budget_remaining("alice") - 0.70) < 1e-9


def test_approve_task_over_budget(tmp_path: Path) -> None:
    """approve_task returns False when cost exceeds remaining budget."""
    session = ClassroomSession(_make_config(), tmp_path)
    session.approve_task("alice", 0.80)
    assert session.approve_task("alice", 0.30) is False
    # Budget should not have been deducted for the denied task
    assert abs(session.student_budget_remaining("alice") - 0.20) < 1e-9


def test_approve_task_unknown_student(tmp_path: Path) -> None:
    """approve_task raises KeyError for unknown student."""
    session = ClassroomSession(_make_config(), tmp_path)
    with pytest.raises(KeyError):
        session.approve_task("ghost", 0.10)


def test_student_isolation(tmp_path: Path) -> None:
    """One student's spending does not affect another's budget."""
    session = ClassroomSession(_make_config(), tmp_path)
    session.approve_task("alice", 0.50)
    assert session.student_budget_remaining("bob") == pytest.approx(1.0)


def test_student_summary(tmp_path: Path) -> None:
    """student_summary returns stats for all students."""
    session = ClassroomSession(_make_config(), tmp_path)
    session.approve_task("alice", 0.25)
    session.approve_task("alice", 0.10)
    session.approve_task("bob", 0.50)

    summary = session.student_summary()
    assert len(summary) == 3
    alice = next(s for s in summary if s["student"] == "alice")
    assert alice["tasks_submitted"] == 2
    assert alice["cost_used"] == pytest.approx(0.35)
    assert alice["budget_remaining"] == pytest.approx(0.65)


def test_student_summary_sorted(tmp_path: Path) -> None:
    """student_summary returns students in alphabetical order."""
    session = ClassroomSession(_make_config(), tmp_path)
    summary = session.student_summary()
    names = [s["student"] for s in summary]
    assert names == sorted(names)


def test_export_grades(tmp_path: Path) -> None:
    """export_grades writes a valid CSV file."""
    session = ClassroomSession(_make_config(), tmp_path)
    session.approve_task("alice", 0.25)
    session.approve_task("bob", 0.10)

    out = tmp_path / "grades.csv"
    result = session.export_grades(out)
    assert result == out
    assert out.exists()

    with out.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    assert len(rows) == 3
    assert rows[0]["student"] == "alice"
    assert rows[0]["tasks_completed"] == "1"
    assert rows[0]["cost_used"] == "0.25"


def test_export_grades_creates_directory(tmp_path: Path) -> None:
    """export_grades creates parent directories if needed."""
    session = ClassroomSession(_make_config(), tmp_path)
    out = tmp_path / "nested" / "dir" / "grades.csv"
    session.export_grades(out)
    assert out.exists()


def test_legacy_config_defaults() -> None:
    """_LegacyClassroomConfig has sensible defaults."""
    cfg = _LegacyClassroomConfig(instructor="prof", students=["s1"])
    assert cfg.max_cost_per_student == pytest.approx(1.0)
    assert cfg.allowed_models == ["haiku", "flash"]
    assert cfg.sandbox_mode is True
