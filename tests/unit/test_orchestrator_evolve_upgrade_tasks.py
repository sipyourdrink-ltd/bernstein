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

import pytest
from bernstein.core.models import Task, TaskStatus, TaskType

from bernstein.core.orchestration.evolution import UpgradeStatus
from bernstein.core.orchestration.orchestrator_evolve import _create_upgrade_tasks
from bernstein.core.tasks.auto_spawn_guard import AutoSpawnGuard
from bernstein.evolution.detector import UpgradeCategory
from bernstein.evolution.upgrade_targets import UPGRADE_CATEGORY_TARGETS, upgrade_owned_files


def _proposal(
    proposal_id: str,
    title: str,
    *,
    status: UpgradeStatus = UpgradeStatus.PENDING,
    category: UpgradeCategory = UpgradeCategory.MODEL_ROUTING,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=proposal_id,
        title=title,
        description="desc",
        status=status,
        category=category,
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


@pytest.mark.parametrize("category", list(UpgradeCategory))
def test_upgrade_task_cannot_silently_claim_zero_files(tmp_path: Path, category: UpgradeCategory) -> None:
    """Every eligible category posts a non-empty, path-shaped owned_files.

    Empty owned_files makes _check_file_ownership_overlap, the batch claim
    path, and the circuit-breaker scope check all short-circuit (issue #3398),
    so an upgrade task must declare its category's applicator targets.
    """
    orch = _orch(tmp_path)
    result = SimpleNamespace(errors=[])

    _create_upgrade_tasks(orch, [_proposal("p1", "Improve things", category=category)], result)

    assert orch._client.post.call_count == 1
    _, kwargs = orch._client.post.call_args
    owned = kwargs["json"]["owned_files"]
    assert owned == upgrade_owned_files(category)
    assert owned, f"category {category} posted empty owned_files"
    assert all("/" in entry for entry in owned)
    assert result.errors == []


def test_unmapped_category_is_recorded_not_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A category with no target mapping spawns with a recorded reason.

    Refusing outright was tried in #3397 and fires unconditionally when the
    derivation source is empty by construction; the acceptance criterion is
    a non-empty declaration OR a recorded why-not, never a silent scopeless
    task and never a dead spawn path.
    """
    monkeypatch.delitem(UPGRADE_CATEGORY_TARGETS, UpgradeCategory.MODEL_ROUTING)
    orch = _orch(tmp_path)
    result = SimpleNamespace(errors=[])

    _create_upgrade_tasks(
        orch,
        [_proposal("p1", "Improve things", category=UpgradeCategory.MODEL_ROUTING)],
        result,
    )

    assert orch._client.post.call_count == 1
    _, kwargs = orch._client.post.call_args
    assert kwargs["json"]["owned_files"] == []
    assert len(result.errors) == 1
    assert "p1" in result.errors[0]
    assert "owned_files" in result.errors[0]


def test_component_labels_never_enter_the_posted_owned_files(tmp_path: Path) -> None:
    """Risk-assessment labels ("model_routing") are not paths; posting them as
    owned_files would defeat the guards' empty-means-no-scope short-circuit
    while matching no real file."""
    orch = _orch(tmp_path)
    result = SimpleNamespace(errors=[])

    _create_upgrade_tasks(orch, [_proposal("p1", "Improve things")], result)

    _, kwargs = orch._client.post.call_args
    assert "model_routing" not in kwargs["json"]["owned_files"]
