"""Verified agent-completion contract harness for workflow agent nodes (#6639).

Shared local-process support for the characterization slices of #6841.
This is executable evidence for the proposed completion design; it does not
implement runtime completion or establish an approved runtime contract. The first docstring line of every test names its category:

* ``[EXISTING]`` - behaviour the inspected runner/store already provides and
  any implementation must keep.
* ``[PROPOSED #N]`` - item N of the numbered property matrix in the draft
  design. A failure on the inspected base is a gap against that proposal,
  not an approved API defect.
* ``[GATE #N]`` - executable statement that item N's positive control has no
  native interface to exercise it yet. It flips when one lands; replace it
  with the positive control then.
* ``[NATIVE FACT]`` - a pinned property of an existing native component that
  a review decision relies on. If one flips, revisit the cited decision.

Harness rules: real ``WorkflowRunner`` and real ``TaskStore``; agent work is a
controlled local child released through a barrier file; the downstream node
is a real command appending to a sentinel file; every child is reaped in
``finally``; every wait is bounded; no network, provider, credential, model
or remote executor is involved. All state lives under ``tmp_path``.

The single seam an implementation slice should edit here is
:func:`_contract_runner`: wire the approved completion authority there.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.persistence.workspace import grant_workspace_trust
from bernstein.core.server.server_models import TaskCreate
from bernstein.core.tasks.models import AgentSession, Task, TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore
from bernstein.core.workflows import WorkflowExecution, WorkflowRunner, load_workflow_spec_from_text

if TYPE_CHECKING:
    from bernstein.core.workflows.workflow_runner import (
        AuditEmitter,
        NodeExecution,
    )


class _ProposedContractGap(AssertionError):
    """Only a reproduced contract assertion can count as an expected failure."""


_proposed_gap = pytest.mark.xfail(strict=True, raises=_ProposedContractGap, reason="Proposed workflow contract #6639")


@contextlib.contextmanager
def _known_contract_gap() -> Iterator[None]:
    try:
        yield
    except AssertionError as exc:
        raise _ProposedContractGap(str(exc)) from exc


#: Window in which a premature dependency release becomes visible.
OBSERVE_S = 1.0
#: Hard cap on any single wait in this module.
BOUND_S = 15.0
#: ``timeout_seconds`` for agent nodes (the manifest minimum is 1).
AGENT_TIMEOUT_S = 2
TENANT = "default"
CHILD_MARKER = "bernstein-wf-contract-child"

# The controlled agent. ``mode`` is ``block`` (run until the barrier file
# appears, then exit 0) or an exit code. Lifetime is capped so a crashed test
# process can never leave it running indefinitely.
_CHILD = r"""
import os, signal, sys, time
_marker, started, release, mode, ignore_term = sys.argv[1:6]
if ignore_term == "1":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
tmp = started + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
os.replace(tmp, started)
if mode != "block":
    sys.exit(int(mode))
deadline = time.monotonic() + 60.0
while not os.path.exists(release) and time.monotonic() < deadline:
    time.sleep(0.02)
sys.exit(0)
"""


class _SimulatedCrash(BaseException):
    """Stands in for the runner process dying; bypasses ``except Exception``."""


def _wait_for(predicate: Callable[[], bool], what: str, timeout: float = BOUND_S) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"harness: timed out after {timeout}s waiting for {what}")
        time.sleep(0.02)


def _pid_alive(pid: int) -> bool:
    """Probe a controlled child; procfs also excludes zombies and checks its marker."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError as exc:
        if isinstance(exc, FileNotFoundError) and Path("/proc").is_dir():
            return False
        # No procfs (for example, macOS): probe the PID instead. A missing
        # /proc entry only proves exit when procfs itself is available.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    if stat.rsplit(")", 1)[1].split()[0] == "Z":
        return False
    try:
        return CHILD_MARKER.encode() in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Native store driver and controlled dispatcher
# ---------------------------------------------------------------------------


