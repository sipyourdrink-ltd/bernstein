"""Regression tests for _create_upgrade_tasks' AutoSpawnGuard wiring.

Covers the production incident (work/agent-reports/2026-07-02-run9-attempt9-audit.md)
where the same "Upgrade: Improve task success rate" opportunity was
re-proposed every analysis cycle with no dedupe or cap, producing ~19
duplicate meta-tasks with zero forward progress.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.models import Task, TaskStatus, TaskType

from bernstein.core.orchestration.evolution import UpgradeStatus
from bernstein.core.orchestration.orchestrator_evolve import _create_upgrade_tasks
from bernstein.core.tasks.auto_spawn_guard import AutoSpawnGuard


def _proposal(
    proposal_id: str,
    title: str,
    *,
    status: UpgradeStatus = UpgradeStatus.PENDING,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=proposal_id,
        title=title,
        description="desc",
        status=status,
        proposed_change="x" * 100,
        risk_assessment=SimpleNamespace(affected_components=["model_routing"]),
    )


def _orch(workdir: Path, *, latest_tasks: dict[str, Task] | None = None) -> SimpleNamespace:
    client = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    client.post.return_value = resp

    risk_scorer = SimpleNamespace(
        score_proposal=lambda **kwargs: SimpleNamespace(composite_risk=0.2),
        is_high_risk=lambda score: False,
    )

    return SimpleNamespace(
        _workdir=workdir,
        _config=SimpleNamespace(server_url="http://server"),
        _client=client,
        _risk_scorer=risk_scorer,
        _latest_tasks_by_id=latest_tasks or {},
    )


def test_allowed_upgrade_task_is_created_and_posted(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    result = SimpleNamespace(errors=[])

    _create_upgrade_tasks(orch, [_proposal("p1", "Improve task success rate")], result)

    assert orch._client.post.call_count == 1
    _, kwargs = orch._client.post.call_args
    assert kwargs["json"]["title"] == "Upgrade: Improve task success rate"
    assert result.errors == []


def test_dedupe_refuses_upgrade_task_matching_existing_open_task(tmp_path: Path) -> None:
    existing = Task(
        id="t1",
        title="Upgrade: Improve task success rate",
        description="desc",
        role="backend",
        task_type=TaskType.UPGRADE_PROPOSAL,
        status=TaskStatus.OPEN,
    )
    orch = _orch(tmp_path, latest_tasks={existing.id: existing})
    result = SimpleNamespace(errors=[])

    _create_upgrade_tasks(orch, [_proposal("p1", "Improve task success rate")], result)

    assert orch._client.post.call_count == 0


def test_cap_refuses_upgrade_tasks_beyond_configured_limit(tmp_path: Path) -> None:
    # Pre-seed the shared guard state at the cap (default 3) so the very next
    # evaluation in this run is refused.
    state_path = tmp_path / ".sdd" / "runtime" / "auto_spawn_guard.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"count": 3}), encoding="utf-8")

    orch = _orch(tmp_path)
    result = SimpleNamespace(errors=[])

    _create_upgrade_tasks(orch, [_proposal("p1", "Improve task success rate")], result)

    assert orch._client.post.call_count == 0


def test_non_eligible_proposal_status_is_skipped_before_guard(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    result = SimpleNamespace(errors=[])

    _create_upgrade_tasks(orch, [_proposal("p1", "Applied already", status=UpgradeStatus.APPLIED)], result)

    assert orch._client.post.call_count == 0


def test_oserror_from_guard_save_skips_only_that_proposal(tmp_path: Path) -> None:
    """AutoSpawnGuard._save_count() can raise OSError on a transient
    .sdd/runtime write failure. Before this fix, the per-proposal try/except
    only caught httpx.HTTPError, so an OSError on the first proposal's
    guard.evaluate() escaped and aborted the ENTIRE batch -- the second,
    unrelated proposal never got a chance to post. This asserts the OSError
    is caught, recorded, and the batch continues to the next proposal."""
    orch = _orch(tmp_path)
    result = SimpleNamespace(errors=[])

    with patch.object(AutoSpawnGuard, "_save_count", side_effect=OSError("disk full")):
        _create_upgrade_tasks(
            orch,
            [
                _proposal("p1", "Improve task success rate"),
                _proposal("p2", "Reduce flaky retries"),
            ],
            result,
        )

    # Both proposals hit the same failing guard.evaluate() -> both are
    # skipped, but neither raises out of _create_upgrade_tasks, and both
    # failures are recorded rather than silently swallowed or crashing the
    # batch.
    assert orch._client.post.call_count == 0
    assert len(result.errors) == 2
    assert all("disk full" in e for e in result.errors)
