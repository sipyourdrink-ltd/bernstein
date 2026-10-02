"""A rollback after a partial apply trips the circuit breaker (#6317).

#5913 fed the breaker only when `was_applied` was true, but nothing wrote an
`applied` history row, so a failed apply that had already mutated a file and was
then reverted left the breaker CLOSED. The signal is now the backup manifest
`_backup_file` writes before it mutates anything.

No shipped category has a sink that calls `_backup_file` yet, so the tests that
need a half-applied change use a sink written the way a real one would be
(real `_backup_file`, real `_atomic_write`, a failure partway) and run it
through the real `execute_upgrade` and `EvolutionLoop._apply_proposal`, instead
of patching the executor's methods.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from bernstein.core.models import RiskAssessment, RollbackPlan

from bernstein.evolution import EvolutionCoordinator
from bernstein.evolution.admission import AdmissionPolicy, ColdStartMode
from bernstein.evolution.applicator import FileUpgradeExecutor
from bernstein.evolution.circuit import CircuitState
from bernstein.evolution.detector import UpgradeCategory
from bernstein.evolution.loop import EvolutionLoop
from bernstein.evolution.proposals import UpgradeProposal, UpgradeStatus
from bernstein.evolution.types import RollbackError

if TYPE_CHECKING:
    from pathlib import Path

ORIGINAL = "policies:\n  max_retries: 3\n"


def _proposal(proposal_id: str = "p-partial") -> UpgradeProposal:
    return UpgradeProposal(
        id=proposal_id,
        title="Raise max_retries",
        category=UpgradeCategory.POLICY_UPDATE,
        description="desc",
        current_state="current",
        proposed_change="change",
        benefits=["benefit"],
        risk_assessment=RiskAssessment(level="low"),
        rollback_plan=RollbackPlan(steps=["revert"], estimated_rollback_minutes=5),
        cost_estimate_usd=0.0,
        expected_improvement="improve",
        confidence=0.9,
    )


class _PartialSinkExecutor(FileUpgradeExecutor):
    """A sink shaped like a real one: back up, write one file, fail on the next."""

    def _apply_policy_update(self, proposal: UpgradeProposal) -> bool:
        self._backup_file("policies.yaml", proposal.id)
        self._atomic_write(self.config_dir / "policies.yaml", {"policies": {"max_retries": 99}})
        raise OSError("disk full")


class _FailsBeforeBackupExecutor(FileUpgradeExecutor):
    def _apply_policy_update(self, proposal: UpgradeProposal) -> bool:
        raise OSError("nothing written")


def _loop(tmp_path: Path, executor_cls: type[FileUpgradeExecutor] = _PartialSinkExecutor) -> EvolutionLoop:
    loop = EvolutionLoop(state_dir=tmp_path)
    # Cold start fails open so the proposal is admitted and reaches the sink.
    loop._executor = executor_cls(tmp_path, admission=AdmissionPolicy(cold_start=ColdStartMode.FAIL_OPEN))
    return loop


def _seed(loop: EvolutionLoop) -> Path:
    target = loop._executor.config_dir / "policies.yaml"
    target.write_text(ORIGINAL, encoding="utf-8")
    return target


def test_rollback_after_a_partial_apply_trips_the_breaker(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    target = _seed(loop)

    assert loop._apply_proposal(_proposal(), MagicMock()) is False
    assert target.read_text(encoding="utf-8") == ORIGINAL
    assert loop._breaker.state == CircuitState.OPEN
    assert len(loop._breaker.recent_rollbacks) == 1


def test_rollback_that_restored_nothing_does_not_trip_the_breaker(tmp_path: Path) -> None:
    loop = _loop(tmp_path, _FailsBeforeBackupExecutor)
    target = _seed(loop)

    assert loop._apply_proposal(_proposal("p-noop"), MagicMock()) is False
    assert target.read_text(encoding="utf-8") == ORIGINAL
    assert loop._breaker.state == CircuitState.CLOSED
    assert loop._breaker.recent_rollbacks == []


@pytest.mark.parametrize("category", list(UpgradeCategory))
def test_shipped_apply_path_changes_nothing_and_leaves_the_breaker_closed(
    tmp_path: Path, category: UpgradeCategory
) -> None:
    """The path that ships: no sink, so no manifest, no rollback, no halt.

    Unpatched `FileUpgradeExecutor`, through the real `_apply_proposal`. Pins
    what the code does today so the partial-apply handling above is not read
    as something production can reach.
    """
    loop = EvolutionLoop(state_dir=tmp_path)
    loop._executor = FileUpgradeExecutor(tmp_path, admission=AdmissionPolicy(cold_start=ColdStartMode.FAIL_OPEN))
    proposal = _proposal("p-shipped")
    proposal.category = category

    assert loop._apply_proposal(proposal, MagicMock()) is False
    assert not (tmp_path / "upgrades" / "backups").exists()
    assert loop._executor.was_applied(proposal.id) is False
    assert loop._breaker.state == CircuitState.CLOSED
    assert loop._breaker.recent_rollbacks == []


def test_a_reused_proposal_id_still_trips_the_breaker(tmp_path: Path) -> None:
    """Ids restart at UPG-0001 in every process while `.sdd/upgrades` persists."""
    first = _loop(tmp_path)
    target = _seed(first)
    assert first._apply_proposal(_proposal("UPG-0001"), MagicMock()) is False
    assert first._breaker.state == CircuitState.OPEN
    first._breaker.reset()

    second = _loop(tmp_path)
    assert second._apply_proposal(_proposal("UPG-0001"), MagicMock()) is False
    assert target.read_text(encoding="utf-8") == ORIGINAL
    assert second._breaker.state == CircuitState.OPEN
    assert len(second._breaker.recent_rollbacks) == 2, "the reused id's rollback is recorded as a new one"

    receipts = sorted(p.name for p in (tmp_path / "upgrades" / "rollbacks").iterdir())
    assert receipts == ["UPG-0001.2.json", "UPG-0001.json"], "the second rollback must not overwrite the first receipt"


def test_a_prior_rollback_does_not_mask_a_later_backup_under_the_same_id(tmp_path: Path) -> None:
    executor = FileUpgradeExecutor(tmp_path)
    target = executor.config_dir / "policies.yaml"
    target.write_text(ORIGINAL, encoding="utf-8")
    proposal = _proposal("UPG-0001")

    executor._backup_file("policies.yaml", proposal.id)
    target.write_text("half-written", encoding="utf-8")
    executor.rollback_upgrade(proposal)
    assert executor.was_applied(proposal.id) is False

    executor._backup_file("policies.yaml", proposal.id)
    target.write_text("half-written again", encoding="utf-8")
    assert executor.was_applied(proposal.id) is True
    executor.rollback_upgrade(proposal)
    assert target.read_text(encoding="utf-8") == ORIGINAL


@pytest.mark.parametrize("failing", ["_write_rollback_receipt", "_record_history"])
def test_a_failure_after_the_restore_still_trips_the_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """Receipt or history write fails (ENOSPC) once the files are already back."""
    loop = _loop(tmp_path)
    target = _seed(loop)

    original = getattr(loop._executor, failing)

    def fail_on_rollback(*args: Any, **kwargs: Any) -> Any:
        if failing == "_record_history" and args[1:] != ("rolled_back",):
            return original(*args, **kwargs)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(loop._executor, failing, fail_on_rollback)

    with pytest.raises(RollbackError, match="could not be recorded"):
        loop._apply_proposal(_proposal(), MagicMock())
    assert target.read_text(encoding="utf-8") == ORIGINAL
    assert loop._breaker.state == CircuitState.OPEN
    assert len(loop._breaker.recent_rollbacks) == 1


def test_a_rollback_that_cannot_restore_records_exactly_one_rollback(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    _seed(loop)
    executor = loop._executor
    original_backup = executor._backup_file

    def backup_then_lose_it(filename: str, proposal_id: str) -> None:
        original_backup(filename, proposal_id)
        (executor._backup_dir(proposal_id) / filename).unlink()

    executor._backup_file = backup_then_lose_it  # type: ignore[method-assign]

    with pytest.raises(RollbackError, match="is not there"):
        loop._apply_proposal(_proposal(), MagicMock())
    assert len(loop._breaker.recent_rollbacks) == 1


def test_repeat_rollback_keeps_the_receipt_of_the_one_that_restored(tmp_path: Path) -> None:
    executor = FileUpgradeExecutor(tmp_path)
    (executor.config_dir / "policies.yaml").write_text(ORIGINAL, encoding="utf-8")
    proposal = _proposal()
    executor._backup_file("policies.yaml", proposal.id)
    assert executor.rollback_upgrade(proposal) is True
    assert executor.rollback_upgrade(proposal) is True

    rollbacks = tmp_path / "upgrades" / "rollbacks"
    first = json.loads((rollbacks / f"{proposal.id}.json").read_text(encoding="utf-8"))
    second = json.loads((rollbacks / f"{proposal.id}.2.json").read_text(encoding="utf-8"))
    assert first["restored_files"] == ["policies.yaml"]
    assert second["note"] == "nothing was applied"


def test_manifest_entries_cannot_escape_the_state_directory(tmp_path: Path) -> None:
    state = tmp_path / "state"
    executor = FileUpgradeExecutor(state)
    proposal = _proposal("p-escape")
    backup_dir = executor._backup_dir(proposal.id)
    backup_dir.mkdir(parents=True)
    (backup_dir / "payload").write_text("owned", encoding="utf-8")
    manifest = {"../../escaped.yaml": f"backups/{proposal.id}/payload"}
    executor._backup_manifest_path(proposal.id).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RollbackError, match="outside"):
        executor.rollback_upgrade(proposal)
    assert not (tmp_path / "escaped.yaml").exists()

    manifest = {"policies.yaml": "../../../etc/hosts"}
    executor._backup_manifest_path(proposal.id).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RollbackError, match="outside"):
        executor.rollback_upgrade(proposal)


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "..", ""])
def test_a_proposal_id_that_is_not_one_path_segment_is_refused(tmp_path: Path, bad_id: str) -> None:
    executor = FileUpgradeExecutor(tmp_path / "state")
    with pytest.raises(RollbackError, match="path component"):
        executor.rollback_upgrade(_proposal(bad_id))
    assert not (tmp_path / "escape.json").exists()


class _RollbackRaises:
    """An executor that is only the two-method Protocol, and whose rollback fails."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def execute_upgrade(self, proposal: UpgradeProposal) -> bool:
        self.executed.append(proposal.id)
        return False

    def rollback_upgrade(self, proposal: UpgradeProposal) -> bool:
        raise RollbackError(f"{proposal.id} cannot be restored")


def test_coordinator_marks_a_failed_rollback_rejected_and_stops(tmp_path: Path) -> None:
    executor = _RollbackRaises()
    coordinator = EvolutionCoordinator(state_dir=tmp_path, executor=executor)
    first, second = _proposal("p1"), _proposal("p2")
    first.status = second.status = UpgradeStatus.APPROVED
    coordinator._pending_upgrades = [first, second]

    assert coordinator.execute_pending_upgrades() == []

    assert first.status == UpgradeStatus.REJECTED
    assert second.status == UpgradeStatus.APPROVED, "nothing is applied on top of an undeclared tree"
    assert executor.executed == ["p1"]
    assert coordinator._pending_upgrades == [second]


def test_was_applied_is_false_again_once_rolled_back(tmp_path: Path) -> None:
    executor = FileUpgradeExecutor(tmp_path)
    (executor.config_dir / "policies.yaml").write_text(ORIGINAL, encoding="utf-8")
    proposal = _proposal()
    executor._backup_file("policies.yaml", proposal.id)
    assert executor.was_applied(proposal.id) is True
    executor.rollback_upgrade(proposal)
    assert executor.was_applied(proposal.id) is False
