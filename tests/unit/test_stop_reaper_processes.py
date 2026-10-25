"""Real owned POSIX groups through the cooperative reaper and actual tick."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.agents.stop_reaper import StopEscalationPolicy, StopReaper, StopTier
from bernstein.core.config import platform_compat
from bernstein.core.orchestration.orchestrator import Orchestrator
from bernstein.core.server import create_app
from bernstein.core.tasks.models import AgentBackend, AgentSession, OrchestratorConfig

if TYPE_CHECKING:
    from collections.abc import Iterator


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real POSIX groups required")


def _wait_for_group_exit(pgid: int) -> None:
    deadline = time.monotonic() + 5
    while platform_compat.process_group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not platform_compat.process_group_alive(pgid)


@contextmanager
def _owned_group(code: str) -> Iterator[subprocess.Popen[str]]:
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        assert os.getpgid(proc.pid) == proc.pid != os.getpgrp()
        yield proc
    finally:
        # The only signal target is the independent group created above.
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        _wait_for_group_exit(proc.pid)
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()


def _task(client: TestClient, title: str) -> str:
    response = client.post("/tasks", json={"title": title, "description": "d", "role": "backend"})
    assert response.status_code == 201, response.text
    tid = str(response.json()["id"])
    assert client.post(f"/tasks/{tid}/claim", params={"claimed_by_session": "worker"}).status_code == 200
    return tid


def _wire(
    tmp_path: Path,
    client: TestClient,
    proc: subprocess.Popen[str],
    tid: str,
) -> tuple[Orchestrator, list[float], AgentSession, StopReaper]:
    def forward(request: httpx.Request) -> httpx.Response:
        response = client.request(
            request.method,
            str(request.url),
            content=request.content,
            headers={"content-type": request.headers.get("content-type", "application/json")},
        )
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    adapter = MagicMock(spec=CLIAdapter)
    adapter.name.return_value = "owned-process-test"
    adapter.is_rate_limited.return_value = False
    adapter.kill.side_effect = platform_compat.reap_process_group
    templates = tmp_path / "templates/roles"
    templates.mkdir(parents=True)
    spawner = AgentSpawner(adapter, templates, tmp_path, default_model="unused-model")
    orch = Orchestrator(
        OrchestratorConfig(server_url="http://testserver"),
        spawner,
        tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(forward), base_url="http://testserver"),
    )
    now = [0.0]
    reaper = StopReaper(tmp_path, clock=lambda: now[0], policy=StopEscalationPolicy())
    vars(orch)["_stop_reaper"] = reaper
    session = AgentSession(
        id="owned-worker",
        role="backend",
        task_ids=[tid],
        pid=proc.pid,
        pgid=proc.pid,
        status="working",
        spawn_ts=time.time(),
        heartbeat_ts=time.time(),
    )
    orch._agents[session.id] = session
    orch._spawner._procs[session.id] = proc
    return orch, now, session, reaper


def test_actual_tick_keeps_surviving_child_and_mailbox_waiter_claims(tmp_path: Path) -> None:
    exit_flag = tmp_path / "wrapper-exit"
    child = "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"
    wrapper = (
        "import subprocess,sys,time,pathlib; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "print('ready',flush=True); "
        f"flag=pathlib.Path({str(exit_flag)!r}); "
        "\nwhile not flag.exists(): time.sleep(0.01)"
    )
    app = create_app(jsonl_path=tmp_path / "server/tasks.jsonl")
    app.state.draining = False
    with TestClient(app) as client, _owned_group(wrapper) as proc:
        tid = _task(client, "stop wrapper")
        waiter = _task(client, "unrelated waiter")
        # Existing mailbox route owns SUSPENDED; model the persisted server state
        # here while the actual tick/reaper and real process observation run.
        store = app.state.store
        from bernstein.core.tasks.models import TaskStatus

        asyncio.run(store.suspend_for_rendezvous(waiter, "test-open-entry"))
        assert client.post(f"/tasks/{tid}/cancel?stop_mode=request", json={"reason": "stop"}).status_code == 200
        orch, _now, session, _reaper = _wire(tmp_path, client, proc, tid)
        exit_flag.touch()
        assert proc.wait(timeout=5) == 0
        assert platform_compat.process_group_alive(proc.pid)
        assert orch._spawner.check_alive(session)
        orch.tick()
        assert client.get(f"/tasks/{tid}").json()["status"] == "stop_req"
        assert store._tasks[tid].claimed_by_session == "worker"
        assert store._tasks[waiter].status == TaskStatus.SUSPENDED
        assert store._tasks[waiter].claimed_by_session == "worker"
        os.killpg(proc.pid, signal.SIGKILL)
        _wait_for_group_exit(proc.pid)
        orch.tick()
        assert client.get(f"/tasks/{tid}").json()["status"] == "cancelled"
        events = app.state.audit_chain.query(event_type="task.release_receipt")
        assert len([e for e in events if e.details.get("release_path") == "stop_confirmed"]) == 1
        assert store._tasks[waiter].status == TaskStatus.SUSPENDED
        orch._client.close()


def test_real_group_escalates_and_releases_once_after_observed_death(tmp_path: Path) -> None:
    code = (
        "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
    )
    app = create_app(jsonl_path=tmp_path / "server/tasks.jsonl")
    app.state.draining = False
    with TestClient(app) as client, _owned_group(code) as proc:
        tid = _task(client, "stop stubborn child")
        assert client.post(f"/tasks/{tid}/cancel?stop_mode=request", json={"reason": "stop"}).status_code == 200
        orch, now, _session, reaper = _wire(tmp_path, client, proc, tid)
        for timestamp, tier in [(0, StopTier.NOTIFY), (5, StopTier.INT), (15, StopTier.TERM), (30, StopTier.KILL)]:
            now[0] = timestamp
            orch.tick()
            assert reaper._sessions["owned-worker"].tier == tier
            assert client.get(f"/tasks/{tid}").json()["status"] == "stop_req"
        assert proc.wait(timeout=5) == -signal.SIGKILL
        now[0] = 31
        orch.tick()
        assert client.get(f"/tasks/{tid}").json()["status"] == "cancelled"
        orch.tick()
        events = app.state.audit_chain.query(event_type="task.release_receipt")
        assert len([e for e in events if e.details.get("release_path") == "stop_confirmed"]) == 1
        orch._client.close()


class _EventAdapter(CLIAdapter):
    def __init__(self, release: Event) -> None:
        super().__init__()
        self.release = release
        self.started = Event()

    def name(self) -> str:
        return "owned-event-thread"

    def spawn(self, **kwargs: Any) -> SpawnResult:
        self.started.set()
        assert self.release.wait(60), "owned worker was not released"
        return SpawnResult(pid=0, log_path=Path(kwargs["workdir"]) / "thread.log")


def test_actual_tick_keeps_live_in_process_handle_until_real_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = Event()
    adapter = _EventAdapter(release)
    app = create_app(jsonl_path=tmp_path / "server/tasks.jsonl")
    app.state.draining = False
    with TestClient(app) as client:
        tid = _task(client, "event worker")

        def forward(request: httpx.Request) -> httpx.Response:
            response = client.request(
                request.method,
                str(request.url),
                content=request.content,
                headers={"content-type": request.headers.get("content-type", "application/json")},
            )
            return httpx.Response(response.status_code, content=response.content, headers=response.headers)

        templates = tmp_path / "templates/roles"
        templates.mkdir(parents=True)
        spawner = AgentSpawner(
            adapter,
            templates,
            tmp_path,
            default_model="mock-model",
            use_worktrees=False,
            backend=AgentBackend.IN_PROCESS,
        )
        orch = Orchestrator(
            OrchestratorConfig(server_url="http://testserver"),
            spawner,
            tmp_path,
            client=httpx.Client(transport=httpx.MockTransport(forward), base_url="http://testserver"),
        )
        session = spawner.spawn_for_tasks([app.state.store.get_task(tid)])
        assert adapter.started.wait(2)
        assert spawner._in_process is not None
        handle = spawner._in_process.active_sessions[session.id]
        from bernstein.core.agents import agent_lifecycle

        os_probe = MagicMock(side_effect=AssertionError("in-process worker reached OS PID probe"))
        os_signal = MagicMock(side_effect=AssertionError("in-process worker reached OS group signal"))
        monkeypatch.setattr(agent_lifecycle, "_is_process_alive", os_probe)
        monkeypatch.setattr(platform_compat, "kill_process_group", os_signal)
        now = [0.0]
        reaper = StopReaper(tmp_path, clock=lambda: now[0])
        vars(orch)["_stop_reaper"] = reaper
        orch._agents[session.id] = session
        try:
            response = client.post(f"/tasks/{tid}/cancel?stop_mode=request", json={"reason": "stop"})
            assert response.status_code == 200
            for timestamp in [0, 6, 30, 100]:
                now[0] = timestamp
                orch.tick()
                assert handle.thread.is_alive()
                assert session.id in spawner._in_process.active_sessions, "live cooperative worker lost its handle"
                assert spawner._in_process.active_sessions[session.id] is handle
                assert app.state.store.get_task(tid).status.value == "stop_req"
                assert app.state.store.get_task(tid).claimed_by_session == "worker"
                assert not app.state.audit_chain.query(event_type="task.release_receipt")
            release.set()
            handle.thread.join(timeout=2)
            assert not handle.thread.is_alive()
            orch.tick()
            assert app.state.store.get_task(tid).status.value == "cancelled"
            orch.tick()
            receipts = app.state.audit_chain.query(event_type="task.release_receipt")
            assert len([e for e in receipts if e.details.get("release_path") == "stop_confirmed"]) == 1
            assert session.id not in spawner._in_process.active_sessions
            os_probe.assert_not_called()
            os_signal.assert_not_called()
            assert not reaper._session_stop_ops
        finally:
            release.set()
            handle.thread.join(timeout=2)
            spawner._in_process.cleanup(session.id)
            orch._client.close()


def test_in_process_heartbeat_does_not_probe_os_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from bernstein.core.agents import agent_lifecycle

    release = Event()
    adapter = _EventAdapter(release)
    templates = tmp_path / "templates/roles"
    templates.mkdir(parents=True)
    spawner = AgentSpawner(
        adapter, templates, tmp_path, default_model="mock-model", use_worktrees=False, backend=AgentBackend.IN_PROCESS
    )
    from bernstein.core.tasks.models import Task

    session = spawner.spawn_for_tasks([Task(id="heartbeat-thread", title="thread", description="d", role="backend")])
    assert spawner._in_process is not None
    handle = spawner._in_process.active_sessions[session.id]
    probe = MagicMock(side_effect=AssertionError("thread identity reached OS PID probe"))
    monkeypatch.setattr(agent_lifecycle, "_is_process_alive", probe)
    try:
        agent_lifecycle._refresh_heartbeat_from_signals(
            SimpleNamespace(_spawner=spawner, _workdir=tmp_path), session, 123.0
        )
        assert session.heartbeat_ts == 123.0
        probe.assert_not_called()
    finally:
        release.set()
        handle.thread.join(timeout=2)
        spawner._in_process.cleanup(session.id)


def test_external_process_heartbeat_retains_os_pid_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from bernstein.core.agents import agent_lifecycle

    with _owned_group("import time; print('ready',flush=True); time.sleep(60)") as proc:
        templates = tmp_path / "templates/roles"
        templates.mkdir(parents=True)
        spawner = AgentSpawner(MagicMock(spec=CLIAdapter), templates, tmp_path)
        session = AgentSession(id="external-heartbeat", role="backend", pid=proc.pid, pgid=proc.pid, status="working")
        probe = MagicMock(wraps=agent_lifecycle._is_process_alive)
        monkeypatch.setattr(agent_lifecycle, "_is_process_alive", probe)
        agent_lifecycle._refresh_heartbeat_from_signals(
            SimpleNamespace(_spawner=spawner, _workdir=tmp_path), session, 321.0
        )
        assert session.heartbeat_ts == 321.0
        probe.assert_called_once_with(proc.pid)
