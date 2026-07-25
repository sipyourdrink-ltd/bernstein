"""Unit tests for the three-tier watchdog system."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.models import AgentSession, ModelConfig, Task
from bernstein.core.watchdog import WatchdogFinding, WatchdogManager, collect_watchdog_findings


def _session(task_id: str, *, spawn_ts: float = 100.0) -> AgentSession:
    return AgentSession(
        id="sess-1",
        role="backend",
        task_ids=[task_id],
        status="working",
        spawn_ts=spawn_ts,
        model_config=ModelConfig("sonnet", "high"),
    )


def _write_heartbeat(workdir: Path, session_id: str, timestamp: float) -> None:
    hb_path = workdir / ".sdd" / "runtime" / "heartbeats" / f"{session_id}.json"
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    hb_path.write_text(json.dumps({"timestamp": timestamp}), encoding="utf-8")


def _write_log(workdir: Path, session_id: str, line_count: int) -> None:
    log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"line {idx}" for idx in range(1, line_count + 1)]
    log_path.write_text("\n".join(lines), encoding="utf-8")


def _write_heartbeat_phase(workdir: Path, session_id: str, timestamp: float, phase: str) -> None:
    hb_path = workdir / ".sdd" / "runtime" / "heartbeats" / f"{session_id}.json"
    hb_path.parent.mkdir(parents=True, exist_ok=True)
    hb_path.write_text(json.dumps({"timestamp": timestamp, "phase": phase}), encoding="utf-8")


def _age_log(workdir: Path, session_id: str, age_s: float) -> None:
    """Backdate the log file mtime so it is NOT a fresh liveness signal."""
    log_path = workdir / ".sdd" / "runtime" / f"{session_id}.log"
    old = time.time() - age_s
    os.utime(log_path, (old, old))


def _orch(
    workdir: Path,
    *,
    session: AgentSession,
    stall_count: int = 0,
    log_state: dict[str, tuple[int, int]] | None = None,
    heartbeat_timeout_s: int = 120,
    heartbeat_starting_timeout_s: int = 300,
) -> SimpleNamespace:
    task = Task(id=session.task_ids[0], title="Fix API", description="desc", role="backend")
    return SimpleNamespace(
        _workdir=workdir,
        _config=SimpleNamespace(
            heartbeat_timeout_s=heartbeat_timeout_s,
            heartbeat_starting_timeout_s=heartbeat_starting_timeout_s,
        ),
        _agents={session.id: session},
        _stall_counts={session.task_ids[0]: stall_count},
        _watchdog_log_state={} if log_state is None else dict(log_state),
        _latest_tasks_by_id={task.id: task},
    )


def _response(task_id: str) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"id": task_id}
    return resp


def test_collect_watchdog_findings_prioritizes_progress_stall(tmp_path: Path) -> None:
    workdir = tmp_path
    session = _session("task-1")
    orch = _orch(workdir, session=session, stall_count=3)
    _write_heartbeat(workdir, session.id, 90.0)
    _write_log(workdir, session.id, 5)

    with patch("bernstein.core.observability.watchdog.time.time", return_value=200.0):
        findings = collect_watchdog_findings(orch)

    assert len(findings) == 1
    assert findings[0].source == "progress_stall"
    assert findings[0].severity == "medium"


def test_collect_watchdog_findings_detects_stale_log_growth(tmp_path: Path) -> None:
    workdir = tmp_path
    session = _session("task-1")
    orch = _orch(workdir, session=session, log_state={session.id: (4, 2)})
    _write_heartbeat(workdir, session.id, 195.0)
    _write_log(workdir, session.id, 4)

    with patch("bernstein.core.observability.watchdog.time.time", return_value=200.0):
        findings = collect_watchdog_findings(orch)

    assert len(findings) == 1
    assert findings[0].source == "log_growth"
    assert findings[0].severity == "medium"
    assert orch._watchdog_log_state[session.id] == (4, 3)


def test_collect_watchdog_findings_detects_silent_agent(tmp_path: Path) -> None:
    workdir = tmp_path
    session = _session("task-1", spawn_ts=0.0)
    orch = _orch(workdir, session=session)

    with patch("bernstein.core.observability.watchdog.time.time", return_value=200.0):
        findings = collect_watchdog_findings(orch)

    assert len(findings) == 1
    assert findings[0].source == "heartbeat"
    assert findings[0].severity == "high"


# ---------------------------------------------------------------------------
# Issue #3012: a fresh log mtime is a positive liveness signal that suppresses
# the heartbeat-staleness incident (real wall-clock; no time patching so the
# log mtime and the heartbeat content timestamp share one clock).
# ---------------------------------------------------------------------------


def test_fresh_log_suppresses_stale_heartbeat_incident(tmp_path: Path) -> None:
    """Heartbeat older than the timeout, but the log was written within the
    grace window -> the agent is alive and NO heartbeat incident is raised
    (issue #3012)."""
    workdir = tmp_path
    now = time.time()
    session = _session("task-1", spawn_ts=now - 200)
    orch = _orch(workdir, session=session, heartbeat_timeout_s=120)
    # Heartbeat 140s old: past the 120s timeout, so absent a liveness signal
    # this would be a CRITICAL incident.
    _write_heartbeat(workdir, session.id, now - 140)
    _write_log(workdir, session.id, 3)  # freshly written -> mtime ~now

    findings = collect_watchdog_findings(orch)

    assert [f for f in findings if f.source == "heartbeat"] == []


def test_stale_heartbeat_with_stale_log_still_raises_critical(tmp_path: Path) -> None:
    """Control for the suppression test: same stale heartbeat, but the log
    mtime is ALSO past the grace window -> the critical incident still fires,
    proving the suppression is driven by log freshness, not disabled."""
    workdir = tmp_path
    now = time.time()
    session = _session("task-1", spawn_ts=now - 200)
    orch = _orch(workdir, session=session, heartbeat_timeout_s=120)
    _write_heartbeat(workdir, session.id, now - 140)
    _write_log(workdir, session.id, 3)
    _age_log(workdir, session.id, age_s=300)  # log not fresh

    findings = collect_watchdog_findings(orch)

    heartbeat = [f for f in findings if f.source == "heartbeat"]
    assert len(heartbeat) == 1
    assert heartbeat[0].severity == "critical"


def test_starting_phase_uses_configurable_larger_timeout(tmp_path: Path) -> None:
    """A `starting` agent is judged against the larger, configurable
    starting-phase timeout, so a heartbeat age past the normal 120s cap but
    below the starting window raises no incident (issue #3012)."""
    workdir = tmp_path
    now = time.time()
    session = _session("task-1", spawn_ts=now - 400)
    orch = _orch(
        workdir,
        session=session,
        heartbeat_timeout_s=120,
        heartbeat_starting_timeout_s=300,
    )
    # Heartbeat 140s old with phase=starting: past the 120s cap (would be
    # critical) but below the 300s starting window and its 150s high-water mark.
    _write_heartbeat_phase(workdir, session.id, now - 140, phase="starting")
    _write_log(workdir, session.id, 3)
    _age_log(workdir, session.id, age_s=300)  # stale log: isolate the phase-timeout effect

    findings = collect_watchdog_findings(orch)

    assert [f for f in findings if f.source == "heartbeat"] == []


def test_starting_phase_still_flags_once_past_starting_timeout(tmp_path: Path) -> None:
    """Beyond the starting-phase window a frozen `starting` heartbeat with no
    log activity is still flagged critical -- the larger timeout is a grace
    window, not a permanent exemption."""
    workdir = tmp_path
    now = time.time()
    session = _session("task-1", spawn_ts=now - 600)
    orch = _orch(
        workdir,
        session=session,
        heartbeat_timeout_s=120,
        heartbeat_starting_timeout_s=300,
    )
    _write_heartbeat_phase(workdir, session.id, now - 360, phase="starting")  # past 300s
    _write_log(workdir, session.id, 3)
    _age_log(workdir, session.id, age_s=300)  # not fresh

    findings = collect_watchdog_findings(orch)

    heartbeat = [f for f in findings if f.source == "heartbeat"]
    assert len(heartbeat) == 1
    assert heartbeat[0].severity == "critical"


def test_watchdog_manager_creates_one_triage_task_for_active_incident(tmp_path: Path) -> None:
    client = MagicMock()
    client.post.return_value = _response("triage-1")
    manager = WatchdogManager(tmp_path, client, "http://server")
    finding = WatchdogFinding(
        key="progress_stall:sess-1:task-1",
        session_id="sess-1",
        task_id="task-1",
        source="progress_stall",
        severity="medium",
        summary="Agent stalled on task task-1",
        detail="Three identical snapshots.",
    )

    with patch("bernstein.core.observability.watchdog.time.time", return_value=100.0):
        manager.sync([finding])
        manager.sync([finding])

    assert client.post.call_count == 1
    state_path = tmp_path / ".sdd" / "runtime" / "watchdog_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state[finding.key]["count"] == 2
    assert state[finding.key]["triage_task_id"] == "triage-1"


def test_watchdog_manager_escalates_repeated_high_severity_incident(tmp_path: Path) -> None:
    client = MagicMock()
    client.post.return_value = _response("triage-2")
    notifications: list[dict[str, object]] = []
    bulletins: list[tuple[str, str]] = []
    manager = WatchdogManager(
        tmp_path,
        client,
        "http://server",
        notify=lambda event, title, body, **metadata: notifications.append(  # type: ignore[misc]
            {"event": event, "title": title, "body": body, "metadata": metadata}
        ),
        post_bulletin=lambda kind, body: bulletins.append((kind, body)),
    )
    finding = WatchdogFinding(
        key="heartbeat:sess-1:task-1",
        session_id="sess-1",
        task_id="task-1",
        source="heartbeat",
        severity="high",
        summary="Heartbeat stale for task task-1",
        detail="Heartbeat age exceeded the wakeup threshold.",
    )

    with patch("bernstein.core.observability.watchdog.time.time", return_value=200.0):
        manager.sync([finding])
        manager.sync([finding])

    assert client.post.call_count == 1
    assert len(notifications) == 1
    assert notifications[0]["event"] == "approval.needed"
    assert len(bulletins) == 1
    assert bulletins[0][0] == "alert"


def test_watchdog_manager_logs_detection_and_escalation(tmp_path: Path, caplog) -> None:
    """Logging gap: WatchdogManager.sync() persisted new-incident and
    human-escalation events to JSONL, but never logged them -- an operator
    tailing the process log saw nothing until the triage-task-created line,
    which only fires when the triage HTTP call succeeds. Assert the TIER1
    detected line fires on first sight, and the TIER3 escalation line fires
    once the incident crosses its threshold."""
    caplog.set_level("WARNING", logger="bernstein.core.observability.watchdog")
    client = MagicMock()
    client.post.return_value = _response("triage-2")
    manager = WatchdogManager(tmp_path, client, "http://server")
    finding = WatchdogFinding(
        key="heartbeat:sess-1:task-1",
        session_id="sess-1",
        task_id="task-1",
        source="heartbeat",
        severity="high",
        summary="Heartbeat stale for task task-1",
        detail="Heartbeat age exceeded the wakeup threshold.",
    )

    with patch("bernstein.core.observability.watchdog.time.time", return_value=200.0):
        manager.sync([finding])
        manager.sync([finding])

    messages = [r.message for r in caplog.records]
    assert any("watchdog TIER1 detected" in m and "heartbeat:sess-1:task-1" in m for m in messages), messages
    assert any("watchdog TIER3 escalating to human" in m and "heartbeat:sess-1:task-1" in m for m in messages), messages


def test_watchdog_manager_refuses_triage_of_triage(tmp_path: Path) -> None:
    """Regression test for the 2026-07-02 production incident: a watchdog
    triage task that itself stalls must NOT spawn a second triage task about
    it ("Watchdog triage of watchdog triage"). See
    work/agent-reports/2026-07-02-run9-attempt9-audit.md.
    """
    client = MagicMock()
    client.post.return_value = _response("triage-3")
    manager = WatchdogManager(tmp_path, client, "http://server")

    # The finding's task_title is itself an existing "Watchdog triage: ..."
    # meta-task -- i.e. this finding is ABOUT a previously auto-spawned
    # triage task, which would make a new triage task depth 2.
    finding = WatchdogFinding(
        key="heartbeat:sess-2:triage-task-1",
        session_id="sess-2",
        task_id="triage-task-1",
        source="heartbeat",
        severity="high",
        summary="Heartbeat stale for task Watchdog triage: Heartbeat stale for task X",
        detail="Heartbeat age exceeded the wakeup threshold.",
        task_title="Watchdog triage: Heartbeat stale for task X",
    )

    with patch("bernstein.core.observability.watchdog.time.time", return_value=300.0):
        manager.sync([finding])

    assert client.post.call_count == 0
    state_path = tmp_path / ".sdd" / "runtime" / "watchdog_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state[finding.key]["triage_task_id"] is None


def test_watchdog_manager_dedupes_triage_tasks_across_incidents(tmp_path: Path) -> None:
    """Two distinct incidents that would create identically-worded triage
    tasks must only spawn one."""
    client = MagicMock()
    client.post.return_value = _response("triage-4")
    manager = WatchdogManager(tmp_path, client, "http://server")

    finding_a = WatchdogFinding(
        key="heartbeat:sess-a:task-a",
        session_id="sess-a",
        task_id="task-a",
        source="heartbeat",
        severity="high",
        summary="Heartbeat stale for task Fix API",
        detail="detail-a",
        task_title="Fix API",
    )
    finding_b = WatchdogFinding(
        key="heartbeat:sess-b:task-b",
        session_id="sess-b",
        task_id="task-b",
        source="heartbeat",
        severity="high",
        summary="Heartbeat stale for task Fix API",
        detail="detail-b",
        task_title="Fix API",
    )

    with patch("bernstein.core.observability.watchdog.time.time", return_value=300.0):
        manager.sync([finding_a])
        manager.sync([finding_b])

    assert client.post.call_count == 1


def test_watchdog_manager_dedupes_triage_tasks_within_single_sync_pass(tmp_path: Path) -> None:
    """Same-pass dedupe: two distinct incidents arriving in ONE sync() call
    must still only spawn one identically-worded triage task. Incidents
    created earlier in the same pass live only in the in-memory ``active``
    dict (state is persisted once, after the loop), so a dedupe scan of
    ``state.values()`` alone missed them."""
    client = MagicMock()
    client.post.return_value = _response("triage-5")
    manager = WatchdogManager(tmp_path, client, "http://server")

    finding_a = WatchdogFinding(
        key="heartbeat:sess-a:task-a",
        session_id="sess-a",
        task_id="task-a",
        source="heartbeat",
        severity="high",
        summary="Heartbeat stale for task Fix API",
        detail="detail-a",
        task_title="Fix API",
    )
    finding_b = WatchdogFinding(
        key="heartbeat:sess-b:task-b",
        session_id="sess-b",
        task_id="task-b",
        source="heartbeat",
        severity="high",
        summary="Heartbeat stale for task Fix API",
        detail="detail-b",
        task_title="Fix API",
    )

    with patch("bernstein.core.observability.watchdog.time.time", return_value=300.0):
        manager.sync([finding_a, finding_b])

    assert client.post.call_count == 1