class _StoreDriver:
    """Single owner of one real :class:`TaskStore`, callable from any thread.

    TaskStore is async and documented single-process. One background loop owns
    it; :meth:`restart` discards that instance and replays the same JSONL into
    a fresh one (sequential ownership, never concurrent writers).
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "runtime" / "tasks.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="taskstore-loop", daemon=True)
        self._thread.start()
        self.store = self._open()

    def _open(self) -> TaskStore:
        store = TaskStore(
            self.path,
            archive_path=self.root / "archive" / "tasks.jsonl",
            metrics_jsonl_path=self.root / "metrics" / "tasks.jsonl",
        )
        store.replay_jsonl()
        return store

    def call(self, factory: Callable[[TaskStore], Coroutine[Any, Any, Any]]) -> Any:
        return asyncio.run_coroutine_threadsafe(factory(self.store), self._loop).result(timeout=BOUND_S)

    def restart(self) -> None:
        self.store = self._open()

    def task(self, task_id: str) -> Task | None:
        return self.store.get_task(task_id)

    def close(self) -> None:
        if self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=BOUND_S)
        self._loop.close()


@dataclass
class _Plan:
    """How the controlled dispatcher behaves for every spawn it serves."""

    mode: str = "block"
    ignore_sigterm: bool = False
    crash_after_launch: bool = False
    after_launch: Callable[[_LifecycleSpawner, Task, AgentSession, subprocess.Popen[bytes]], None] | None = None


class _LifecycleSpawner:
    """Controlled stand-in for ``AgentSpawner.spawn_for_tasks``.

    Mirrors the native shape: the session id is minted here as
    ``<role>-<8 hex>``, the call returns while the agent still runs, and no
    store is consulted before spawning. Like an orchestrator, it also registers
    (lookup before create) and claims the task in the real TaskStore. Every
    dispatch appends to ``spawn_calls.log``, the synthetic invocation counter
    shared across restarts and processes.
    """

    def __init__(self, driver: _StoreDriver, fixture_dir: Path, plan: _Plan | None = None) -> None:
        self.driver = driver
        self.dir = fixture_dir
        self.plan = plan or _Plan()
        self.procs: list[subprocess.Popen[bytes]] = []
        self.sessions: list[AgentSession] = []
        self.claim_refusals: list[str] = []
        self.first_dispatch_returning = threading.Event()
        self._lock = threading.Lock()

    @property
    def release_path(self) -> Path:
        return self.dir / "release"

    def spawned_task_ids(self) -> list[str]:
        path = self.dir / "spawn_calls.log"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def spawn_count(self) -> int:
        return len(self.spawned_task_ids())

    def spawn_for_tasks(self, tasks: list[Task], model_override: str | None = None) -> AgentSession:
        task = tasks[0]
        with self._lock:
            ordinal = self.spawn_count() + 1
            with (self.dir / "spawn_calls.log").open("a", encoding="utf-8") as fh:
                fh.write(f"{task.id}\n")
        if self.driver.task(task.id) is None:
            req = TaskCreate(
                id=task.id, title=task.title, description=task.description, role=task.role, tenant_id=TENANT
            )
            self.driver.call(lambda s: s.create(req))
        session = AgentSession(id=f"{task.role}-{uuid.uuid4().hex[:8]}", role=task.role, task_ids=[task.id])
        started = self.dir / f"started-{ordinal}"
        argv = [sys.executable, "-I", "-c", _CHILD, CHILD_MARKER, str(started), str(self.release_path)]
        argv += [self.plan.mode, "1" if self.plan.ignore_sigterm else "0"]
        proc = subprocess.Popen(
            argv,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.procs.append(proc)
        with (self.dir / "children.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{proc.pid}\n")
        _wait_for(started.exists, f"child start marker {started.name}")
        session.pid = proc.pid
        session.status = "working"
        try:
            self.driver.call(lambda s: s.claim_by_id(task.id, claimed_by_session=session.id))
        except Exception as exc:  # the native spawner never gates on the store
            self.claim_refusals.append(f"{task.id}: {type(exc).__name__}")
        self.sessions.append(session)
        if self.plan.after_launch is not None:
            self.plan.after_launch(self, task, session, proc)
        if self.plan.crash_after_launch:
            raise _SimulatedCrash("runner died after the spawn side effect, before confirming it")
        self.first_dispatch_returning.set()
        return session

    def reap(self) -> None:
        self.release_path.touch()
        for proc in self.procs:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=BOUND_S)


class _Run:
    """One runner call on a daemon thread, so a contract-conforming (blocking) runner stays observable."""

    def __init__(self, fn: Callable[[], WorkflowExecution]) -> None:
        self.result: WorkflowExecution | None = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._target, args=(fn,), name="workflow-run", daemon=True)
        self._thread.start()

    def _target(self, fn: Callable[[], WorkflowExecution]) -> None:
        try:
            self.result = fn()
        except BaseException as exc:  # crash injection and runner errors are both evidence
            self.error = exc

    def wait(self, timeout: float) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def done(self) -> bool:
        return not self._thread.is_alive()

    def statuses(self) -> list[tuple[str, str]] | str:
        if self.result is not None:
            return [(n.node_id, n.status.value) for n in self.result.nodes]
        return f"error={self.error!r}" if self.error is not None else "still running"

    def node(self, node_id: str) -> NodeExecution | None:
        if self.result is None:
            return None
        found = [n for n in self.result.nodes if n.node_id == node_id]
        return found[-1] if found else None

    def raise_unexpected(self) -> None:
        if self.error is not None and not isinstance(self.error, _SimulatedCrash):
            raise self.error


def _contract_runner(*, workdir: Path, spawner: _LifecycleSpawner, audit: AuditEmitter | None = None) -> WorkflowRunner:
    """Only seam the implementation slice edits: wire the approved completion authority here."""
    return WorkflowRunner(spawner=spawner, workdir=workdir, audit_emitter=audit)  # type: ignore[arg-type]


def _crash_on(event: str, node_id: str) -> AuditEmitter:
    """Audit emitter that kills the run (once) when ``event`` fires for ``node_id``."""
    fired: list[bool] = []

    def emit(event_type: str, resource_id: str, details: dict[str, Any]) -> None:
        if event_type == event and resource_id == node_id and not fired:
            fired.append(True)
            raise _SimulatedCrash(f"runner died at {event}")

    return emit


@dataclass
class _Env:
    workdir: Path
    fixture_dir: Path
    sentinel: Path
    driver: _StoreDriver
    spawners: list[_LifecycleSpawner] = field(default_factory=list)
    runs: list[_Run] = field(default_factory=list)
    processes: list[subprocess.Popen[bytes]] = field(default_factory=list)

    def spawner(self, plan: _Plan | None = None) -> _LifecycleSpawner:
        sp = _LifecycleSpawner(self.driver, self.fixture_dir, plan)
        self.spawners.append(sp)
        return sp

    def runner(self, spawner: _LifecycleSpawner, audit: AuditEmitter | None = None) -> WorkflowRunner:
        return _contract_runner(workdir=self.workdir, spawner=spawner, audit=audit)

    def start(self, fn: Callable[[], WorkflowExecution]) -> _Run:
        run = _Run(fn)
        self.runs.append(run)
        return run

    def sentinel_writes(self) -> int:
        return len(self.sentinel.read_text(encoding="utf-8")) if self.sentinel.exists() else 0

    def logged_children(self) -> list[int]:
        path = self.fixture_dir / "children.log"
        return [int(p) for p in path.read_text(encoding="utf-8").split()] if path.exists() else []


@pytest.fixture
def env(tmp_path: Path) -> Iterator[_Env]:
    workdir = tmp_path / "wf"
    workdir.mkdir()
    grant_workspace_trust(workdir)
    fixture_dir = tmp_path / "fixture"
    fixture_dir.mkdir()
    driver = _StoreDriver(tmp_path / "sdd")
    e = _Env(workdir=workdir, fixture_dir=fixture_dir, sentinel=workdir / "consume.sentinel", driver=driver)
    try:
        yield e
    finally:
        (fixture_dir / "release").touch()
        for proc in e.processes:
            if proc.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=BOUND_S)
        for sp in e.spawners:
            sp.reap()
        for pid in e.logged_children():  # children of a killed runner process are not ours to wait()
            if _pid_alive(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
        _wait_for(lambda: not any(_pid_alive(p) for p in e.logged_children()), "all controlled children reaped")
        try:
            for run in e.runs:
                assert run.wait(BOUND_S), f"harness: workflow runner did not stop within {BOUND_S}s"
        finally:
            driver.close()
            e.driver.close()


def _spec_text(sentinel: Path, *, produce_extra: str = "", timeout: int = AGENT_TIMEOUT_S) -> str:
    return f"""
