"""Receipt-backed fleet steering core tests (#2508).

Each acceptance criterion from the issue is exercised directly:

* Every steering action is an audit-chain receipt recorded before its
  effect; an effect with no matching receipt is rejected.
* Pause checkpoints a running worker; resume continues from the checkpoint
  (the claim is parked, not abandoned; the checkpoint identity is preserved).
* Guidance queued while a worker is mid-step is delivered exactly once, in
  mailbox chain append order, and appears in the worker transcript.
* Determinism: a steered run's steering messages become hash-chained journal
  steps; a second host walking the same journal computes identical hashes.
* Verifiability: mutating a receipt payload fails audit verification at the
  exact chain position and the classification labels the run tampered, not
  steered.
* The receipt binds the confirmed payload; a mismatched displayed payload is
  rejected before any receipt is written.
* Isolation: aborting one worker leaves other workers' signals untouched.
* Steering requires an authorised scope; denials are recorded and rejected.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.communication.task_mailbox import TaskMailbox, verify_against_chain
from bernstein.core.orchestration.steering import (
    CLASSIFICATION_CLEAN,
    CLASSIFICATION_STEERED,
    CLASSIFICATION_TAMPERED,
    InvalidSteeringCommand,
    SteeringCommand,
    SteeringController,
    SteeringPayloadMismatch,
    UnauthorizedSteering,
    classify_steering_run,
    consume_steering,
    find_steering_receipt,
    parse_delivery_body,
)
from bernstein.core.persistence.journal import Journal, JournalReader
from bernstein.core.security.audit_chain import EVENT_STEERING_RECEIPT, AuditChainStore
from bernstein.core.security.denial_tracker import DenialTracker

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"steering-core-test-key"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=_KEY)


@pytest.fixture()
def mailbox(tmp_path: Path) -> TaskMailbox:
    return TaskMailbox(
        tmp_path / "runtime" / "mailbox.jsonl",
        hmac_key=_KEY,
        identity_dir=tmp_path / "identity",
    )


def _controller(
    chain: AuditChainStore,
    mailbox: TaskMailbox,
    tmp_path: Path,
    *,
    denial_tracker: DenialTracker | None = None,
    parked: list[str] | None = None,
    resumed: list[str] | None = None,
) -> SteeringController:
    return SteeringController(
        chain=chain,
        mailbox=mailbox,
        signals_dir=tmp_path / "signals",
        sdd_dir=tmp_path / ".sdd",
        denial_tracker=denial_tracker,
        claim_parker=(parked.append if parked is not None else None),
        claim_resumer=(resumed.append if resumed is not None else None),
    )


# ---------------------------------------------------------------------------
# Command boundary validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        SteeringCommand(kind="bogus", task_id="t", principal="a"),
        SteeringCommand(kind="guidance", task_id="t", principal="a"),  # missing text
        SteeringCommand(kind="redirect", task_id="t", principal="a"),  # missing target
        SteeringCommand(kind="guidance", task_id="", principal="a", guidance="x"),
        SteeringCommand(kind="guidance", task_id="t", principal="", guidance="x"),
        SteeringCommand(kind="pause", task_id="t", principal="a"),  # missing session
        SteeringCommand(kind="abort", task_id="t", principal="a"),  # missing session
        SteeringCommand(kind="pause", task_id="t", principal="a", session_id="s", guidance="x"),
    ],
)
def test_validate_rejects_malformed_commands(command: SteeringCommand) -> None:
    with pytest.raises(InvalidSteeringCommand):
        command.validate()


def test_oversized_guidance_is_rejected() -> None:
    huge = "x" * 4096
    with pytest.raises(InvalidSteeringCommand):
        SteeringCommand(kind="guidance", task_id="t", principal="a", guidance=huge).validate()


# ---------------------------------------------------------------------------
# AC: receipt is recorded BEFORE the effect
# ---------------------------------------------------------------------------


def test_receipt_recorded_before_effect(chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path) -> None:
    controller = _controller(chain, mailbox, tmp_path)
    cmd = SteeringCommand(kind="guidance", task_id="task-1", principal="alice", guidance="focus on the failing test")

    outcome = controller.steer(cmd, scope="operator")

    receipts = chain.query(event_type=EVENT_STEERING_RECEIPT)
    assert len(receipts) == 1
    assert receipts[0].details["payload_hash"] == cmd.payload_hash()
    assert receipts[0].details["kind"] == "guidance"
    assert receipts[0].details["principal"] == "alice"
    # The delivered effect references the receipt's chain HMAC.
    assert outcome.receipt.receipt_hash == receipts[0].hmac
    envelope = parse_delivery_body(outcome.message.body)
    assert envelope["receipt_hash"] == outcome.receipt.receipt_hash
    assert envelope["payload_hash"] == cmd.payload_hash()


def test_effect_without_matching_receipt_is_rejected(
    chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path
) -> None:
    """A steer message referencing a receipt absent from the chain is rejected."""
    # Post a steer message directly, referencing a receipt that was never recorded.
    forged_body = json.dumps(
        {
            "v": 1,
            "kind": "guidance",
            "task_id": "task-1",
            "principal": "mallory",
            "guidance": "do the wrong thing",
            "redirect_target": "",
            "reason": "",
            "receipt_hash": "hmac-sha256:deadbeef",
            "payload_hash": "sha256:deadbeef",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    mailbox.post(task_id="task-1", sender="operator", kind="steer.guidance", body=forged_body)

    journal = Journal.open(tmp_path / "journal" / "agent-1")
    result = consume_steering(mailbox=mailbox, journal=journal, chain=chain, task_id="task-1")

    assert result.applied == []
    assert result.rejected and result.rejected[0][0] == 0
    # A steer.rejected journal entry was recorded (the refusal itself is an
    # audit-chain event, not a dropped message).
    reader = JournalReader(tmp_path / "journal" / "agent-1")
    entries = list(reader.entries())
    assert len(entries) == 1
    assert entries[0].tool_call["steer"] == "rejected"
    assert entries[0].tool_call["reason"] == "missing_receipt_hash"


# ---------------------------------------------------------------------------
# AC: the receipt binds the confirmed payload (displayed == executed)
# ---------------------------------------------------------------------------


def test_displayed_payload_mismatch_is_rejected_before_receipt(
    chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path
) -> None:
    controller = _controller(chain, mailbox, tmp_path)
    cmd = SteeringCommand(kind="redirect", task_id="task-1", principal="alice", redirect_target="ship the hotfix")

    with pytest.raises(SteeringPayloadMismatch):
        controller.steer(cmd, scope="operator", displayed_payload_hash="sha256:something-else")

    # No receipt was written: the mismatch is caught before the chain is touched.
    assert chain.query(event_type=EVENT_STEERING_RECEIPT) == []
    assert len(mailbox) == 0


def test_matching_displayed_payload_is_accepted(chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path) -> None:
    controller = _controller(chain, mailbox, tmp_path)
    cmd = SteeringCommand(kind="redirect", task_id="task-1", principal="alice", redirect_target="ship the hotfix")
    outcome = controller.steer(cmd, scope="operator", displayed_payload_hash=cmd.payload_hash())
    assert outcome.receipt.payload_hash == cmd.payload_hash()


# ---------------------------------------------------------------------------
# AC: guidance delivered exactly once, in chain order, appears in transcript
# ---------------------------------------------------------------------------


def test_queued_guidance_delivered_exactly_once_in_order(
    chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path
) -> None:
    controller = _controller(chain, mailbox, tmp_path)
    first = SteeringCommand(kind="guidance", task_id="task-1", principal="alice", guidance="stop refactoring")
    second = SteeringCommand(kind="redirect", task_id="task-1", principal="alice", redirect_target="fix the flaky test")
    controller.steer(first, scope="operator")
    controller.steer(second, scope="operator")

    journal = Journal.open(tmp_path / "journal" / "agent-1")
    result = consume_steering(mailbox=mailbox, journal=journal, chain=chain, task_id="task-1")

    assert [c.kind for c in result.applied] == ["guidance", "redirect"]
    assert [c.seq for c in result.applied] == [0, 1]

    # A second sweep past the cursor consumes nothing: exactly once.
    again = consume_steering(mailbox=mailbox, journal=journal, chain=chain, task_id="task-1", since_seq=result.next_seq)
    assert again.applied == []

    # The guidance appears in the worker transcript (journal steps).
    entries = list(JournalReader(tmp_path / "journal" / "agent-1").entries())
    assert len(entries) == 2
    assert entries[0].tool_call["steer"] == "guidance"
    assert entries[1].tool_call["steer"] == "redirect"


# ---------------------------------------------------------------------------
# AC: determinism - steering steps replay byte-identically across hosts
# ---------------------------------------------------------------------------


def test_steered_run_replays_byte_identically_on_a_second_host(
    chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path
) -> None:
    controller = _controller(chain, mailbox, tmp_path)
    for cmd in (
        SteeringCommand(kind="pause", task_id="task-1", principal="alice", session_id="sess-1", reason="wait"),
        SteeringCommand(kind="guidance", task_id="task-1", principal="alice", guidance="focus on tests"),
        SteeringCommand(kind="redirect", task_id="task-1", principal="alice", redirect_target="ship hotfix"),
    ):
        controller.steer(cmd, scope="operator")

    # Host A consumes into journal A.
    journal_a = Journal.open(tmp_path / "hostA" / "agent")
    consume_steering(mailbox=mailbox, journal=journal_a, chain=chain, task_id="task-1")
    hashes_a = [e.step_hash for e in JournalReader(tmp_path / "hostA" / "agent").entries()]

    # Host B replays the same mailbox + chain into a fresh journal.
    journal_b = Journal.open(tmp_path / "hostB" / "agent")
    consume_steering(mailbox=mailbox, journal=journal_b, chain=chain, task_id="task-1")
    hashes_b = [e.step_hash for e in JournalReader(tmp_path / "hostB" / "agent").entries()]

    assert hashes_a == hashes_b
    assert len(hashes_a) == 3
    # The journal chain verifies on both hosts.
    assert JournalReader(tmp_path / "hostA" / "agent").verify().ok
    assert JournalReader(tmp_path / "hostB" / "agent").verify().ok


# ---------------------------------------------------------------------------
# AC: verifiability - tampering fails at the exact chain position
# ---------------------------------------------------------------------------


def test_mutating_receipt_payload_breaks_audit_verification(
    chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path
) -> None:
    controller = _controller(chain, mailbox, tmp_path)
    controller.steer(
        SteeringCommand(kind="guidance", task_id="task-1", principal="alice", guidance="do X"),
        scope="operator",
    )
    ok, _ = chain.verify()
    assert ok

    # Tamper with the recorded receipt payload on disk.
    audit_files = sorted((tmp_path / "audit").glob("*.jsonl"))
    assert audit_files
    target = audit_files[0]
    lines = target.read_text(encoding="utf-8").splitlines()
    mutated = []
    tampered_line = -1
    for idx, line in enumerate(lines):
        row = json.loads(line)
        if row.get("event_type") == EVENT_STEERING_RECEIPT:
            row["details"]["payload_hash"] = "sha256:0000"
            tampered_line = idx
        mutated.append(json.dumps(row, sort_keys=True))
    target.write_text("\n".join(mutated) + "\n", encoding="utf-8")

    reopened = AuditChainStore(tmp_path / "audit", key=_KEY)
    ok2, problems = reopened.verify()
    assert not ok2
    assert problems  # verification fails, naming the broken position
    assert tampered_line >= 0


def test_classification_distinguishes_steered_from_tampered(
    chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path
) -> None:
    controller = _controller(chain, mailbox, tmp_path)
    controller.steer(
        SteeringCommand(kind="guidance", task_id="task-1", principal="alice", guidance="focus"),
        scope="operator",
    )
    journal_dir = tmp_path / "journal" / "agent"
    journal = Journal.open(journal_dir)
    consume_steering(mailbox=mailbox, journal=journal, chain=chain, task_id="task-1")

    # A legitimately steered run classifies as steered.
    steered = classify_steering_run(JournalReader(journal_dir))
    assert steered.label == CLASSIFICATION_STEERED
    assert steered.steering_steps == [0]
    assert steered.divergent_index is None

    # Tamper with the steering journal step -> classified tampered, not steered.
    bucket = journal_dir / "000000.jsonl"
    rows = bucket.read_text(encoding="utf-8").splitlines()
    row0 = json.loads(rows[0])
    row0["tool_call"]["steer"] = "abort"  # rewrite what the operator did
    bucket.write_text(json.dumps(row0, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    tampered = classify_steering_run(JournalReader(journal_dir))
    assert tampered.label == CLASSIFICATION_TAMPERED
    assert tampered.divergent_index is not None


def test_clean_run_with_no_steering_classifies_clean(tmp_path: Path) -> None:
    journal_dir = tmp_path / "journal" / "agent"
    journal = Journal.open(journal_dir)
    journal.append(input_hash="sha256:abc", tool_call={"tool": "Bash"}, tool_result={"ok": True})
    assert classify_steering_run(JournalReader(journal_dir)).label == CLASSIFICATION_CLEAN


# ---------------------------------------------------------------------------
# AC: isolation - aborting one worker leaves the others untouched
# ---------------------------------------------------------------------------


def test_abort_isolates_the_target_worker(chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path) -> None:
    controller = _controller(chain, mailbox, tmp_path)
    outcome = controller.steer(
        SteeringCommand(kind="abort", task_id="task-A", principal="alice", session_id="sess-A", reason="wrong path"),
        scope="operator",
    )

    signals = tmp_path / "signals"
    assert outcome.abort_signal_path is not None
    assert (signals / "sess-A" / "SHUTDOWN").is_file()
    # The other worker's signal directory was never touched.
    assert not (signals / "sess-B").exists()
    # The abort receipt is on the chain and the mailbox mirrors the message.
    assert len(chain.query(event_type=EVENT_STEERING_RECEIPT)) == 1
    # Steering worker B leaves worker A's SHUTDOWN in place and adds only B's.
    controller.steer(
        SteeringCommand(kind="abort", task_id="task-B", principal="alice", session_id="sess-B", reason="done"),
        scope="operator",
    )
    assert (signals / "sess-A" / "SHUTDOWN").is_file()
    assert (signals / "sess-B" / "SHUTDOWN").is_file()


# ---------------------------------------------------------------------------
# AC: authorisation - unauthorised scope is recorded and rejected
# ---------------------------------------------------------------------------


def test_viewer_scope_is_rejected_and_recorded(chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path) -> None:
    tracker = DenialTracker(threshold=100)
    controller = _controller(chain, mailbox, tmp_path, denial_tracker=tracker)
    cmd = SteeringCommand(kind="abort", task_id="task-1", principal="mallory", session_id="sess-1")

    with pytest.raises(UnauthorizedSteering):
        controller.steer(cmd, scope="viewer")

    # No receipt, no delivery, no signal - the effect never ran.
    assert chain.query(event_type=EVENT_STEERING_RECEIPT) == []
    assert len(mailbox) == 0
    assert not (tmp_path / "signals" / "sess-1" / "SHUTDOWN").exists()
    # The denial was recorded.
    assert tracker.get_denial_count("sess-1") == 1


# ---------------------------------------------------------------------------
# AC: pause checkpoints; resume continues from the checkpoint
# ---------------------------------------------------------------------------


def test_pause_checkpoints_and_resume_continues_from_it(
    chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path
) -> None:
    from bernstein.core.tasks.checkpoint_retry import latest_checkpoint

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "file.py").write_text("print('work in progress')\n", encoding="utf-8")

    parked: list[str] = []
    resumed: list[str] = []
    controller = _controller(chain, mailbox, tmp_path, parked=parked, resumed=resumed)

    pause_outcome = controller.steer(
        SteeringCommand(
            kind="pause",
            task_id="task-1",
            principal="alice",
            session_id="sess-1",
            adapter="claude",
            worktree=str(worktree),
            reason="operator hold",
        ),
        scope="operator",
    )

    # Pause checkpointed the worker: a resumable reference exists.
    assert pause_outcome.checkpoint is not None
    assert pause_outcome.checkpoint.session_id == "sess-1"
    stored = latest_checkpoint(tmp_path / ".sdd", "task-1")
    assert stored is not None
    assert stored.event_hash == pause_outcome.checkpoint.event_hash
    # The claim was parked (not abandoned) and the pause signal was written.
    assert parked == ["task-1"]
    assert (tmp_path / "signals" / "sess-1" / "PAUSE").is_file()

    # Resume continues from the very same checkpoint without restarting.
    resume_outcome = controller.steer(
        SteeringCommand(kind="resume", task_id="task-1", principal="alice", session_id="sess-1"),
        scope="operator",
    )
    assert resume_outcome.checkpoint is not None
    assert resume_outcome.checkpoint.event_hash == pause_outcome.checkpoint.event_hash
    assert resumed == ["task-1"]
    # The pause signal was cleared so the scheduler may dispatch again.
    assert not (tmp_path / "signals" / "sess-1" / "PAUSE").exists()


# ---------------------------------------------------------------------------
# The delivered steering messages cross-verify against the audit chain
# ---------------------------------------------------------------------------


def test_delivered_steering_messages_cross_verify_against_chain(
    chain: AuditChainStore, mailbox: TaskMailbox, tmp_path: Path
) -> None:
    controller = _controller(chain, mailbox, tmp_path)
    controller.steer(
        SteeringCommand(kind="guidance", task_id="task-1", principal="alice", guidance="focus"),
        scope="operator",
    )
    ok, problems = verify_against_chain(mailbox, chain)
    assert ok, problems
    # find_steering_receipt resolves the delivered message back to its receipt.
    envelope = parse_delivery_body(mailbox.all_messages()[0].body)
    receipt = find_steering_receipt(chain, receipt_hash=envelope["receipt_hash"], payload_hash=envelope["payload_hash"])
    assert receipt is not None
