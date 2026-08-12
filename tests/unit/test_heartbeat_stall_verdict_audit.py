"""Invariant: no automatic kill leaves the audit chain without a stall verdict.

Every worker kill decided by the three escalation paths in ``heartbeat``
(``_escalate_heartbeat``, ``_escalate_stall_simple``, ``_escalate_stall_profiled``)
must be mirrored into the HMAC audit chain as a ``stall.verdict`` event before
the kill is issued -- exactly once, even across retries, with the detector, the
reason, and the measured inputs that actually drove the decision.

The mirror is best-effort: a chain failure must never block or delay the kill,
and an unrecorded kill must stay observable. The tests prove both halves by
asserting the record exists in the chain (never that a mock was called) and by
driving the real kill path with a raising chain store.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.models import (
    AgentHeartbeat,
    AgentSession,
    ModelConfig,
    ProgressSnapshot,
)

from bernstein.core.agents.agent_signals import AgentSignalManager
from bernstein.core.agents.heartbeat import (
    AGENT,
    StallProfile,
    _escalate_heartbeat,
    _escalate_stall_profiled,
    _escalate_stall_simple,
    check_stale_agents,
    check_stalled_tasks,
)
from bernstein.core.security.audit_chain import (
    EVENT_STALL_VERDICT,
    AuditChainStore,
)


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


def _chain_verdicts(tmp_path: Path) -> list[object]:
    chain = AuditChainStore(tmp_path / ".sdd" / "audit")
    return chain.query(event_type=EVENT_STALL_VERDICT)


def _heartbeat_orch(tmp_path: Path, session: AgentSession) -> SimpleNamespace:
    (tmp_path / ".sdd").mkdir(exist_ok=True)
    return SimpleNamespace(
        _agents={session.id: session},
        _workdir=tmp_path,
        _signal_mgr=MagicMock(),
        _spawner=MagicMock(),
        _stall_counts={},
        _config=SimpleNamespace(),
    )


# ---------------------------------------------------------------------------
# Site 1: _escalate_heartbeat (SIGKILL tier)
# ---------------------------------------------------------------------------


def test_heartbeat_escalation_kill_records_verdict(tmp_path: Path) -> None:
    """The heartbeat detector records the age and the threshold it crossed."""
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

    rows = _chain_verdicts(tmp_path)
    assert len(rows) == 1
    details = rows[0].details
    assert details["session_id"] == "A-1"
    assert details["reason"] == "heartbeat_stale"
    assert details["detector"] == "heartbeat"
    assert details["heartbeat_age_s"] == 950.0
    assert details["identical_snapshot_count"] is None
    assert details["threshold"] == 90.0
    ok, errors = AuditChainStore(tmp_path / ".sdd" / "audit").verify()
    assert ok, errors


def test_heartbeat_escalation_kill_records_verdict_once_across_retries(tmp_path: Path) -> None:
    """A retry of the same frozen heartbeat does not record a second verdict.

    The escalation ladder is idempotent per tier, so the kill branch is entered
    once per episode; the verdict count must track the kill count.
    """
    orch = _heartbeat_orch(tmp_path, _session())
    with patch("bernstein.core.platform_compat.kill_process_group"):
        for _ in range(2):
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

    rows = _chain_verdicts(tmp_path)
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Site 2: _escalate_stall_simple
# ---------------------------------------------------------------------------


def test_stall_simple_kill_records_verdict(tmp_path: Path) -> None:
    """The simple stall detector records the count and its fixed threshold."""
    orch = _heartbeat_orch(tmp_path, _session())
    _escalate_stall_simple(orch, orch._agents["A-1"], "T-1", count=AGENT.escalation_kill_count)

    assert orch._spawner.kill.called
    rows = _chain_verdicts(tmp_path)
    assert len(rows) == 1
    details = rows[0].details
    assert details["reason"] == "no_progress"
    assert details["detector"] == "stall_simple"
    assert details["identical_snapshot_count"] == AGENT.escalation_kill_count
    assert details["threshold"] == AGENT.escalation_kill_count
    assert details["heartbeat_age_s"] is None


# ---------------------------------------------------------------------------
# Site 3: _escalate_stall_profiled
# ---------------------------------------------------------------------------


def test_stall_profiled_kill_records_verdict(tmp_path: Path) -> None:
    """The profiled stall detector records the count and the profile threshold."""
    orch = _heartbeat_orch(tmp_path, _session())
    profile = StallProfile(wakeup_threshold=3, shutdown_threshold=5, kill_threshold=7, reason="default profile")
    _escalate_stall_profiled(orch, orch._agents["A-1"], "T-1", count=7, profile=profile)

    assert orch._spawner.kill.called
    rows = _chain_verdicts(tmp_path)
    assert len(rows) == 1
    details = rows[0].details
    assert details["reason"] == "no_progress"
    assert details["detector"] == "stall_profiled"
    assert details["identical_snapshot_count"] == 7
    assert details["threshold"] == 7
    assert details["heartbeat_age_s"] is None


def test_stalled_tasks_profiled_kill_records_verdict_end_to_end(tmp_path: Path) -> None:
    """The production check_stalled_tasks path records the strict-profile verdict."""
    session = _session()
    (tmp_path / ".sdd").mkdir(exist_ok=True)
    snap = {"timestamp": 10.0, "files_changed": 1, "tests_passing": 2, "errors": 0, "last_file": "a.py"}
    orch = SimpleNamespace(
        _agents={session.id: session},
        _workdir=tmp_path,
        _config=SimpleNamespace(server_url="http://srv", heartbeat_timeout_s=60.0),
        _client=MagicMock(),
        _last_snapshot_ts={},
        _last_snapshot={
            "T-1": ProgressSnapshot(timestamp=9.0, files_changed=1, tests_passing=2, errors=0, last_file="a.py")
        },
        _stall_counts={"T-1": 4},
        _signal_mgr=MagicMock(),
        _spawner=MagicMock(),
    )
    orch._client.get.return_value.json.return_value = [snap]
    orch._client.get.return_value.raise_for_status.return_value = None

    with patch("bernstein.core.agents.heartbeat.time.time", return_value=300.0):
        check_stalled_tasks(orch)

    orch._spawner.kill.assert_called_once()
    rows = _chain_verdicts(tmp_path)
    assert len(rows) == 1
    details = rows[0].details
    assert details["detector"] == "stall_profiled"
    assert details["reason"] == "no_progress"
    assert details["identical_snapshot_count"] == 5
    assert details["threshold"] == 5


# ---------------------------------------------------------------------------
# Best-effort contract: a chain failure never blocks the kill or the signals
# ---------------------------------------------------------------------------


def test_chain_failure_never_blocks_the_kill_or_the_signals(tmp_path: Path) -> None:
    """Recording is best-effort: with the chain store raising, the kill still
    happens, the WAKEUP/SHUTDOWN signal ordering is unchanged, and the failure
    is loud (a warning naming the session).
    """
    session = _session()
    orch = SimpleNamespace(
        _agents={session.id: session},
        _signal_mgr=AgentSignalManager(tmp_path),
        _spawner=MagicMock(),
        _workdir=tmp_path,
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

    # The kill still happens.
    assert kpg.called, "a chain write failure must never block the kill"
    # The SHUTDOWN signal that follows the kill is still written.
    shutdown_file = tmp_path / ".sdd" / "runtime" / "signals" / "A-1" / "SHUTDOWN"
    assert shutdown_file.exists()
    assert "no_heartbeat" in shutdown_file.read_text(encoding="utf-8")
    # The failure is loud: a warning naming the session, not a silent swallow.
    verdict_warnings = [c for c in log.warning.call_args_list if "Could not emit stall.verdict" in str(c.args[0])]
    assert verdict_warnings, "an unrecorded kill must stay observable via a warning"
    assert "A-1" in verdict_warnings[0].args


def test_heartbeat_kill_without_workdir_skips_record_but_still_kills(tmp_path: Path) -> None:
    """Without a workdir there is no chain to write, so the verdict is skipped
    silently -- but the kill is unaffected."""
    session = _session()
    orch = SimpleNamespace(
        _agents={session.id: session},
        _signal_mgr=MagicMock(),
        _spawner=MagicMock(),
        _stall_counts={},
    )
    _escalate_stall_simple(orch, session, "T-1", count=AGENT.escalation_kill_count)

    assert orch._spawner.kill.called
    assert not (tmp_path / ".sdd" / "audit").exists()
