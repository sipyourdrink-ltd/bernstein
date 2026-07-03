"""Behavioral tests for ``heartbeat`` monitoring and stall escalation.

Exercises the adaptive stall-profile selector, the heartbeat file reader
(primary + fallback locations, malformed input), heartbeat-instruction
injection, idle detection by log activity, and the staleness / stall
escalation ladders driven by a duck-typed orchestrator fake. The fake
mirrors the ``SimpleNamespace`` pattern used by the existing heartbeat
tests so the real escalation code runs end to end.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.models import AgentSession, ModelConfig, ProgressSnapshot

from bernstein.core.agents.agent_log_aggregator import AgentLogSummary
from bernstein.core.agents.heartbeat import (
    HeartbeatMonitor,
    HeartbeatStatus,
    check_stale_agents,
    check_stalled_tasks,
    compute_stall_profile,
    detect_idle_agents,
)


def _session(sid: str = "A-1", status: str = "working", task_id: str = "T-1") -> AgentSession:
    return AgentSession(
        id=sid,
        role="backend",
        task_ids=[task_id],
        status=status,
        spawn_ts=100.0,
        model_config=ModelConfig("sonnet", "high"),
    )


def _status(*, phase: str = "", last: datetime | None = None) -> HeartbeatStatus:
    return HeartbeatStatus(
        session_id="S",
        last_heartbeat=last,
        age_seconds=1.0,
        phase=phase,
        progress_pct=0,
        is_alive=True,
        is_stale=False,
    )


def _log_summary(*, rate_limit_hits: int = 0, last_activity_line: int = 5) -> AgentLogSummary:
    return AgentLogSummary(
        session_id="S",
        total_lines=10,
        events=[],
        error_count=0,
        warning_count=0,
        files_modified=[],
        tests_run=False,
        tests_passed=False,
        test_summary="",
        rate_limit_hits=rate_limit_hits,
        compile_errors=0,
        tool_failures=0,
        first_meaningful_action_line=1,
        last_activity_line=last_activity_line,
        dominant_failure_category=None,
    )


# ---------------------------------------------------------------------------
# compute_stall_profile
# ---------------------------------------------------------------------------


def test_stall_profile_testing_phase_is_most_lenient() -> None:
    """A testing-phase heartbeat gets the widest thresholds (8/12/16)."""
    profile = compute_stall_profile(None, _status(phase="testing"), None)
    assert (profile.wakeup_threshold, profile.shutdown_threshold, profile.kill_threshold) == (8, 12, 16)
    assert "testing" in profile.reason


def test_stall_profile_rate_limit_widens_thresholds() -> None:
    """Recent rate-limit hits widen thresholds to 6/10/14."""
    profile = compute_stall_profile(None, _status(phase="implementing"), _log_summary(rate_limit_hits=2))
    assert (profile.wakeup_threshold, profile.shutdown_threshold, profile.kill_threshold) == (6, 10, 14)
    assert "rate-limit" in profile.reason


def test_stall_profile_no_heartbeat_no_log_is_strictest() -> None:
    """No heartbeat and no log activity yields the tightest 2/3/5 profile."""
    profile = compute_stall_profile(None, _status(last=None), _log_summary(last_activity_line=0))
    assert (profile.wakeup_threshold, profile.shutdown_threshold, profile.kill_threshold) == (2, 3, 5)
    assert "no heartbeat" in profile.reason


def test_stall_profile_large_task_gets_extra_runway(make_task: object) -> None:
    """A large-scope task gets a 5/8/12 profile when a heartbeat exists."""
    from bernstein.core.models import Scope

    task = make_task(scope=Scope.LARGE)  # type: ignore[operator]
    profile = compute_stall_profile(task, _status(phase="implementing", last=datetime.now(UTC)), None)
    assert (profile.wakeup_threshold, profile.shutdown_threshold, profile.kill_threshold) == (5, 8, 12)
    assert "large" in profile.reason


def test_stall_profile_default_when_unremarkable() -> None:
    """An ordinary medium task with a live heartbeat gets the 3/5/7 default."""
    profile = compute_stall_profile(None, _status(phase="implementing", last=datetime.now(UTC)), _log_summary())
    assert (profile.wakeup_threshold, profile.shutdown_threshold, profile.kill_threshold) == (3, 5, 7)
    assert profile.reason == "default profile"


# ---------------------------------------------------------------------------
# HeartbeatMonitor.check
# ---------------------------------------------------------------------------


def test_check_no_heartbeat_reports_dead(tmp_path: Path) -> None:
    """A session with no heartbeat file is neither alive nor stale."""
    status = HeartbeatMonitor(tmp_path, timeout_s=60.0).check("UNKNOWN")
    assert status.is_alive is False
    assert status.is_stale is False
    assert status.last_heartbeat is None


def test_check_fresh_heartbeat_is_alive_and_clamps_progress(tmp_path: Path) -> None:
    """A recent heartbeat is alive; out-of-range progress clamps to 100."""
    fb = tmp_path / ".sdd" / "runtime" / "signals" / "S9" / "HEARTBEAT"
    fb.parent.mkdir(parents=True)
    fb.write_text(json.dumps({"timestamp": time.time(), "phase": "impl", "progress_pct": 150}))
    status = HeartbeatMonitor(tmp_path, timeout_s=60.0).check("S9")
    assert status.is_alive is True
    assert status.is_stale is False
    assert status.progress_pct == 100


def test_check_stale_heartbeat_flags_stale(tmp_path: Path) -> None:
    """A heartbeat older than the timeout is stale, not alive."""
    fb = tmp_path / ".sdd" / "runtime" / "signals" / "S9" / "HEARTBEAT"
    fb.parent.mkdir(parents=True)
    fb.write_text(json.dumps({"timestamp": time.time() - 120, "phase": "x", "progress_pct": 50}))
    status = HeartbeatMonitor(tmp_path, timeout_s=60.0).check("S9")
    assert status.is_alive is False
    assert status.is_stale is True


def test_check_iso_timestamp_parsed(tmp_path: Path) -> None:
    """An ISO-8601 timestamp string in the fallback file is parsed."""
    fb = tmp_path / ".sdd" / "runtime" / "signals" / "ISO" / "HEARTBEAT"
    fb.parent.mkdir(parents=True)
    iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    fb.write_text(json.dumps({"timestamp": iso, "phase": "impl"}))
    assert HeartbeatMonitor(tmp_path, timeout_s=60.0).check("ISO").is_alive is True


def test_check_malformed_json_returns_dead(tmp_path: Path) -> None:
    """A corrupt heartbeat file is treated as no heartbeat."""
    fb = tmp_path / ".sdd" / "runtime" / "signals" / "BAD" / "HEARTBEAT"
    fb.parent.mkdir(parents=True)
    fb.write_text("{not valid json")
    status = HeartbeatMonitor(tmp_path, timeout_s=60.0).check("BAD")
    assert status.is_alive is False
    assert status.last_heartbeat is None


def test_check_unparseable_timestamp_returns_dead(tmp_path: Path) -> None:
    """A non-numeric, non-ISO timestamp yields no heartbeat."""
    fb = tmp_path / ".sdd" / "runtime" / "signals" / "BADTS" / "HEARTBEAT"
    fb.parent.mkdir(parents=True)
    fb.write_text(json.dumps({"timestamp": "not-a-date", "phase": "x"}))
    assert HeartbeatMonitor(tmp_path, timeout_s=60.0).check("BADTS").last_heartbeat is None


def test_check_all_preserves_order(tmp_path: Path) -> None:
    """check_all returns one status per id, in input order."""
    statuses = HeartbeatMonitor(tmp_path, timeout_s=60.0).check_all(["X", "Y", "Z"])
    assert [s.session_id for s in statuses] == ["X", "Y", "Z"]


def test_inject_heartbeat_instructions_targets_session_file(tmp_path: Path) -> None:
    """The injected shell snippet writes to the session's heartbeat path."""
    snippet = HeartbeatMonitor(tmp_path, timeout_s=60.0).inject_heartbeat_instructions("SID")
    assert "mkdir -p" in snippet
    assert "SID.json" in snippet
    assert "sleep 15" in snippet


