"""Unit tests for :class:`PlanLifecycle` state transitions."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bernstein.core.planning.lifecycle import (
    PlanArchiveError,
    PlanLifecycle,
    PlanState,
    default_lifecycle,
)
from bernstein.core.planning.run_summary import (
    FailureSummary,
    GateResult,
    ModelCost,
    RunSummary,
    TaskCounts,
)


def _write_active_plan(lifecycle: PlanLifecycle, name: str = "demo.yaml") -> Path:
    body = yaml.dump(
        {
            "name": "demo",
            "stages": [
                {"name": "s1", "steps": [{"goal": "do something", "role": "backend"}]},
            ],
        }
    )
    plan = lifecycle.bucket(PlanState.ACTIVE) / name
    plan.write_text(body)
    return plan


def _basic_summary() -> RunSummary:
    return RunSummary(
        pr_url="https://github.com/example/repo/pull/42",
        gate_results=[GateResult("tests", True, "1023 passed")],
        model_costs=[ModelCost("gpt-4o", 0.42)],
        wall_clock_seconds=185.0,
        agent_time_seconds=120.0,
        tasks=TaskCounts(completed=4, failed=0, skipped=1),
    )


def _basic_failure() -> FailureSummary:
    return FailureSummary(
        failing_stage="build",
        task_ids=["plan-0-1", "plan-0-2"],
        last_error="ImportError: no module 'foo'",
    )


# ---------------------------------------------------------------------------
# Bucketing & transitions
# ---------------------------------------------------------------------------


def test_lifecycle_creates_buckets(tmp_path: Path) -> None:
    lifecycle = PlanLifecycle(tmp_path / "plans")
    for state in PlanState:
        assert lifecycle.bucket(state).is_dir(), f"missing bucket {state.value}"


def test_archive_completed_moves_to_completed(tmp_path: Path) -> None:
    lifecycle = PlanLifecycle(tmp_path / "plans")
    plan_path = _write_active_plan(lifecycle)
    archived = lifecycle.archive_completed(plan_path, _basic_summary())

    assert archived.parent == lifecycle.bucket(PlanState.COMPLETED)
    assert archived.name.startswith("20")  # YYYY-...
    assert archived.suffix == ".yaml"
    assert not plan_path.exists()
    text = archived.read_text()
    assert "## Run summary" in text
    assert "https://github.com/example/repo/pull/42" in text
    # YAML body still present after the prelude - strip the comment block then parse.
    body_start = text.index("-->") + len("-->")
    parsed = yaml.safe_load(text[body_start:])
    assert parsed["name"] == "demo"


def test_archive_blocked_moves_to_blocked(tmp_path: Path) -> None:
    lifecycle = PlanLifecycle(tmp_path / "plans")
    plan_path = _write_active_plan(lifecycle, "abort.yaml")
    archived = lifecycle.archive_blocked(plan_path, _basic_failure())

    assert archived.parent == lifecycle.bucket(PlanState.BLOCKED)
    text = archived.read_text()
    assert "## Failure reason" in text
    assert "build" in text
    assert "plan-0-1" in text


def test_archive_refuses_outside_active_dir(tmp_path: Path) -> None:
    lifecycle = PlanLifecycle(tmp_path / "plans")
    rogue = tmp_path / "rogue.yaml"
    rogue.write_text("name: rogue\nstages: []\n")
    with pytest.raises(PlanArchiveError):
        lifecycle.archive_completed(rogue, _basic_summary())


def test_archive_missing_source_raises(tmp_path: Path) -> None:
    lifecycle = PlanLifecycle(tmp_path / "plans")
    missing = lifecycle.bucket(PlanState.ACTIVE) / "nope.yaml"
    with pytest.raises(PlanArchiveError):
        lifecycle.archive_completed(missing, _basic_summary())


def test_assert_writable_refuses_archived_paths(tmp_path: Path) -> None:
    lifecycle = PlanLifecycle(tmp_path / "plans")
    plan_path = _write_active_plan(lifecycle)
    archived = lifecycle.archive_completed(plan_path, _basic_summary())

    with pytest.raises(PlanArchiveError):
        lifecycle.assert_writable(archived)
    # Active path is fine even if absent.
    lifecycle.assert_writable(lifecycle.bucket(PlanState.ACTIVE) / "any.yaml")


def test_archived_files_are_read_only(tmp_path: Path) -> None:
    lifecycle = PlanLifecycle(tmp_path / "plans")
    plan_path = _write_active_plan(lifecycle)
    archived = lifecycle.archive_completed(plan_path, _basic_summary())

    mode = archived.stat().st_mode & 0o777
    assert mode == 0o444, f"archived file should be 0o444, got {mode:o}"


def test_default_lifecycle_helper(tmp_path: Path) -> None:
    lifecycle = default_lifecycle(tmp_path)
    assert lifecycle.root == tmp_path / "plans"


def test_list_plans_returns_all_buckets(tmp_path: Path) -> None:
    lifecycle = PlanLifecycle(tmp_path / "plans")
    p1 = _write_active_plan(lifecycle, "alpha.yaml")
    p2 = _write_active_plan(lifecycle, "beta.yaml")
    lifecycle.archive_completed(p1, _basic_summary())
    lifecycle.archive_blocked(p2, _basic_failure())

    all_plans = lifecycle.list_plans()
    states = {plan.state for plan in all_plans}
    assert states == {PlanState.COMPLETED, PlanState.BLOCKED}

    completed = lifecycle.list_plans(PlanState.COMPLETED)
    assert len(completed) == 1
    assert completed[0].state == PlanState.COMPLETED


def test_find_by_id_returns_archived_plan(tmp_path: Path) -> None:
    lifecycle = PlanLifecycle(tmp_path / "plans")
    plan_path = _write_active_plan(lifecycle, "needle.yaml")
    archived = lifecycle.archive_completed(plan_path, _basic_summary())

    found = lifecycle.find(archived.stem)
    assert found is not None
    assert found.path == archived
    assert found.state == PlanState.COMPLETED

    assert lifecycle.find("does-not-exist") is None
