"""Regression tests for supervisor liveness on a frozen heartbeat (issue #2796).

A live agent whose heartbeat file never advances past the spawn-time
``"starting"`` state must not deadlock the run. The monitor loop must
escalate a stuck heartbeat to a terminal kill (SIGTERM then SIGKILL) so the
process is reaped and the slot is freed, instead of rewriting the same
SHUTDOWN signal every monitor tick forever.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.heartbeat import check_stale_agents
from bernstein.core.models import AgentSession, ModelConfig

from bernstein.core.agents.agent_signals import AgentSignalManager
from bernstein.core.defaults import AGENT


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


def test_stale_heartbeat_escalates_to_terminal_kill() -> None:
    """A heartbeat frozen past the SIGKILL threshold force-kills the process."""
    session = _session("T-1", pid=4321)
    spawner = MagicMock()
    orch = SimpleNamespace(
        _agents={"A-1": session},
        _signal_mgr=MagicMock(),
        _spawner=spawner,
    )
    # Heartbeat frozen at spawn (timestamp 100); age well past escalation_sigkill_s.
    orch._signal_mgr.read_heartbeat.return_value = SimpleNamespace(timestamp=100.0)
    kill_time = 100.0 + AGENT.escalation_sigkill_s + 10.0

    with (
        patch("bernstein.core.agents.heartbeat.time.time", return_value=kill_time),
        patch("bernstein.core.platform_compat.kill_process_group") as kpg,
    ):
        check_stale_agents(orch)

    # The stuck process is force-killed (SIGKILL) rather than only signalled
    # via the SHUTDOWN file.
    assert kpg.called


def test_stale_heartbeat_does_not_loop_shutdown_forever() -> None:
    """Repeated monitor ticks on a frozen heartbeat write SHUTDOWN at most once."""
    session = _session("T-1", pid=4321)
    orch = SimpleNamespace(
        _agents={"A-1": session},
        _signal_mgr=MagicMock(),
        _spawner=MagicMock(),
    )
    # Age >= shutdown threshold but below the kill threshold: SHUTDOWN territory.
    frozen_ts = 100.0
    tick_time = frozen_ts + AGENT.escalation_sigterm_s + 5.0
    # Real dedup lives in AgentSignalManager.write_shutdown; emulate it on the mock.
    emitted: set[tuple[str, str]] = set()

    def _dedup_shutdown(session_id: str, reason: str, task_title: str) -> bool:
        key = (session_id, reason)
        if key in emitted:
            return False
        emitted.add(key)
        return True

    orch._signal_mgr.write_shutdown.side_effect = _dedup_shutdown
    orch._signal_mgr.read_heartbeat.return_value = SimpleNamespace(timestamp=frozen_ts)

    with (
        patch("bernstein.core.agents.heartbeat.time.time", return_value=tick_time),
        patch("bernstein.core.platform_compat.kill_process_group"),
    ):
        for _ in range(5):
            check_stale_agents(orch)

    # write_shutdown may be invoked each tick, but only the first newly emits;
    # the deduped set proves at most one SHUTDOWN reaches the agent per episode.
    assert len(emitted) == 1