# ---------------------------------------------------------------------------
# detect_idle_agents
# ---------------------------------------------------------------------------


def test_detect_idle_agents_flags_quiet_logs(tmp_path: Path) -> None:
    """An agent with a near-empty log is flagged idle; a busy one is not."""
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "IDLE.log").write_text("line1\nline2\n")
    (runtime / "BUSY.log").write_text("\n".join(f"l{i}" for i in range(20)))
    agents = {
        "IDLE": SimpleNamespace(status="working"),
        "BUSY": SimpleNamespace(status="working"),
    }
    assert detect_idle_agents(tmp_path, agents) == ["IDLE"]


def test_detect_idle_agents_skips_dead(tmp_path: Path) -> None:
    """Dead agents are never flagged idle even with empty logs."""
    (tmp_path / ".sdd" / "runtime").mkdir(parents=True)
    agents = {"DEAD": SimpleNamespace(status="dead")}
    assert detect_idle_agents(tmp_path, agents) == []


# ---------------------------------------------------------------------------
# check_stale_agents - escalation ladder
# ---------------------------------------------------------------------------


def test_check_stale_agents_disabled_is_noop() -> None:
    """When heartbeat_enabled is False no signals are written."""
    session = _session()
    orch = SimpleNamespace(
        _agents={"A-1": session},
        _signal_mgr=MagicMock(),
        _config=SimpleNamespace(heartbeat_enabled=False),
    )
    check_stale_agents(orch)
    orch._signal_mgr.write_wakeup.assert_not_called()
    orch._signal_mgr.write_shutdown.assert_not_called()


