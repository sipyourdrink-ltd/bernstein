"""``bernstein ps`` sees an agent whose worker died but whose child is alive.

``_collect_pid_agents`` keyed liveness solely on ``worker_pid`` and deleted the
PID file when that PID was gone, so an adapter leaf process still running under
a dead worker was reported as no agent at all (issue #2800). Liveness now also
probes ``child_pid`` and keeps the file while any tracked PID is alive.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from bernstein.cli.commands import status_cmd


def _write_pid(pid_path: Path, name: str, **fields: Any) -> Path:
    pid_path.mkdir(parents=True, exist_ok=True)
    record = {"session": name, "role": "backend", "started_at": time.time()}
    record.update(fields)
    f = pid_path / f"{name}.json"
    f.write_text(json.dumps(record), encoding="utf-8")
    return f


def test_child_alive_keeps_agent(tmp_path: Path, monkeypatch: Any) -> None:
    """Worker dead but child alive -> agent is still reported, file not stale."""
    pid_path = tmp_path / "pids"
    f = _write_pid(pid_path, "s1", worker_pid=111, child_pid=222)

    monkeypatch.setattr(status_cmd, "is_process_alive", lambda pid: pid == 222)

    agents, stale = status_cmd._collect_pid_agents(pid_path)

    assert [a["session"] for a in agents] == ["s1"]
    assert f not in stale


def test_worker_and_child_dead_is_stale(tmp_path: Path, monkeypatch: Any) -> None:
    """Both PIDs dead -> agent dropped and its file marked stale."""
    pid_path = tmp_path / "pids"
    f = _write_pid(pid_path, "s2", worker_pid=111, child_pid=222)

    monkeypatch.setattr(status_cmd, "is_process_alive", lambda pid: False)

    agents, stale = status_cmd._collect_pid_agents(pid_path)

    assert agents == []
    assert f in stale


def test_worker_alive_reports_agent(tmp_path: Path, monkeypatch: Any) -> None:
    """Existing behaviour preserved: a live worker is reported."""
    pid_path = tmp_path / "pids"
    _write_pid(pid_path, "s3", worker_pid=111, child_pid=222)

    monkeypatch.setattr(status_cmd, "is_process_alive", lambda pid: pid == 111)

    agents, stale = status_cmd._collect_pid_agents(pid_path)

    assert [a["session"] for a in agents] == ["s3"]
    assert stale == []
