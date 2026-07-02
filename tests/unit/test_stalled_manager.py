"""Unit tests for the stalled-manager detector (#1261)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from bernstein.core.models import AgentSession, ModelConfig, Task

from bernstein.core.orchestration.stalled_manager import (
    REMEDIATION_DOC,
    STALL_THRESHOLD_S,
    StalledManagerDiagnostic,
    _redact_env,
    build_diagnostic,
    detect_stalled_manager,
    handle_stalled_manager,
)


def _manager_session(*, spawn_ts: float, task_id: str = "manager-task-1") -> AgentSession:
    return AgentSession(
        id="manager-abc12345",
        role="manager",
        task_ids=[task_id],
        status="working",
        spawn_ts=spawn_ts,
        model_config=ModelConfig("opus", "max"),
    )


def _build_orch(
    workdir: Path,
    *,
    session: AgentSession,
    extra_tasks: list[str] | None = None,
    manager_env: dict[str, str] | None = None,
) -> SimpleNamespace:
    task_ids = [session.task_ids[0], *(extra_tasks or [])]
    latest_tasks = {
        tid: Task(
            id=tid,
            title="Plan and decompose goal into tasks" if tid == session.task_ids[0] else f"Child {tid}",
            description="",
            role="manager" if tid == session.task_ids[0] else "backend",
        )
        for tid in task_ids
    }
    bulletins: list[tuple[str, str]] = []

    def _post_bulletin(kind: str, body: str) -> None:
        bulletins.append((kind, body))

    orch = SimpleNamespace(
        _workdir=workdir,
        _agents={session.id: session},
        _latest_tasks_by_id=latest_tasks,
        _config=SimpleNamespace(stalled_manager_threshold_s=STALL_THRESHOLD_S),
        _manager_env_snapshot=manager_env or {},
        _running=True,
        _post_bulletin=_post_bulletin,
    )
    # Expose bulletins for assertion convenience.
    orch.bulletins = bulletins  # type: ignore[attr-defined]
    return orch


def _write_hook_events(workdir: Path, session_id: str, events: list[dict[str, Any]]) -> None:
    path = workdir / ".sdd" / "runtime" / "hooks" / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# detect_stalled_manager
# ---------------------------------------------------------------------------


def test_detect_returns_none_when_manager_under_threshold(tmp_path: Path) -> None:
    now = 1_000.0
    session = _manager_session(spawn_ts=now - 30.0)  # 30s alive
    orch = _build_orch(tmp_path, session=session)
    with patch("bernstein.core.orchestration.stalled_manager.time.time", return_value=now):
        assert detect_stalled_manager(orch) is None


def test_detect_returns_none_when_child_tasks_exist(tmp_path: Path) -> None:
    now = 1_000.0
    session = _manager_session(spawn_ts=now - (STALL_THRESHOLD_S + 30.0))  # past threshold
    orch = _build_orch(tmp_path, session=session, extra_tasks=["task-2"])
    with patch("bernstein.core.orchestration.stalled_manager.time.time", return_value=now):
        assert detect_stalled_manager(orch) is None


def test_detect_fires_when_manager_alive_with_no_children(tmp_path: Path) -> None:
    now = 1_000.0
    session = _manager_session(spawn_ts=now - (STALL_THRESHOLD_S + 30.0))
    _write_hook_events(
        tmp_path,
        session.id,
        [
            {"event": "PostToolUse", "tool_name": "Bash", "tool_input": "find .sdd -type f"},
            {"event": "PostToolUse", "tool_name": "Bash", "tool_input": "curl http://127.0.0.1:8052/tasks"},
            {"event": "PostToolUse", "tool_name": "Grep", "tool_input": "auth"},
        ],
    )
    orch = _build_orch(tmp_path, session=session)
    with patch("bernstein.core.orchestration.stalled_manager.time.time", return_value=now):
        diag = detect_stalled_manager(orch)
    assert diag is not None
    assert diag.session_id == session.id
    assert diag.manager_task_id == "manager-task-1"
    assert diag.runtime_s == STALL_THRESHOLD_S + 30.0
    assert diag.hook_event_count == 3
    # Last 5 Bash commands - there are only 2 here.
    assert diag.last_bash_commands == [
        "find .sdd -type f",
        "curl http://127.0.0.1:8052/tasks",
    ]
    assert diag.remediation == REMEDIATION_DOC


def test_detect_ignores_dead_manager_session(tmp_path: Path) -> None:
    now = 1_000.0
    session = _manager_session(spawn_ts=now - 200.0)
    session.status = "dead"
    orch = _build_orch(tmp_path, session=session)
    with patch("bernstein.core.orchestration.stalled_manager.time.time", return_value=now):
        assert detect_stalled_manager(orch) is None


def test_detect_ignores_non_manager_roles(tmp_path: Path) -> None:
    now = 1_000.0
    session = _manager_session(spawn_ts=now - 200.0)
    session.role = "backend"
    orch = _build_orch(tmp_path, session=session)
    with patch("bernstein.core.orchestration.stalled_manager.time.time", return_value=now):
        assert detect_stalled_manager(orch) is None


# ---------------------------------------------------------------------------
# handle_stalled_manager: side effects
# ---------------------------------------------------------------------------


def test_handle_writes_failure_record_and_log_and_aborts(tmp_path: Path, capsys: Any) -> None:
    now = 1_000.0
    session = _manager_session(spawn_ts=now - (STALL_THRESHOLD_S + 30.0))
    _write_hook_events(
        tmp_path,
        session.id,
        [
            {"event": "PostToolUse", "tool_name": "Bash", "tool_input": "curl /tasks"},
            {"event": "PostToolUse", "tool_name": "Write", "tool_input": "/tmp/task1.json"},
        ],
    )
    orch = _build_orch(
        tmp_path,
        session=session,
        manager_env={
            "BERNSTEIN_AUTH_TOKEN": "super-secret-abc123",
            "BERNSTEIN_SERVER_URL": "http://127.0.0.1:8052",
            "ANTHROPIC_API_KEY": "sk-ant-xxx",
            "HOME": "/home/user",  # should be filtered out
        },
    )

    with (
        patch("bernstein.core.orchestration.stalled_manager.time.time", return_value=now),
        patch("bernstein.core.orchestration.stalled_manager.time.strftime", return_value="20260516T120000"),
    ):
        diag = handle_stalled_manager(orch)

    assert diag is not None

    # Run was aborted cleanly: _running flipped to False.
    assert orch._running is False
    # Sticky flag prevents re-emission.
    assert orch._stalled_manager_emitted is True
    assert orch._stalled_manager_diagnostic is diag

    # Console message surfaced.
    captured = capsys.readouterr()
    assert "Manager session" in captured.out
    assert REMEDIATION_DOC in captured.out

    # Bulletin was posted with the alert.
    assert orch.bulletins == [("alert", f"stalled_manager: {diag.message()}")]

    # Failure record was persisted under .sdd/runtime/failures/.
    failures_dir = tmp_path / ".sdd" / "runtime" / "failures"
    files = list(failures_dir.glob("manager-stalled-*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["kind"] == "stalled_manager"
    assert record["session_id"] == session.id
    assert record["manager_task_id"] == "manager-task-1"
    assert record["hook_event_count"] == 2
    assert record["remediation"] == REMEDIATION_DOC
    # Secrets redacted; non-secret tracked env retained; non-tracked dropped.
    assert record["env_seen"]["BERNSTEIN_AUTH_TOKEN"] == "<redacted>"
    assert record["env_seen"]["ANTHROPIC_API_KEY"] == "<redacted>"
    assert record["env_seen"]["BERNSTEIN_SERVER_URL"] == "http://127.0.0.1:8052"
    assert "HOME" not in record["env_seen"]

    # Orchestrator log line written.
    log_path = tmp_path / ".sdd" / "runtime" / "orchestrator.log"
    assert log_path.exists()
    log_contents = log_path.read_text(encoding="utf-8")
    assert "stalled_manager" in log_contents
    assert REMEDIATION_DOC in log_contents


def test_handle_is_idempotent_across_ticks(tmp_path: Path) -> None:
    now = 1_000.0
    session = _manager_session(spawn_ts=now - 200.0)
    orch = _build_orch(tmp_path, session=session)
    with patch("bernstein.core.orchestration.stalled_manager.time.time", return_value=now):
        first = handle_stalled_manager(orch)
        second = handle_stalled_manager(orch)
    assert first is not None
    assert second is None  # already emitted; do not re-fire


def test_handle_returns_none_when_no_stall(tmp_path: Path) -> None:
    now = 1_000.0
    session = _manager_session(spawn_ts=now - 10.0)
    orch = _build_orch(tmp_path, session=session)
    with patch("bernstein.core.orchestration.stalled_manager.time.time", return_value=now):
        assert handle_stalled_manager(orch) is None
    assert orch._running is True


# ---------------------------------------------------------------------------
# Diagnostic message + helpers
# ---------------------------------------------------------------------------


def test_diagnostic_message_includes_actionable_pointer() -> None:
    diag = StalledManagerDiagnostic(
        session_id="manager-deadbeef",
        manager_task_id="task-1",
        runtime_s=125.0,
        hook_event_count=18,
    )
    msg = diag.message()
    assert "manager-deadbeef" in msg
    assert "125s" in msg
    assert "18 hook event" in msg
    assert REMEDIATION_DOC in msg
    assert "authenticate" in msg.lower()


def test_redact_env_handles_secret_substrings() -> None:
    out = _redact_env(
        {
            "BERNSTEIN_AUTH_TOKEN": "abc",
            "BERNSTEIN_HOOK_SECRET": "xyz",
            "BERNSTEIN_BIND_HOST": "127.0.0.1",
            "OPENROUTER_API_KEY": "sk-or-...",
            "PATH": "/usr/bin",
        }
    )
    assert out["BERNSTEIN_AUTH_TOKEN"] == "<redacted>"
    assert out["BERNSTEIN_HOOK_SECRET"] == "<redacted>"
    assert out["BERNSTEIN_BIND_HOST"] == "127.0.0.1"
    assert out["OPENROUTER_API_KEY"] == "<redacted>"
    assert "PATH" not in out


def test_build_diagnostic_picks_last_five_bash_commands(tmp_path: Path) -> None:
    session = _manager_session(spawn_ts=time.time() - 100.0)
    events = [{"event": "PostToolUse", "tool_name": "Bash", "tool_input": f"cmd-{i}"} for i in range(8)]
    _write_hook_events(tmp_path, session.id, events)
    diag = build_diagnostic(tmp_path, session, now=time.time())
    assert diag.last_bash_commands == ["cmd-3", "cmd-4", "cmd-5", "cmd-6", "cmd-7"]
    assert diag.hook_event_count == 8