def test_check_stale_agents_simple_path_shutdown() -> None:
    """Without a workdir the simple path still escalates a very stale agent."""
    session = _session()
    orch = SimpleNamespace(
        _agents={"A-1": session},
        _signal_mgr=MagicMock(),
        _config=SimpleNamespace(),  # no _workdir -> simple fallback
    )
    orch._signal_mgr.read_heartbeat.return_value = SimpleNamespace(timestamp=70.0)
    with patch("bernstein.core.agents.heartbeat.time.time", return_value=200.0):
        check_stale_agents(orch)
    orch._signal_mgr.write_shutdown.assert_called_once()


def test_check_stale_agents_skips_dead_session() -> None:
    """A dead session is skipped by the stale-agent check."""
    session = _session(status="dead")
    orch = SimpleNamespace(
        _agents={"A-1": session},
        _signal_mgr=MagicMock(),
        _config=SimpleNamespace(),
    )
    orch._signal_mgr.read_heartbeat.return_value = SimpleNamespace(timestamp=70.0)
    with patch("bernstein.core.agents.heartbeat.time.time", return_value=200.0):
        check_stale_agents(orch)
    orch._signal_mgr.write_shutdown.assert_not_called()


# ---------------------------------------------------------------------------
# check_stalled_tasks - profiled escalation (workdir present)
# ---------------------------------------------------------------------------


def _stall_orch(tmp_path: Path, start_count: int) -> SimpleNamespace:
    session = _session()
    snap = {"timestamp": 10.0, "files_changed": 1, "tests_passing": 2, "errors": 0, "last_file": "a.py"}
    orch = SimpleNamespace(
        _agents={"A-1": session},
        _workdir=tmp_path,
        _config=SimpleNamespace(server_url="http://srv", heartbeat_timeout_s=60.0),
        _client=MagicMock(),
        _last_snapshot_ts={},
        _last_snapshot={
            "T-1": ProgressSnapshot(timestamp=9.0, files_changed=1, tests_passing=2, errors=0, last_file="a.py")
        },
        _stall_counts={"T-1": start_count},
        _signal_mgr=MagicMock(),
        _spawner=MagicMock(),
    )
    orch._client.get.return_value.json.return_value = [snap]
    orch._client.get.return_value.raise_for_status.return_value = None
    return orch


def test_stalled_tasks_profiled_wakeup(tmp_path: Path) -> None:
    """With no heartbeat/log the wakeup threshold (2) sends a WAKEUP."""
    orch = _stall_orch(tmp_path, start_count=1)  # next count -> 2
    with patch("bernstein.core.agents.heartbeat.time.time", return_value=300.0):
        check_stalled_tasks(orch)
    orch._signal_mgr.write_wakeup.assert_called_once()
    orch._signal_mgr.write_shutdown.assert_not_called()
    orch._spawner.kill.assert_not_called()


