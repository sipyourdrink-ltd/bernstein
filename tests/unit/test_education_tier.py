"""Tests for education tier classroom orchestration (road-019)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from bernstein.core.education_tier import ClassroomConfig, ClassroomSession


def _make_config(students: list[str] | None = None) -> ClassroomConfig:
    """Create a standard test classroom config."""
    return ClassroomConfig(
        instructor="prof_smith",
        students=students or ["alice", "bob", "charlie"],
        max_cost_per_student=1.0,
        allowed_models=["haiku", "flash"],
        sandbox_mode=True,
    )


def test_budget_remaining_initial(tmp_path: Path) -> None:
    """All students start with full budget."""
    session = ClassroomSession(_make_config(), tmp_path)
    assert session.student_budget_remaining("alice") == 1.0
    assert session.student_budget_remaining("bob") == 1.0


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
    assert session.student_budget_remaining("bob") == 1.0


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
    assert alice["cost_used"] == 0.35
    assert alice["budget_remaining"] == 0.65


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


def test_config_defaults() -> None:
    """ClassroomConfig has sensible defaults."""
    cfg = ClassroomConfig(instructor="prof", students=["s1"])
    assert cfg.max_cost_per_student == 1.0
    assert cfg.allowed_models == ["haiku", "flash"]
    assert cfg.sandbox_mode is True
