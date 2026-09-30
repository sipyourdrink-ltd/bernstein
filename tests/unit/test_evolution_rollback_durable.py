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

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest
from bernstein.core.models import RiskAssessment, RollbackPlan

from bernstein.evolution.applicator import FileUpgradeExecutor
from bernstein.evolution.detector import UpgradeCategory
from bernstein.evolution.proposals import UpgradeProposal, UpgradeStatus
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
    executor._backup_file(filename, proposal)
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
    (tmp_path / "upgrades" / "backups" / proposal.storage_key / "policies.yaml").unlink()

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
    receipt = json.loads(
        (tmp_path / "upgrades" / "rollbacks" / f"{proposal.storage_key}.json").read_text(encoding="utf-8")
    )
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

    path = tmp_path / "upgrades" / "rollbacks" / f"{proposal.storage_key}.json"
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


# --- the breaker only hears about a real revert (#5408 follow-on) -----------
#
# `record_rollback` trips the circuit breaker on ANY rollback inside 48 hours,
# and the timestamp it checks is the one it just appended -- so one rollback is
# enough to halt evolution. That is the intended policy for a system that edits
# itself, and `test_rollback_trips` pins it.
#
# What it was being fed is the problem. `execute_upgrade` answers False for
# three different situations -- admission refused the proposal, the category
# had no sink, or an apply failed partway -- and only the last undid anything.
# Every category resolves to `_skip_no_sink` today, so the loop's failure path
# ran for a proposal that never touched the tree, recorded it as a rollback,
# and halted evolution on the FIRST proposal it ever saw, with a reason reading
# "Rollback detected".


def test_a_proposal_that_never_applied_is_not_a_rollback(tmp_path: Path) -> None:
    """The fact the loop needs, and could not previously ask for."""
    proposal = _proposal()
    executor = FileUpgradeExecutor(tmp_path)
    assert executor.execute_upgrade(proposal) is False, "no category has a sink today"
    assert executor.was_applied(proposal) is False


def test_a_proposal_that_applied_is_reported_as_applied(tmp_path: Path) -> None:
    proposal = _proposal()
    _applied_change(tmp_path, proposal)
    # A fresh executor, because the loop may not be the process that applied.
    assert FileUpgradeExecutor(tmp_path).was_applied(proposal) is True


def test_a_rolled_back_proposal_is_no_longer_reported_as_applied(tmp_path: Path) -> None:
    """Otherwise a second pass would record a second rollback for one change."""
    proposal = _proposal()
    _applied_change(tmp_path, proposal)
    executor = FileUpgradeExecutor(tmp_path)
    executor.rollback_upgrade(proposal)
    assert executor.was_applied(proposal) is False


# --- two runs' UPG-0001 are two proposals on disk (#6186) --------------------
#
# `ProposalGenerator` restarts its counter in every process, so the first
# proposal of every run is `UPG-0001`. The id is a label; the backup directory,
# the manifest, the history lookup and the rollback receipt are keyed on
# `storage_key`, which is derived from the proposal itself.


def _run_proposal(title: str, created_at: float) -> UpgradeProposal:
    return dataclasses.replace(_proposal("UPG-0001"), title=title, created_at=created_at)


def test_two_runs_upg_0001_do_not_overwrite_each_others_receipt(tmp_path: Path) -> None:
    first = _run_proposal("Rewrite the admission policy", created_at=1_000.0)
    second = _run_proposal("Disable the retry budget entirely", created_at=2_000.0)
    executor = FileUpgradeExecutor(tmp_path)

    executor.rollback_upgrade(first)
    FileUpgradeExecutor(tmp_path).rollback_upgrade(second)

    receipts = sorted((tmp_path / "upgrades" / "rollbacks").glob("*.json"))
    titles = sorted(json.loads(r.read_text(encoding="utf-8"))["title"] for r in receipts)
    assert titles == ["Disable the retry budget entirely", "Rewrite the admission policy"]


def test_a_never_applied_upg_0001_does_not_restore_another_runs_backup(tmp_path: Path) -> None:
    applied = _run_proposal("Disable the retry budget entirely", created_at=1_000.0)
    _applied_change(tmp_path, applied)
    unrelated = _run_proposal("Rewrite the admission policy", created_at=2_000.0)

    restarted = FileUpgradeExecutor(tmp_path)
    assert restarted.was_applied(unrelated) is False, "another run's `applied` row is not this proposal's"
    assert restarted.rollback_upgrade(unrelated) is True

    assert (restarted.config_dir / "policies.yaml").read_text(encoding="utf-8") == CHANGED, (
        "rolling back a proposal that never applied must not revert a different proposal's change"
    )


def test_the_storage_key_is_stable_for_one_proposal_and_distinct_across_runs() -> None:
    proposal = _run_proposal("Raise max_retries", created_at=1_000.0)
    later_in_its_life = dataclasses.replace(proposal, status=UpgradeStatus.APPLIED, applied_at=5.0)
    next_run = _run_proposal("Raise max_retries", created_at=2_000.0)

    assert later_in_its_life.storage_key == proposal.storage_key
    assert next_run.storage_key != proposal.storage_key
    assert proposal.storage_key.startswith("UPG-0001-"), "the label stays readable in paths"
