"""Invariant: every automatic stall kill leaves a chained escalation receipt.

Each worker kill decided by the three escalation paths in ``heartbeat``
(``_escalate_heartbeat``, ``_escalate_stall_simple``, ``_escalate_stall_profiled``)
emits a signed, journal-anchored escalation receipt after the kill is issued,
mirrored into the HMAC audit chain as ``escalation.receipt`` -- chained
directly off the ``stall.verdict`` that recorded the decision first (order:
verdict -> kill -> receipt).

Like the verdict, emission is best-effort: a missing journal, a failing
assembly, or a chain write failure must never block or undo the kill, and the
failure stays observable as a warning naming the session. These tests prove
the ordering and the best-effort contract by asserting on the chain and the
on-disk receipt (never that a mock was called) and by driving the real kill
paths with raising assembly and chain stores.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.models import AgentHeartbeat, AgentSession, ModelConfig

from bernstein.core.agents.heartbeat import (
    AGENT,
    StallProfile,
    _escalate_heartbeat,
    _escalate_stall_profiled,
    _escalate_stall_simple,
    check_stale_agents,
)
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.audit_chain import (
    EVENT_ESCALATION_RECEIPT,
    EVENT_STALL_VERDICT,
    AuditChainStore,
)

RUN_ID = "run-receipt-1"


def _session(sid: str = "A-1", task_id: str = "T-1", *, pid: int | None = 4321) -> AgentSession:
    return AgentSession(
        id=sid,
        role="backend",
        task_ids=[task_id],
        status="working",
        spawn_ts=100.0,
        pid=pid,
        model_config=ModelConfig("sonnet", "high"),
    )


def _build_journal(workdir: Path, *, n_events: int = 20) -> None:
    (workdir / ".sdd").mkdir(exist_ok=True)
    journal = EventJournal(RUN_ID, workdir / ".sdd")
    for i in range(n_events):
        journal.record("task.tick", session_id="A-1", seq=i)
    del journal


def _chain_verdicts(workdir: Path) -> list[object]:
    chain = AuditChainStore(workdir / ".sdd" / "audit")
    return chain.query(event_type=EVENT_STALL_VERDICT)


def _chain_receipts(workdir: Path) -> list[object]:
    chain = AuditChainStore(workdir / ".sdd" / "audit")
    return chain.query(event_type=EVENT_ESCALATION_RECEIPT)


def _receipt_files(workdir: Path) -> list[Path]:
    return sorted((workdir / ".sdd" / "escalation" / "receipts").glob("*.json"))


def _heartbeat_orch(workdir: Path, session: AgentSession) -> SimpleNamespace:
    return SimpleNamespace(
        _agents={session.id: session},
        _workdir=workdir,
        _run_id=RUN_ID,
        _signal_mgr=MagicMock(),
        _spawner=MagicMock(),
        _stall_counts={},
    )


def _verify_auto_receipt(workdir: Path, receipt_path: Path) -> tuple[bool, str]:
    from bernstein.core.orchestration.escalation import verify_escalation_receipt
    from bernstein.core.security.audit import load_or_create_audit_key

    result = verify_escalation_receipt(
        sdd_dir=workdir / ".sdd",
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=load_or_create_audit_key(),
        receipt_id=receipt_path.stem,
    )
    return result.ok, result.reason


# ---------------------------------------------------------------------------
# Ordering: verdict -> kill -> receipt, chained off the verdict
# ---------------------------------------------------------------------------


def test_heartbeat_kill_emits_receipt_chained_off_verdict(tmp_path: Path) -> None:
    """After a heartbeat kill the receipt event chains directly off the verdict."""
    _build_journal(tmp_path)
    orch = _heartbeat_orch(tmp_path, _session())
    with patch("bernstein.core.platform_compat.kill_process_group"):
        _escalate_heartbeat(
            orch,
            orch._agents["A-1"],
            age=950.0,
            elapsed=850.0,
            shutdown_threshold=60.0,
            wakeup_threshold=30.0,
            shutdown_reason="no_heartbeat",
            kill_threshold=90.0,
        )

    verdicts = _chain_verdicts(tmp_path)
    receipts = _chain_receipts(tmp_path)
    assert len(verdicts) == 1
    assert len(receipts) == 1
    # The receipt embeds the verdict's own HMAC as its predecessor.
    assert receipts[0].details["prev_chain_digest"] == verdicts[0].hmac
    ok, errors = AuditChainStore(tmp_path / ".sdd" / "audit").verify()
    assert ok, errors
    # A signed receipt landed on disk, one per kill.
    assert len(_receipt_files(tmp_path)) == 1


def test_signal_between_verdict_and_receipt(tmp_path: Path) -> None:
    """At the moment the kill runs the verdict is recorded and no receipt yet."""
    _build_journal(tmp_path)
    orch = _heartbeat_orch(tmp_path, _session())

    seen: dict[str, int] = {}

    def fake_kill(session: object) -> None:
        seen["verdicts_at_kill"] = len(_chain_verdicts(tmp_path))
        seen["receipts_at_kill"] = len(_chain_receipts(tmp_path))

    orch._spawner.kill.side_effect = fake_kill
    _escalate_stall_simple(orch, orch._agents["A-1"], "T-1", count=AGENT.escalation_kill_count)

    assert seen["verdicts_at_kill"] == 1
    assert seen["receipts_at_kill"] == 0
    assert len(_chain_receipts(tmp_path)) == 1
    assert orch._spawner.kill.called


# ---------------------------------------------------------------------------
# Session link and the single verifier
# ---------------------------------------------------------------------------


def test_receipt_records_session_link_in_chain_and_on_disk(tmp_path: Path) -> None:
    """The session link is read from the record, not reconstructed."""
    _build_journal(tmp_path)
    orch = _heartbeat_orch(tmp_path, _session(sid="B-7", task_id="T-9"))
    _escalate_stall_simple(orch, orch._agents["B-7"], "T-9", count=AGENT.escalation_kill_count)

    rows = _chain_receipts(tmp_path)
    assert len(rows) == 1
    assert rows[0].details["session_id"] == "B-7"
    files = _receipt_files(tmp_path)
    assert len(files) == 1
    row = json.loads(files[0].read_text(encoding="utf-8"))
    assert row["session_id"] == "B-7"
    expected_worktree_id = hashlib.sha256(str(tmp_path.resolve()).encode("utf-8")).hexdigest()[:16]
    assert row["worktree_id"] == expected_worktree_id


def test_auto_receipt_verifies_with_the_same_verifier(tmp_path: Path) -> None:
    """``verify_escalation_receipt`` accepts an automatic-path receipt unchanged."""
    _build_journal(tmp_path)
    orch = _heartbeat_orch(tmp_path, _session())
    _escalate_stall_profiled(
        orch,
        orch._agents["A-1"],
        "T-1",
        count=7,
        profile=StallProfile(wakeup_threshold=3, shutdown_threshold=5, kill_threshold=7, reason="default profile"),
    )

    files = _receipt_files(tmp_path)
    assert len(files) == 1
    ok, reason = _verify_auto_receipt(tmp_path, files[0])
    assert ok, reason


# ---------------------------------------------------------------------------
# Best-effort contract: failure never blocks the kill
# ---------------------------------------------------------------------------


def test_missing_journal_kills_and_records_verdict_with_degraded_receipt(tmp_path: Path) -> None:
    """No journal -> degraded receipt with journal_state='missing' is recorded."""
    orch = _heartbeat_orch(tmp_path, _session())
    _escalate_stall_simple(orch, orch._agents["A-1"], "T-1", count=AGENT.escalation_kill_count)

    assert orch._spawner.kill.called
    assert len(_chain_verdicts(tmp_path)) == 1
    receipts = _chain_receipts(tmp_path)
    assert len(receipts) == 1
    files = _receipt_files(tmp_path)
    assert len(files) == 1
    ok, reason = _verify_auto_receipt(tmp_path, files[0])
    assert ok, reason

    # The chain entry itself must carry the degradation: an auditor walking the
    # chain alone cannot otherwise tell a kill with an absent journal from one
    # whose window reconstructs.
    assert receipts[0].details["journal_state"] == "missing"


def test_present_journal_receipt_leaves_the_chain_payload_unchanged(tmp_path: Path) -> None:
    """``journal_state`` appears in the mirror only when it is not 'present'."""
    _build_journal(tmp_path)
    orch = _heartbeat_orch(tmp_path, _session())
    _escalate_stall_simple(orch, orch._agents["A-1"], "T-1", count=AGENT.escalation_kill_count)

    receipts = _chain_receipts(tmp_path)
    assert len(receipts) == 1
    assert "journal_state" not in receipts[0].details


def test_assembly_failure_never_blocks_the_kill(tmp_path: Path) -> None:
    """A raising assembly leaves the kill issued and the chain failure loud."""
    _build_journal(tmp_path)
    orch = _heartbeat_orch(tmp_path, _session())
    with (
        patch("bernstein.core.orchestration.escalation.assemble_escalation_receipt", side_effect=RuntimeError("boom")),
        patch("bernstein.core.agents.heartbeat.logger") as log,
    ):
        _escalate_stall_simple(orch, orch._agents["A-1"], "T-1", count=AGENT.escalation_kill_count)

    assert orch._spawner.kill.called
    assert _receipt_files(tmp_path) == []
    warnings = [c for c in log.warning.call_args_list if "Could not emit escalation.receipt" in str(c.args[0])]
    assert warnings
    assert "A-1" in warnings[0].args


def test_chain_failure_never_blocks_kill_or_signals(tmp_path: Path) -> None:
    """With the chain store raising, the kill still happens and the SHUTDOWN
    signal that follows is unchanged; the receipt failure is loud too."""
    session = _session()
    _build_journal(tmp_path)
    from bernstein.core.agents.agent_signals import AgentSignalManager

    orch = SimpleNamespace(
        _agents={session.id: session},
        _workdir=tmp_path,
        _run_id=RUN_ID,
        _signal_mgr=AgentSignalManager(tmp_path),
        _spawner=MagicMock(),
        _config=SimpleNamespace(heartbeat_enabled=True, heartbeat_timeout_s=60.0),
    )
    orch._signal_mgr.write_heartbeat("A-1", AgentHeartbeat(timestamp=1000.0, status="starting"))

    with (
        patch("bernstein.core.agents.heartbeat.time.time", return_value=1100.0),
        patch("bernstein.core.platform_compat.kill_process_group") as kpg,
        patch(
            "bernstein.core.security.audit_chain.AuditChainStore",
            side_effect=RuntimeError("audit backend down"),
        ),
        patch("bernstein.core.agents.heartbeat.logger") as log,
    ):
        check_stale_agents(orch)

    assert kpg.called
    shutdown_file = tmp_path / ".sdd" / "runtime" / "signals" / "A-1" / "SHUTDOWN"
    assert shutdown_file.exists()
    assert "no_heartbeat" in shutdown_file.read_text(encoding="utf-8")
    receipt_warnings = [c for c in log.warning.call_args_list if "Could not emit escalation.receipt" in str(c.args[0])]
    assert receipt_warnings, "an unrecorded receipt must stay observable via a warning"
    assert "A-1" in receipt_warnings[0].args