name: verified-completion
description: "agent producer, command consumer"
version: "1.0.0"
nodes:
  - id: produce
    agent: backend
    prompt: "Produce the artifact for {{goal}}"
    timeout_seconds: {timeout}
{produce_extra}
  - id: consume
    depends_on: [produce]
    command: "printf x >> {shlex.quote(str(sentinel))}"
"""


def _spec(e: _Env, **kw: Any) -> Any:
    return load_workflow_spec_from_text(_spec_text(e.sentinel, **kw))


def _observe(e: _Env, sp: _LifecycleSpawner, run: _Run) -> None:
    """Wait until the first dispatch (or the run's end), then for the observation window."""
    _wait_for(lambda: sp.spawn_count() >= 1 or run.done(), "first dispatch or run end")
    run.wait(OBSERVE_S)
    run.raise_unexpected()


def _observe_dispatched_attempt(e: _Env, sp: _LifecycleSpawner, run: _Run) -> Task:
    """Require the child and native lifecycle transition before testing the sentinel."""
    _wait_for(lambda: sp.first_dispatch_returning.is_set() or run.done(), "dispatch return or run end")
    run.raise_unexpected()
    assert sp.first_dispatch_returning.is_set(), "precondition: the dispatcher reached its return"
    assert sp.procs and sp.sessions, "precondition: a controlled child was launched"
    run.wait(OBSERVE_S)
    run.raise_unexpected()
    task = e.driver.task(sp.sessions[0].task_ids[0])
    assert task is not None, "precondition: the dispatched task exists in the native store"
    return task


def _wait_exit(proc: subprocess.Popen[bytes]) -> int:
    return proc.wait(timeout=BOUND_S)


def assert_harness_sentinel_dispatcher_and_reaper_are_sound(env: _Env) -> None:
    """[EXISTING] A command producer releases the consumer exactly once; the dispatcher claims natively and reaps."""
    spec = load_workflow_spec_from_text(
        _spec_text(env.sentinel).replace(
            'agent: backend\n    prompt: "Produce the artifact for {goal}"', 'command: "true"'
        )
    )
    execution = WorkflowRunner(workdir=env.workdir).run(spec, run_id="h-cmd")
    assert execution.succeeded is True
    assert env.sentinel_writes() == 1

    sp = env.spawner(_Plan(mode="block"))
    session = sp.spawn_for_tasks([Task(id="h-probe", title="probe", description="probe", role="backend")])
    assert sp.procs[0].poll() is None
    assert _pid_alive(sp.procs[0].pid), "the liveness probe must detect a running controlled child"
    task = env.driver.task("h-probe")
    assert task is not None
    assert task.status is TaskStatus.CLAIMED
    assert task.claimed_by_session == session.id
    sp.reap()
    assert sp.procs[0].poll() is not None
    assert not _pid_alive(sp.procs[0].pid)
