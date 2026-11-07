"""A run that self-stops on quiescence must stay stopped.

Reproduces the restart loop seen on a one-task demo run: the orchestrator
finished, journaled ``run_completed`` then ``run_quiescence``, wrote the
``spawner-deliberate-stop`` marker and exited. The watchdog restarted it five
times anyway. Every restart minted a new run, refused to close the previous
one ("owner record does not match journal start") and queued a fresh retry for
every failed task the earlier processes had already retried.

Each test pins one link of that chain against the real write and read sites.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from bernstein.core.orchestration import bootstrap
from bernstein.core.orchestration.run_closure_owner import (
    read_spawner_run_owner,
    write_spawner_run_owner,
)
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.tasks.models import Task

DEAD_PID = 999999


class _StopLoop(Exception):
    """Raised from the patched sleep to end ``run_watchdog`` after one cycle."""


def _reaped_pid() -> int:
    """A pid that is positively dead: a child that already exited and was reaped."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime


def _one_watchdog_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Run exactly one ``run_watchdog`` poll and return the spawner restarts it made."""
    runtime = _runtime(tmp_path)
    (runtime / "server.pid").write_text(str(os.getpid()))
    (runtime / "watchdog.pid").write_text(str(os.getpid()))
    (runtime / "spawner.pid").write_text(str(_reaped_pid()))

    sleeps = iter([None])

    def _sleep(_s: float) -> None:
        if next(sleeps, _StopLoop) is _StopLoop:
            raise _StopLoop

    restarts: list[Any] = []
    # Replace only bootstrap's ``time``. Patching ``time.sleep`` globally also
    # catches ``subprocess.run(..., timeout=...)`` inside the liveness probe,
    # which sleeps while it reaps ``ps`` on a loaded host and would end the
    # loop before its first cycle.
    monkeypatch.setattr(bootstrap, "time", SimpleNamespace(sleep=_sleep, monotonic=time.monotonic))
    monkeypatch.setattr(bootstrap, "_start_spawner", lambda *a, **kw: restarts.append((a, kw)) or 4242)
    monkeypatch.setattr(bootstrap, "_start_server", lambda *a, **kw: pytest.fail("server is alive"))
    with pytest.raises(_StopLoop):
        bootstrap.run_watchdog(tmp_path, port=8095, poll_s=0)
    return restarts


def test_watchdog_honours_the_deliberate_stop_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The quiescence self-stop writes the marker; the live watchdog loop must read it."""
    (_runtime(tmp_path) / "spawner-deliberate-stop").write_text("quiescence")

    assert _one_watchdog_cycle(tmp_path, monkeypatch) == []


def test_watchdog_still_restarts_a_crash_without_the_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: no marker and no completed journal is an ordinary crash."""
    assert len(_one_watchdog_cycle(tmp_path, monkeypatch)) == 1


def test_supervisor_stands_down_when_quiescence_follows_run_completed(tmp_path: Path) -> None:
    """The real shutdown appends ``run_quiescence`` after ``run_completed``."""
    journal = EventJournal("run-1", tmp_path / ".sdd")
    journal.record("run_started", run_id="run-1")
    write_spawner_run_owner(
        sdd_dir=tmp_path / ".sdd",
        run_id="run-1",
        journal_head=journal.fingerprint(),
        journal_event_count=journal.event_count(),
        pid=DEAD_PID,
    )
    journal.record("run_completed", run_id="run-1", outcome="completed")
    journal.record("run_quiescence", run_id="run-1", verified=True, residual=[])

    assert bootstrap._supervisor_should_stand_down(tmp_path) is not None


def test_orchestrator_owner_record_names_the_run_started_row(tmp_path: Path) -> None:
    """The owner record must match what recovery verifies: the journal's first row.

    ``Orchestrator.run()`` journals ``loaded_extension_set`` right after
    ``run_started``; an owner record taken after that row names the wrong head
    and every recovery attempt refuses the run.
    """
    import httpx

    from tests.unit.test_orchestrator import _build_orchestrator, _mock_transport

    orch = _build_orchestrator(tmp_path, _mock_transport({"GET /tasks": httpx.Response(200, json=[])}))
    orch.tick = lambda: orch.stop()  # type: ignore[assignment,method-assign]
    orch.run()

    owner = read_spawner_run_owner(tmp_path / ".sdd")
    assert owner is not None
    events = load_events(tmp_path / ".sdd" / "runs" / owner.run_id / "journal.jsonl").events
    assert [e["event"] for e in events[:2]] == ["run_started", "loaded_extension_set"]
    assert owner.started_journal_event_count == 1
    assert owner.started_journal_head == events[0]["event_hash"]


def test_a_restarted_orchestrator_does_not_retry_an_already_retried_task() -> None:
    """A retry already on the board is durable proof its source was handled.

    The in-memory set of retried task ids is empty in a fresh process; without
    reading the board, every restart re-queued every earlier failure.
    """
    from bernstein.core.tasks.task_lifecycle import maybe_retry_task, note_queued_retries

    failed = Task(id="a", title="t", description="d", role="docs", status="failed")
    retry = Task(id="b", title="t", description="d", role="docs", metadata={"retry_of": "a"})
    retried: set[str] = set()
    note_queued_retries([failed, retry], retried)

    client = MagicMock()
    queued = maybe_retry_task(
        failed,
        retried_task_ids=retried,
        max_task_retries=3,
        client=client,
        server_url="http://testserver",
        quarantine=MagicMock(),
    )
    assert queued is False
    client.post.assert_not_called()