def test_stalled_tasks_profiled_shutdown(tmp_path: Path) -> None:
    """The shutdown threshold (3) for the strict profile sends SHUTDOWN."""
    orch = _stall_orch(tmp_path, start_count=2)  # next count -> 3
    with patch("bernstein.core.agents.heartbeat.time.time", return_value=300.0):
        check_stalled_tasks(orch)
    orch._signal_mgr.write_shutdown.assert_called_once()
    orch._spawner.kill.assert_not_called()


def test_stalled_tasks_profiled_kill_and_reset(tmp_path: Path) -> None:
    """The kill threshold (5) kills the agent and resets the stall count."""
    orch = _stall_orch(tmp_path, start_count=4)  # next count -> 5
    with patch("bernstein.core.agents.heartbeat.time.time", return_value=300.0):
        check_stalled_tasks(orch)
    orch._spawner.kill.assert_called_once()
    assert orch._stall_counts["T-1"] == 0


def test_stalled_tasks_alive_resets_stall_count(tmp_path: Path) -> None:
    """A live heartbeat resets the stall count and skips escalation."""
    orch = _stall_orch(tmp_path, start_count=4)
    # Write a fresh heartbeat so is_alive is True.
    fb = tmp_path / ".sdd" / "runtime" / "signals" / "A-1" / "HEARTBEAT"
    fb.parent.mkdir(parents=True)
    fb.write_text(json.dumps({"timestamp": 299.5, "phase": "impl"}))
    with patch("bernstein.core.agents.heartbeat.time.time", return_value=300.0):
        check_stalled_tasks(orch)
    orch._spawner.kill.assert_not_called()
    assert orch._stall_counts["T-1"] == 0


def test_stalled_tasks_no_new_snapshot_is_noop(tmp_path: Path) -> None:
    """A snapshot not newer than the last seen one triggers no escalation."""
    orch = _stall_orch(tmp_path, start_count=4)
    # Mark the snapshot timestamp as already seen.
    orch._last_snapshot_ts = {"T-1": 10.0}
    with patch("bernstein.core.agents.heartbeat.time.time", return_value=300.0):
        check_stalled_tasks(orch)
    orch._spawner.kill.assert_not_called()


# ---------------------------------------------------------------------------
# Defect-10: leaked heartbeat shell loop -- self-termination + reap
# ---------------------------------------------------------------------------


def test_inject_heartbeat_instructions_is_self_terminating(tmp_path: Path) -> None:
    """The generated loop checks a STOP marker and records its own pid."""
    monitor = HeartbeatMonitor(tmp_path)
    snippet = monitor.inject_heartbeat_instructions("SID-1")

    assert "SID-1.stop" in snippet
    assert "SID-1.pid" in snippet
    assert "while [ ! -f" in snippet  # self-terminating condition, not `while true`
    assert "while true" not in snippet
    assert "echo $!" in snippet  # records loop pid for belt-and-braces reap


def test_heartbeat_loop_actually_self_terminates_on_stop_marker(tmp_path: Path) -> None:
    """End-to-end: run the real generated snippet, request stop, prove no leaked process.

    This is the direct regression test for the observed defect: a bernstein
    run left a heartbeat shell loop running on the HOST after the run ended.
    """
    monitor = HeartbeatMonitor(tmp_path)
    snippet = monitor.inject_heartbeat_instructions("SID-2")

    # Run the exact generated snippet the way an agent shell would.
    subprocess.run(["/bin/sh", "-c", snippet], check=True, timeout=10)

    pid_path = tmp_path / ".sdd" / "runtime" / "heartbeats" / "SID-2.pid"
    heartbeat_path = tmp_path / ".sdd" / "runtime" / "heartbeats" / "SID-2.json"

    # Loop should have started and written at least one heartbeat + its pidfile.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (pid_path.exists() and heartbeat_path.exists()):
        time.sleep(0.1)
    assert pid_path.exists(), "heartbeat loop never wrote its pidfile"
    pid = int(pid_path.read_text(encoding="utf-8").strip())

    def _alive(p: int) -> bool:
        try:
            os.kill(p, 0)
        except ProcessLookupError:
            return False
        else:
            return True

    assert _alive(pid), "loop pid should still be running before STOP is requested"

    # This is the fix under test: request stop instead of leaving it running forever.
    monitor.request_heartbeat_stop("SID-2")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.2)

    assert not _alive(pid), "heartbeat loop leaked past its STOP marker -- defect-10 regression"


