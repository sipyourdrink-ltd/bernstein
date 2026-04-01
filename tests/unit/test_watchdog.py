"""Unit tests for the three-tier watchdog system."""

from __future__ import annotations

import json
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


def _orch(
    workdir: Path,
    *,
    session: AgentSession,
    stall_count: int = 0,
    log_state: dict[str, tuple[int, int]] | None = None,
) -> SimpleNamespace:
    task = Task(id=session.task_ids[0], title="Fix API", description="desc", role="backend")
    return SimpleNamespace(
        _workdir=workdir,
        _config=SimpleNamespace(heartbeat_timeout_s=120),
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

    with patch("bernstein.core.watchdog.time.time", return_value=200.0):
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

    with patch("bernstein.core.watchdog.time.time", return_value=200.0):
        findings = collect_watchdog_findings(orch)

    assert len(findings) == 1
    assert findings[0].source == "log_growth"
    assert findings[0].severity == "medium"
    assert orch._watchdog_log_state[session.id] == (4, 3)


def test_collect_watchdog_findings_detects_silent_agent(tmp_path: Path) -> None:
    workdir = tmp_path
    session = _session("task-1", spawn_ts=0.0)
    orch = _orch(workdir, session=session)

    with patch("bernstein.core.watchdog.time.time", return_value=200.0):
        findings = collect_watchdog_findings(orch)

    assert len(findings) == 1
    assert findings[0].source == "heartbeat"
    assert findings[0].severity == "high"


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

    with patch("bernstein.core.watchdog.time.time", return_value=100.0):
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

    with patch("bernstein.core.watchdog.time.time", return_value=200.0):
        manager.sync([finding])
        manager.sync([finding])

    assert client.post.call_count == 1
    assert len(notifications) == 1
    assert notifications[0]["event"] == "approval.needed"
    assert len(bulletins) == 1
    assert bulletins[0][0] == "alert"
