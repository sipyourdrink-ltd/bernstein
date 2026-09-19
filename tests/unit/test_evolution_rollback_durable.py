"""Rollback restores the named proposal, from disk, or says it could not (#5408).

Three defects in one method. `FileUpgradeExecutor.rollback_upgrade` restored
from `self._backup_files`, an in-memory dictionary populated during the same
process that applied the change; it ignored its own argument, so it could not
roll back a NAMED proposal; and it recorded nothing, so after the process
exited there was no answer to "was this rolled back, and what did it restore".

The sharpest consequence is not any of those. An empty backup map iterates
zero times and returns `True` -- so a rollback in a fresh process reported
success having restored nothing at all, which is a false green in the one path
that exists to undo a bad change.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import RiskAssessment, RollbackPlan

from bernstein.evolution.applicator import FileUpgradeExecutor
from bernstein.evolution.detector import UpgradeCategory
from bernstein.evolution.proposals import UpgradeProposal
from bernstein.evolution.types import RollbackError

if TYPE_CHECKING:
    from pathlib import Path

ORIGINAL = "policies:\n  max_retries: 3\n"
CHANGED = "policies:\n  max_retries: 99\n"


def _proposal(proposal_id: str = "prop-001") -> UpgradeProposal:
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


def _applied_change(state_dir: Path, proposal: UpgradeProposal, filename: str = "policies.yaml") -> None:
    """Put the tree in the state a real apply would leave: backed up, then changed.

    Driven through the executor's own backup path rather than by hand, so the
    test cannot pass against a manifest format the executor does not write.
    """
    executor = FileUpgradeExecutor(state_dir)
    (executor.config_dir / filename).write_text(ORIGINAL, encoding="utf-8")
    executor._backup_file(filename, proposal.id)
    (executor.config_dir / filename).write_text(CHANGED, encoding="utf-8")
    executor._record_history(proposal, "applied")


def test_rollback_restores_the_original_file_contents_exactly(tmp_path: Path) -> None:
    """Compared by CONTENT, not by return value.

    The old implementation's return value was `True` whether or not anything
    was restored, so a test asserting on it would have passed against the bug.
    """
    proposal = _proposal()
    _applied_change(tmp_path, proposal)
    target = tmp_path / "config" / "policies.yaml"
    assert target.read_text(encoding="utf-8") == CHANGED

    assert FileUpgradeExecutor(tmp_path).rollback_upgrade(proposal) is True
    assert target.read_text(encoding="utf-8") == ORIGINAL


def test_rollback_works_in_a_process_that_did_not_perform_the_apply(tmp_path: Path) -> None:
    """A FRESH executor, which is the whole point.

    Reusing the instance that applied the change would pass against the
    in-memory map and prove nothing about a restart.
    """
    proposal = _proposal()
    _applied_change(tmp_path, proposal)

    restarted = FileUpgradeExecutor(tmp_path)
    assert not hasattr(restarted, "_backup_files"), "a process-local map is what this must not depend on"
    assert restarted.rollback_upgrade(proposal) is True
    assert (tmp_path / "config" / "policies.yaml").read_text(encoding="utf-8") == ORIGINAL


def test_rollback_restores_the_proposal_it_is_given_and_not_another(tmp_path: Path) -> None:
    """The signature was `rollback_upgrade(self, _proposal)` and meant it."""
    first, second = _proposal("prop-001"), _proposal("prop-002")
    _applied_change(tmp_path, first, "policies.yaml")
    _applied_change(tmp_path, second, "routing.yaml")

    FileUpgradeExecutor(tmp_path).rollback_upgrade(first)

    assert (tmp_path / "config" / "policies.yaml").read_text(encoding="utf-8") == ORIGINAL
    assert (tmp_path / "config" / "routing.yaml").read_text(encoding="utf-8") == CHANGED, (
        "rolling back one proposal must not undo another"
    )


def test_an_applied_proposal_with_no_backup_record_fails_loudly(tmp_path: Path) -> None:
    """The false green, now an error.

    History says this was applied and there is no manifest to restore from, so
    the files it changed cannot be put back. The old code iterated an empty map
    and returned `True`.
    """
    proposal = _proposal()
    executor = FileUpgradeExecutor(tmp_path)
    executor._record_history(proposal, "applied")

    with pytest.raises(RollbackError, match="no backup manifest"):
        executor.rollback_upgrade(proposal)


def test_a_backup_recorded_but_missing_from_disk_fails_loudly(tmp_path: Path) -> None:
    """A manifest naming a file that is gone is not something to shrug at."""
    proposal = _proposal()
    _applied_change(tmp_path, proposal)
    (tmp_path / "upgrades" / "backups" / proposal.id / "policies.yaml").unlink()

    with pytest.raises(RollbackError, match="recorded a backup"):
        FileUpgradeExecutor(tmp_path).rollback_upgrade(proposal)


def test_rolling_back_a_proposal_that_never_applied_is_not_an_error(tmp_path: Path) -> None:
    """The common case today, and the one that must NOT raise.

    Every category resolves to `_skip_no_sink`, so the loop's failure path calls
    rollback for a proposal that changed nothing. That is a genuine no-op, and
    the receipt says which of the two kinds of success it was.
    """
    proposal = _proposal()
    executor = FileUpgradeExecutor(tmp_path)
    executor._record_history(proposal, "skipped_no_sink")

    assert executor.rollback_upgrade(proposal) is True
    receipt = json.loads((tmp_path / "upgrades" / "rollbacks" / f"{proposal.id}.json").read_text(encoding="utf-8"))
    assert receipt["restored_files"] == []
    assert receipt["note"] == "nothing was applied"


def test_a_rollback_leaves_a_receipt_that_verifies_offline(tmp_path: Path) -> None:
    """Rollback recorded nothing at all before this.

    The hash is over the receipt body's canonical JSON, the same shape
    `change_contract_replay.write_verdict_receipt` uses, so a reader holding
    only the file can tell whether it has been edited since it was written.
    """
    import hashlib

    proposal = _proposal()
    _applied_change(tmp_path, proposal)
    FileUpgradeExecutor(tmp_path).rollback_upgrade(proposal)

    path = tmp_path / "upgrades" / "rollbacks" / f"{proposal.id}.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["proposal_id"] == proposal.id
    assert receipt["restored_files"] == ["policies.yaml"]

    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert receipt["receipt_hash"] == hashlib.sha256(canonical).hexdigest()

    # And an edited receipt does not verify, or the hash is decoration.
    tampered = dict(body, restored_files=["policies.yaml", "routing.yaml"])
    tampered_canonical = json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert receipt["receipt_hash"] != hashlib.sha256(tampered_canonical).hexdigest()


def test_a_rolled_back_proposal_can_be_rolled_back_again_without_raising(tmp_path: Path) -> None:
    """Idempotence, because the loop's failure path is not guaranteed to run once.

    After a rollback, history's last word for the proposal is `rolled_back`, so
    a second call sees nothing applied rather than an apply it cannot undo.
    """
    proposal = _proposal()
    _applied_change(tmp_path, proposal)
    executor = FileUpgradeExecutor(tmp_path)
    assert executor.rollback_upgrade(proposal) is True
    assert executor.rollback_upgrade(proposal) is True
    assert (tmp_path / "config" / "policies.yaml").read_text(encoding="utf-8") == ORIGINAL