def _spawn_fake_heartbeat_loop(tmp_path: Path, session_id: str) -> subprocess.Popen[bytes]:
    """Spawn a long-lived process whose command line looks like this session's
    heartbeat loop (it references the session's stop-marker path), so the reap
    identity check recognizes it as the real loop."""
    stop_path = tmp_path / ".sdd" / "runtime" / "heartbeats" / f"{session_id}.stop"
    return subprocess.Popen(
        ["sh", "-c", f"while [ ! -f '{stop_path}' ]; do sleep 1; done"],
    )


def test_reap_heartbeat_loop_kills_recorded_pid(tmp_path: Path) -> None:
    """reap_heartbeat_loop is a belt-and-braces kill-by-pid, safe when nothing was spawned."""
    monitor = HeartbeatMonitor(tmp_path)

    # No heartbeat loop ever spawned for this session: must be a safe no-op.
    monitor.reap_heartbeat_loop("NEVER-SPAWNED", reason="test_noop")

    # Simulate an orphaned loop by recording a real long-lived process's pid
    # whose command line identifies it as this session's heartbeat loop.
    proc = _spawn_fake_heartbeat_loop(tmp_path, "SID-3")
    pid_path = tmp_path / ".sdd" / "runtime" / "heartbeats" / "SID-3.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid), encoding="utf-8")

    monitor.reap_heartbeat_loop("SID-3", reason="test_kill")
    proc.wait(timeout=5)

    assert proc.returncode is not None
    assert not pid_path.exists()
    stop_path = tmp_path / ".sdd" / "runtime" / "heartbeats" / "SID-3.stop"
    assert stop_path.exists()


def test_reap_heartbeat_loop_spares_recycled_foreign_pid(tmp_path: Path) -> None:
    """A stale pidfile whose PID was recycled to an unrelated process must NOT
    be signalled: the reap verifies the process still looks like this session's
    heartbeat loop before killing it."""
    monitor = HeartbeatMonitor(tmp_path)

    # A foreign process that has nothing to do with this session's heartbeat.
    proc = subprocess.Popen(["sleep", "30"])
    try:
        pid_path = tmp_path / ".sdd" / "runtime" / "heartbeats" / "SID-FOREIGN.pid"
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(proc.pid), encoding="utf-8")

        monitor.reap_heartbeat_loop("SID-FOREIGN", reason="test_recycled")

        # The foreign process must still be alive (not signalled).
        assert proc.poll() is None, "reap killed a recycled/unrelated PID"
        # Stale pidfile is cleaned up even though we did not kill the process.
        assert not pid_path.exists()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_stall_kill_reaps_heartbeat_loop(tmp_path: Path) -> None:
    """Killing a stalled agent also reaps any heartbeat loop it spawned (belt-and-braces)."""
    orch = _stall_orch(tmp_path, start_count=4)  # next count -> 5 -> kill

    # Simulate a heartbeat loop the killed session had spawned (command line
    # identifies it as A-1's heartbeat loop so the reap identity check matches).
    proc = _spawn_fake_heartbeat_loop(tmp_path, "A-1")
    pid_path = tmp_path / ".sdd" / "runtime" / "heartbeats" / "A-1.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(proc.pid), encoding="utf-8")

    with patch("bernstein.core.agents.heartbeat.time.time", return_value=300.0):
        check_stalled_tasks(orch)

    orch._spawner.kill.assert_called_once()
    proc.wait(timeout=5)
    assert proc.returncode is not None, "heartbeat loop for the killed session was not reaped"
    orch._signal_mgr.write_wakeup.assert_not_called()
