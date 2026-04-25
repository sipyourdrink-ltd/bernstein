"""End-to-end integration: a plan run terminates, archived YAML lands
in ``plans/completed/`` with all four summary subsections populated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from bernstein.core.planning.lifecycle import PlanLifecycle, PlanState
from bernstein.core.planning.plan_loader import load_plan
from bernstein.core.planning.run_summary import (
    FailureSummary,
    GateResult,
    ModelCost,
    RunSummary,
    TaskCounts,
)

_PLAN_YAML: dict[str, Any] = {
    "name": "strategic-300",
    "description": "Integration target plan",
    "stages": [
        {
            "name": "design",
            "steps": [
                {"goal": "draft architecture", "role": "architect"},
                {"goal": "review", "role": "reviewer"},
            ],
        },
        {
            "name": "build",
            "depends_on": ["design"],
            "steps": [
                {"goal": "ship feature", "role": "backend"},
            ],
        },
    ],
}


def _seed_active_plan(plans_root: Path, name: str = "strategic-300") -> Path:
    active = plans_root / "active"
    active.mkdir(parents=True, exist_ok=True)
    plan = active / f"{name}.yaml"
    plan.write_text(yaml.dump(_PLAN_YAML))
    return plan


def test_e2e_archive_run_summary_populated(tmp_path: Path) -> None:
    plans_root = tmp_path / "plans"
    plan = _seed_active_plan(plans_root)
    lifecycle = PlanLifecycle(plans_root)

    summary = RunSummary(
        pr_url="https://github.com/example/bernstein/pull/300",
        gate_results=[
            GateResult("tests", True, "all passed"),
            GateResult("ruff", True),
            GateResult("pyright", False, "1 error in plan_loader.py"),
        ],
        model_costs=[
            ModelCost("claude-opus-4-7", 2.34),
            ModelCost("gpt-4o-mini", 0.18),
        ],
        wall_clock_seconds=4500.0,
        agent_time_seconds=2700.0,
        tasks=TaskCounts(completed=3, failed=0, skipped=0),
    )

    archived = lifecycle.archive_completed(plan, summary)

    # 1. Lives under plans/completed/.
    assert archived.parent == plans_root / "completed"
    assert archived.exists()

    # 2. Source plan is removed; bucket listing shows it.
    assert not plan.exists()
    completed = lifecycle.list_plans(PlanState.COMPLETED)
    assert any(a.path == archived for a in completed)

    # 3. File is read-only on disk.
    mode = archived.stat().st_mode & 0o777
    assert mode == 0o444

    # 4. All four summary subsections are present and populated.
    body = archived.read_text()
    assert "## Run summary" in body
    for subsection in ("### Gate results", "### Cost breakdown", "### Duration", "### Tasks"):
        assert subsection in body, f"missing {subsection!r}"
    assert "https://github.com/example/bernstein/pull/300" in body
    assert "claude-opus-4-7" in body
    assert "Wall-clock:" in body
    assert "Agent-time:" in body
    assert "Completed: 3" in body

    # 5. The archived file remains a valid plan YAML - the loader can
    # parse the body once the leading comment block is stripped.  This
    # is the user's escape hatch for re-running an archived plan.
    body_start = body.index("-->") + len("-->")
    payload = body[body_start:]
    rerun_path = tmp_path / "active-rerun.yaml"
    rerun_path.write_text(payload)
    config, tasks = load_plan(rerun_path)
    assert config.name == "strategic-300"
    assert len(tasks) == 3


def test_e2e_archive_blocked_carries_failure_reason(tmp_path: Path) -> None:
    plans_root = tmp_path / "plans"
    plan = _seed_active_plan(plans_root, "abort-case")
    lifecycle = PlanLifecycle(plans_root)

    archived = lifecycle.archive_blocked(
        plan,
        FailureSummary(
            failing_stage="build",
            task_ids=["plan-1-0"],
            last_error="RuntimeError: build broke",
        ),
    )

    body = archived.read_text()
    assert "## Failure reason" in body
    assert "Failing stage: build" in body
    assert "plan-1-0" in body
    assert "RuntimeError: build broke" in body
