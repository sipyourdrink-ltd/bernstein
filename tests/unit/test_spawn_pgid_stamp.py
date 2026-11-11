"""Only independently led process groups may be stamped onto sessions."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.agents import spawner_core
from bernstein.core.config import platform_compat
from bernstein.core.tasks.models import AgentBackend, Scope, Task


@pytest.mark.parametrize(
    ("pid", "group", "own_group", "expected"),
    [
        (123, 123, 456, 123),
        (123, 456, 789, None),
        (123, 123, 123, None),
        (None, 123, 456, None),
        (0, 0, 456, None),
        (1, 1, 456, None),
    ],
)
def test_posix_group_validation(monkeypatch, pid, group, own_group, expected):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(
        spawner_core,
        "os",
        SimpleNamespace(
            name="posix",
            getpgid=lambda value: group,
            getpgrp=lambda: own_group,
        ),
        raising=False,
    )
    assert spawner_core._leader_pgid(pid) == expected


def test_missing_process(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(spawner_core.os, "name", "posix")

    def gone(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(spawner_core.os, "getpgid", gone, raising=False)
    assert spawner_core._leader_pgid(123) is None


@pytest.mark.parametrize(("pid", "expected"), [(123, 123), (1, 1), (0, None), (-1, None), (None, None)])
def test_windows_tree_leader(monkeypatch, pid, expected):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert spawner_core._leader_pgid(pid) == expected


def test_unknown_platform(monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(spawner_core.os, "name", "other")
    assert spawner_core._leader_pgid(123) is None


@pytest.mark.skipif(os.name != "posix", reason="Requires POSIX process groups")
@pytest.mark.parametrize("isolated", [True, False])
def test_real_process_group(isolated):
    kwargs = platform_compat.process_group_popen_kwargs() if isolated else {}
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
    try:
        if isolated:
            assert spawner_core._leader_pgid(proc.pid) == proc.pid == os.getpgid(proc.pid)
        else:
            assert spawner_core._leader_pgid(proc.pid) is None
    finally:
        if isolated:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        proc.wait(timeout=5)


class _ResultAdapter(CLIAdapter):
    def __init__(self, result: SpawnResult) -> None:
        super().__init__()
        self.result = result

    def name(self) -> str:
        return "owned-result-test"

    def spawn(self, **kwargs: Any) -> SpawnResult:
        return self.result


def _spawner(tmp_path, adapter, backend=AgentBackend.SUBPROCESS):
    templates = tmp_path / "templates/roles"
    templates.mkdir(parents=True, exist_ok=True)
    return spawner_core.AgentSpawner(
        adapter,
        templates,
        tmp_path,
        backend=backend,
        use_worktrees=False,
        default_model="mock-model",
    )


def _task():
    return Task(id="group-task", title="group task", description="d", role="backend", scope=Scope.SMALL)


@pytest.mark.parametrize("synthetic", [123, 2**63])
def test_in_process_agent_does_not_probe_os_process_group(tmp_path, monkeypatch, synthetic):
    adapter = _ResultAdapter(SpawnResult(pid=0, log_path=tmp_path / "unused.log"))
    spawner = _spawner(tmp_path, adapter, AgentBackend.IN_PROCESS)
    assert spawner._in_process is not None

    def run(**kwargs):
        # The dispatch decision must stay bound to the actual route, even
        # if mutable configuration is changed while that route runs.
        spawner._backend = AgentBackend.SUBPROCESS
        return synthetic, tmp_path / "thread.log"

    monkeypatch.setattr(spawner._in_process, "run", run)
    probe = MagicMock(side_effect=AssertionError("synthetic handle reached OS group probe"))
    monkeypatch.setattr(spawner_core, "_leader_pgid", probe)
    session = spawner.spawn_for_tasks([_task()])
    assert session.pid == synthetic
    assert session.pgid is None
    probe.assert_not_called()


@pytest.mark.skipif(os.name != "posix", reason="Real POSIX process group required")
@pytest.mark.parametrize("resume", [False, True], ids=["primary", "resume-in-process-config"])
def test_external_dispatch_stamps_owned_group(tmp_path, resume):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        adapter = _ResultAdapter(SpawnResult(pid=proc.pid, proc=proc, log_path=tmp_path / "child.log"))
        spawner = _spawner(tmp_path, adapter, AgentBackend.IN_PROCESS if resume else AgentBackend.SUBPROCESS)
        session = (
            spawner.spawn_for_resume([_task()], worktree_path=tmp_path, changed_files=[])
            if resume
            else spawner.spawn_for_tasks([_task()])
        )
        assert session.pid == session.pgid == proc.pid
        assert spawner.check_alive(session)
        _request_stop, observe = spawner._cooperative_stop_callbacks(session)
        assert observe()
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_unknown_in_process_handle_is_not_an_os_target(tmp_path, monkeypatch):
    adapter = _ResultAdapter(SpawnResult(pid=0, log_path=tmp_path / "unused.log"))
    spawner = _spawner(tmp_path, adapter, AgentBackend.IN_PROCESS)
    from bernstein.core.tasks.models import AgentSession

    session = AgentSession(id="unknown-thread", role="backend", pid=123, status="working")
    probe = MagicMock(side_effect=AssertionError("unknown handle reached OS adapter operation"))
    monkeypatch.setattr(adapter, "kill", probe)
    monkeypatch.setattr(adapter, "is_alive", probe)
    request, observe = spawner._cooperative_stop_callbacks(session)
    request()
    assert observe(), "unknown provenance cannot prove death or authorize an OS target"


@pytest.mark.skipif(os.name != "posix", reason="Real owned POSIX child required")
def test_resume_nonleader_uses_real_process_callbacks_under_in_process_config(tmp_path, monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    stopped = []
    try:
        assert os.getpgid(proc.pid) == os.getpgrp() != proc.pid
        adapter = _ResultAdapter(SpawnResult(pid=proc.pid, proc=proc, log_path=tmp_path / "ordinary-child.log"))

        def stop_owned(pid):
            assert pid == proc.pid
            stopped.append(pid)
            proc.kill()  # Only this owned PID; never signal its shared group.

        monkeypatch.setattr(adapter, "kill", stop_owned)
        group_signal = MagicMock(side_effect=AssertionError("shared process group must not be signalled"))
        monkeypatch.setattr(platform_compat, "kill_process_group", group_signal)
        spawner = _spawner(tmp_path, adapter, AgentBackend.IN_PROCESS)
        session = spawner.spawn_for_resume([_task()], worktree_path=tmp_path, changed_files=[])
        assert session.pgid is None
        request, observe = spawner._cooperative_stop_callbacks(session)
        assert observe(), "real resumed process was mistaken for a missing thread"
        request()
        assert stopped == [proc.pid]
        proc.wait(timeout=5)
        assert not observe()
        group_signal.assert_not_called()
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_cooperative_process_finalization_never_signals_after_gone(tmp_path, monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        adapter = _ResultAdapter(SpawnResult(pid=proc.pid, proc=proc, log_path=tmp_path / "exited.log"))
        spawner = _spawner(tmp_path, adapter)
        session = spawner.spawn_for_tasks([_task()])
        proc.wait(timeout=5)
        forbidden = MagicMock(side_effect=AssertionError("observed-dead process was signalled again"))
        monkeypatch.setattr(adapter, "kill", forbidden)
        monkeypatch.setattr(proc, "kill", forbidden)
        monkeypatch.setattr(proc, "terminate", forbidden)
        _request, observe = spawner._cooperative_stop_callbacks(session)
        assert not observe()
        finalize = spawner._cooperative_stop_finalizer(session)
        finalize()
        finalize()  # Retry cannot target a recycled PID.
        assert session.status == "dead"
        assert session.id not in spawner._procs
        forbidden.assert_not_called()
    finally:
        proc.wait(timeout=5)


def test_replaced_execution_callbacks_cannot_target_new_session(tmp_path, monkeypatch):
    from bernstein.core.tasks.models import AgentSession

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        adapter = _ResultAdapter(SpawnResult(pid=proc.pid, proc=proc, log_path=tmp_path / "replacement.log"))
        spawner = _spawner(tmp_path, adapter)
        old = AgentSession(id="reused", role="backend", pid=proc.pid, status="working")
        spawner._procs[old.id] = proc
        spawner._capture_session_execution(old, in_process=False, adapter=adapter)
        request, observe = spawner._cooperative_stop_callbacks(old)
        finalize = spawner._cooperative_stop_finalizer(old)
        replacement = replace(old)
        spawner._capture_session_execution(replacement, in_process=False, adapter=adapter)
        forbidden = MagicMock(side_effect=AssertionError("stale execution targeted replacement"))
        monkeypatch.setattr(adapter, "kill", forbidden)
        request()
        stale_request, stale_observe = spawner._cooperative_stop_callbacks(old)
        stale_request()
        assert stale_observe()
        assert observe(), "stale observation cannot confirm replacement death"
        with pytest.raises(RuntimeError, match="replaced execution"):
            finalize()
        with pytest.raises(RuntimeError, match="replaced execution"):
            spawner._finalize_cooperative_stop(old)
        assert replacement.status == "working"
        assert spawner._procs[replacement.id] is proc and proc.poll() is None
        # Same object, new dispatch epoch also invalidates old callbacks.
        latest_finalize = spawner._cooperative_stop_finalizer(replacement)
        spawner._capture_session_execution(replacement, in_process=False, adapter=adapter)
        with pytest.raises(RuntimeError, match="replaced execution"):
            latest_finalize()
        forbidden.assert_not_called()
    finally:
        proc.kill()
        proc.wait(timeout=5)


@pytest.mark.parametrize("resume", [False, True], ids=["primary", "resume"])
def test_actual_dispatch_adapter_is_retained_when_default_changes(tmp_path, monkeypatch, resume):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        result = SpawnResult(pid=proc.pid, proc=proc, log_path=tmp_path / "actual-adapter.log")
        actual = _ResultAdapter(result)
        replacement = _ResultAdapter(result)
        spawner = _spawner(tmp_path, actual, AgentBackend.IN_PROCESS if resume else AgentBackend.SUBPROCESS)
        actual_kill = MagicMock(return_value=None)
        replacement_kill = MagicMock(return_value=None)
        monkeypatch.setattr(actual, "kill", actual_kill)
        monkeypatch.setattr(replacement, "kill", replacement_kill)

        def dispatch(**kwargs):
            spawner._adapter = replacement
            return result

        monkeypatch.setattr(actual, "spawn", dispatch)
        session = (
            spawner.spawn_for_resume([_task()], worktree_path=tmp_path, changed_files=[])
            if resume
            else spawner.spawn_for_tasks([_task()])
        )
        request, observe = spawner._cooperative_stop_callbacks(session)
        assert observe()
        request()
        actual_kill.assert_called_once_with(proc.pid)
        replacement_kill.assert_not_called()
    finally:
        proc.kill()
        proc.wait(timeout=5)
