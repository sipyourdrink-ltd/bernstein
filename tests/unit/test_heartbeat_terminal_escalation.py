"""Regression tests for supervisor liveness on a frozen heartbeat (issue #2796).

A live agent whose heartbeat file never advances past the spawn-time
``"starting"`` state must not deadlock the run. The monitor loop must
escalate a stuck heartbeat to a terminal kill (SIGTERM then SIGKILL) so the
process is reaped and the slot is freed, instead of rewriting the same
SHUTDOWN signal every monitor tick forever.

These tests drive the real production monitor path: ``check_stale_agents``
runs with a real ``_workdir`` (so it takes the ``HeartbeatMonitor`` branch,
not the workdir-less ``_check_stale_agents_simple`` fallback) and a real
``AgentSignalManager`` reading/writing real signal files. The heartbeat is a
real file on disk; the SHUTDOWN dedup is the real one in
``AgentSignalManager.write_shutdown``.
"""

from __future__ import annotations

import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.heartbeat import check_stale_agents
from bernstein.core.models import AgentHeartbeat, AgentSession, ModelConfig

from bernstein.core.agents.agent_signals import AgentSignalManager


def _session(task_id: str, *, pid: int | None = 4321) -> AgentSession:
    """A live agent session whose heartbeat never advances past spawn."""
    return AgentSession(
        id="A-1",
        role="manager",
        task_ids=[task_id],
        status="working",
        spawn_ts=100.0,
        pid=pid,
        model_config=ModelConfig("sonnet", "high"),
    )


def _orch(workdir: Path, *, timeout_s: float) -> SimpleNamespace:
    """A minimal orchestrator stand-in that routes through the workdir path.

    ``_workdir`` is a real ``Path`` so ``check_stale_agents`` takes the
    production ``HeartbeatMonitor`` branch (with its ``kill_after_s =
    heartbeat_timeout_s + kill_grace_s`` threshold math), not the
    workdir-less ``_check_stale_agents_simple`` fallback.
    """
    return SimpleNamespace(
        _agents={"A-1": _session("T-1")},
        _signal_mgr=AgentSignalManager(workdir),
        _spawner=MagicMock(),
        _workdir=workdir,
        _config=SimpleNamespace(heartbeat_enabled=True, heartbeat_timeout_s=timeout_s),
    )


def test_write_shutdown_is_idempotent_per_reason(tmp_path) -> None:
    """write_shutdown emits and logs the same reason at most once per episode."""
    mgr = AgentSignalManager(tmp_path)

    with patch("bernstein.core.agents.agent_signals.logger") as log:
        first = mgr.write_shutdown("A-1", reason="no_heartbeat", task_title="T-1")
        second = mgr.write_shutdown("A-1", reason="no_heartbeat", task_title="T-1")
        third = mgr.write_shutdown("A-1", reason="no_heartbeat", task_title="T-1")

    assert first is True
    assert second is False
    assert third is False
    # Logged exactly once for the whole stall episode, not once per tick.
    assert log.info.call_count == 1

    # A new episode (after signals are cleared) re-emits.
    mgr.clear_signals("A-1")
    with patch("bernstein.core.agents.agent_signals.logger") as log2:
        again = mgr.write_shutdown("A-1", reason="no_heartbeat", task_title="T-1")
    assert again is True
    assert log2.info.call_count == 1


def test_stale_heartbeat_escalates_to_terminal_kill(tmp_path: Path) -> None:
    """A heartbeat frozen past the production SIGKILL threshold force-kills.

    ``heartbeat_timeout_s`` is 60, so the production kill threshold is
    ``60 + kill_grace_s (30) = 90``. The frozen age is 100: past the
    production threshold but *below* the workdir-less fallback's fixed
    ``escalation_sigkill_s`` (150). A SIGKILL therefore proves the run went
    through ``check_stale_agents``' production threshold math, not the
    ``_check_stale_agents_simple`` fallback the previous test exercised.
    """
    orch = _orch(tmp_path, timeout_s=60.0)
    # Heartbeat frozen at t=1000; the monitor tick runs at t=1100 (age 100s).
    orch._signal_mgr.write_heartbeat("A-1", AgentHeartbeat(timestamp=1000.0, status="starting"))

    with (
        patch("bernstein.core.agents.heartbeat.time.time", return_value=1100.0),
        patch("bernstein.core.platform_compat.kill_process_group") as kpg,
    ):
        check_stale_agents(orch)

    # The stuck process is force-killed (SIGKILL to its pid), not merely
    # signalled via the SHUTDOWN file.
    assert kpg.called, "a frozen heartbeat past the kill threshold must be force-killed"
    kill_args = kpg.call_args.args
    assert kill_args[0] == 4321
    assert signal.SIGKILL in kill_args


def test_stale_heartbeat_does_not_loop_shutdown_forever(tmp_path: Path) -> None:
    """Repeated monitor ticks on a frozen heartbeat write SHUTDOWN at most once.

    The dedup under test is the real one in ``AgentSignalManager.write_shutdown``
    (a real signal directory on disk), reached through the production
    ``check_stale_agents`` path -- not an emulated ``side_effect`` on a mock.
    Age 70 sits in SHUTDOWN territory (>= timeout 60) but below the kill
    threshold (90), so the run stays in the soft-signal tier across ticks.
    """
    orch = _orch(tmp_path, timeout_s=60.0)
    orch._signal_mgr.write_heartbeat("A-1", AgentHeartbeat(timestamp=1000.0, status="starting"))

    with (
        patch("bernstein.core.agents.heartbeat.time.time", return_value=1070.0),
        patch("bernstein.core.platform_compat.kill_process_group"),
        patch("bernstein.core.agents.agent_signals.logger") as log,
    ):
        for _ in range(5):
            check_stale_agents(orch)

    # The SHUTDOWN file is written and logged exactly once for the whole
    # stall episode, not once per tick.
    assert log.info.call_count == 1
    shutdown_file = tmp_path / ".sdd" / "runtime" / "signals" / "A-1" / "SHUTDOWN"
    assert shutdown_file.exists()
    assert "Reason: no_heartbeat" in shutdown_file.read_text(encoding="utf-8")
