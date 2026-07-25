"""Issue #3012: heartbeat escalation must consult log/git liveness before it kills.

The Tier-1 heartbeat escalation ladder used to SIGTERM/SIGKILL an agent purely
on heartbeat age. Adapters that emit no heartbeats (``consumes_heartbeat_dir=
False``) have only their spawn-time heartbeat on disk, so heartbeat age is just
wall-clock since spawn -- and a slow/free model streaming its first turn was
reaped mid-work even though its log had just been written. The reap-cycle's
``liveness_judgment`` correctly consults log/git freshness, but it ran one tick
AFTER the SIGTERM. These tests drive the real ``check_stale_agents`` path and
assert the escalation now applies the same log/git-freshness check BEFORE the
kill.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.heartbeat import check_stale_agents
from bernstein.core.models import AgentHeartbeat, AgentSession, ModelConfig

from bernstein.core.agents.agent_signals import AgentSignalManager


def _session(*, pid: int | None = 999_999, spawn_ts: float) -> AgentSession:
    return AgentSession(
        id="mgr-1",
        role="manager",
        task_ids=["T-1"],
        status="working",
        spawn_ts=spawn_ts,
        pid=pid,
        model_config=ModelConfig("sonnet", "high"),
    )


def _orch(workdir: Path, session: AgentSession, *, timeout_s: float = 120.0) -> SimpleNamespace:
    return SimpleNamespace(
        _agents={session.id: session},
        _signal_mgr=AgentSignalManager(workdir),
        _spawner=MagicMock(),
        _workdir=workdir,
        _config=SimpleNamespace(heartbeat_enabled=True, heartbeat_timeout_s=timeout_s),
    )


def _write_log(workdir: Path, session_id: str, *, mtime: float | None = None) -> Path:
    log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("thinking... (a long first turn is still streaming)\n", encoding="utf-8")
    if mtime is not None:
        os.utime(log_path, (mtime, mtime))
    return log_path


def test_fresh_log_prevents_heartbeat_sigterm(tmp_path: Path) -> None:
    """Stale heartbeat + a log written within the grace window -> the agent is
    alive and NO SIGTERM/SIGKILL is sent (issue #3012)."""
    now = time.time()
    session = _session(spawn_ts=now - 200)
    orch = _orch(tmp_path, session, timeout_s=120.0)
    # Heartbeat frozen 165s ago (past the 120s timeout AND the 150s SIGKILL
    # threshold) -- absent a liveness signal this agent would be force-killed.
    orch._signal_mgr.write_heartbeat(session.id, AgentHeartbeat(timestamp=now - 165, status="starting"))
    _write_log(tmp_path, session.id)  # freshly written -> mtime ~now

    with patch("bernstein.core.platform_compat.kill_process_group") as kpg:
        check_stale_agents(orch)

    assert not kpg.called, "a still-writing agent must not be killed on heartbeat age alone"
    orch._spawner.kill.assert_not_called()
    # The soft SHUTDOWN signal must also be withheld from a live agent.
    shutdown_file = tmp_path / ".sdd" / "runtime" / "signals" / session.id / "SHUTDOWN"
    assert not shutdown_file.exists()
    # Ladder was reset so a genuine later stall re-escalates from the start.
    ladder = orch._heartbeat_escalation_ladder
    state = ladder.get_state(session.id)
    assert state is None or state.highest_tier.name == "NONE"


def test_stale_log_still_allows_heartbeat_kill(tmp_path: Path) -> None:
    """Control: same stale heartbeat, but the log mtime is ALSO past the grace
    window -> the agent is genuinely stuck and the ladder still kills it. This
    proves the gate is driven by log freshness, not a blanket disable (and
    keeps issue #2796's frozen-heartbeat reaping intact)."""
    now = time.time()
    session = _session(spawn_ts=now - 300)
    orch = _orch(tmp_path, session, timeout_s=120.0)
    orch._signal_mgr.write_heartbeat(session.id, AgentHeartbeat(timestamp=now - 165, status="starting"))
    _write_log(tmp_path, session.id, mtime=now - 300)  # log not fresh either

    with patch("bernstein.core.platform_compat.kill_process_group") as kpg:
        check_stale_agents(orch)

    assert kpg.called, "a frozen agent with no log/git activity must still be killed"
    assert kpg.call_args.args[0] == 999_999


def test_no_log_at_all_still_allows_heartbeat_kill(tmp_path: Path) -> None:
    """A frozen heartbeat with no log file at all (nothing ever written) has no
    fresh signal and is escalated -- the gate never masks a truly dead agent."""
    now = time.time()
    session = _session(spawn_ts=now - 300)
    orch = _orch(tmp_path, session, timeout_s=120.0)
    orch._signal_mgr.write_heartbeat(session.id, AgentHeartbeat(timestamp=now - 165, status="starting"))
    # No log file written.

    with patch("bernstein.core.platform_compat.kill_process_group") as kpg:
        check_stale_agents(orch)

    assert kpg.called
