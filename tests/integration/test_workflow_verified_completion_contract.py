"""Verified agent-completion contract harness for workflow agent nodes (#6639).

Status: DESIGN_REVIEW_CANDIDATE. Executable evidence for
``docs/architecture/workflow-agent-completion-review.md``; not an approved
runtime contract. The first docstring line of every test names its category:

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
import inspect
import json
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
from typing import Any

import pytest

from bernstein.core.config.platform_compat import reap_process_group
from bernstein.core.lineage.artifact_record import load_receipt, record_artifact, verify_artifact
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore
from bernstein.core.persistence.workspace import grant_workspace_trust
from bernstein.core.server.server_models import TaskCreate
from bernstein.core.tasks.artifacts import ArtifactKind
from bernstein.core.tasks.contracts import RefusalKind, WorkerRefusal
from bernstein.core.tasks.models import AgentSession, Task, TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore
from bernstein.core.workflows import NodeStatus, WorkflowExecution, WorkflowRunner, load_workflow_spec_from_text
from bernstein.core.workflows.workflow_runner import (
    AuditEmitter,
    NodeExecution,
    run_complete_marker_exists,
    spec_digest,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group child fixtures")

#: Window in which a premature dependency release becomes visible.
OBSERVE_S = 1.0
#: Hard cap on any single wait in this module.
BOUND_S = 15.0
#: ``timeout_seconds`` for agent nodes (the manifest minimum is 1).
AGENT_TIMEOUT_S = 2
TENANT = "default"
CHILD_MARKER = "bernstein-wf-contract-child"
_HMAC = b"k" * 64

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

    def seed_closed(self, task_id: str, *, tenant: str = TENANT, session: str = "backend-0ld0ld00") -> None:
        """Drive a task through the native create/claim/complete/close verbs."""
        req = TaskCreate(id=task_id, title=f"seed:{task_id}", description="seeded", role="backend", tenant_id=tenant)
        self.call(lambda s: s.create(req))
        self.call(lambda s: s.claim_by_id(task_id, claimed_by_session=session))
        self.call(lambda s: s.complete(task_id, "seeded result"))
        self.call(lambda s: s.close(task_id))

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
        for run in e.runs:
            run.wait(BOUND_S)
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


def _record_report(root: Path, task_id: str, text: str) -> dict[str, Path]:
    """Record a genuine native artifact receipt (signed lineage entry + canonical blob)."""
    priv, pub = generate_keypair()
    card = AgentCard(agent_id="agent:wf-contract", kid="kid-wf-contract-001", public_key_pem=pub)
    cards = root / "cards"
    (cards / card.agent_id).mkdir(parents=True, exist_ok=True)
    (cards / card.agent_id / "card.json").write_text(
        json.dumps({"agent_id": card.agent_id, "kid": card.kid, "public_key_pem": card.public_key_pem}),
        encoding="utf-8",
    )
    record_artifact(
        recorder=SignedLineageLog(store=LineageStore(root / "lineage"), operator_hmac_key=_HMAC),
        sink_root=root / "artifacts",
        task_id=task_id,
        kind=ArtifactKind.REPORT,
        artifact=text,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
    )
    return {"sink": root / "artifacts", "log": root / "lineage" / "log.jsonl", "cards": cards}


def _verify_report(paths: dict[str, Path], task_id: str) -> Any:
    return verify_artifact(
        task_id=task_id, sink_root=paths["sink"], log_path=paths["log"], cards_dir=paths["cards"], operator_secret=_HMAC
    )


def _tamper(paths: dict[str, Path], task_id: str) -> None:
    blob = paths["sink"] / task_id / "artifact.bin"
    data = bytearray(blob.read_bytes())
    data[-2] ^= 0x20
    blob.write_bytes(bytes(data))


# ---------------------------------------------------------------------------
# Harness soundness
# ---------------------------------------------------------------------------


def test_harness_sentinel_dispatcher_and_reaper_are_sound(env: _Env) -> None:
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


# ---------------------------------------------------------------------------
# Matrix 1 - dependencies wait for verified agent success
# ---------------------------------------------------------------------------


def test_live_claimed_attempt_does_not_release_dependents(env: _Env) -> None:
    """[PROPOSED #1] A returned session id for a live, claimed agent is not completion."""
    sp = env.spawner(_Plan(mode="block"))
    run = env.start(lambda: env.runner(sp).run(_spec(env), run_id="p1-live"))
    task = _observe_dispatched_attempt(env, sp, run)
    assert sp.procs[0].poll() is None, "precondition: the agent child is still running"
    assert task.status is TaskStatus.CLAIMED
    assert task.claimed_by_session == sp.sessions[0].id
    assert env.sentinel_writes() == 0, (
        f"consumer ran while the agent attempt was still live and claimed; runner reported {run.statuses()}"
    )


def test_exit_zero_and_reported_done_without_acceptance_does_not_release(env: _Env) -> None:
    """[PROPOSED #1] Process exit 0 plus an agent-reported DONE is not native acceptance."""

    def reported_done(sp: _LifecycleSpawner, task: Task, session: AgentSession, proc: subprocess.Popen[bytes]) -> None:
        assert _wait_exit(proc) == 0
        sp.driver.call(lambda s: s.complete(task.id, "agent reported DONE"))

    sp = env.spawner(_Plan(mode="0", after_launch=reported_done))
    run = env.start(lambda: env.runner(sp).run(_spec(env), run_id="p1-done"))
    task = _observe_dispatched_attempt(env, sp, run)
    assert sp.procs[0].poll() == 0, "precondition: agent exited successfully"
    assert task.status is TaskStatus.DONE, "precondition: agent-reported DONE"
    assert task.closed_at is None, "precondition: never closed (no acceptance)"
    assert env.sentinel_writes() == 0, (
        f"consumer ran on exit 0 + reported DONE with no acceptance; runner reported {run.statuses()}"
    )


def test_gate_accepted_attempt_positive_control_has_no_native_interface() -> None:
    """[GATE #1/#7] No public interface can supply attempt-bound acceptance to the runner, so the
    'accepted success releases the sentinel exactly once' positive control is not exercisable.

    Flips when the gate opens; then replace this probe with that positive control.
    """
    runner_params = set(inspect.signature(WorkflowRunner.__init__).parameters) - {"self"}
    assert runner_params == {"spawner", "workdir", "audit_emitter", "max_parallel", "env"}, (
        f"GATE OPENED? WorkflowRunner now accepts {sorted(runner_params)}: write the positive control"
    )
    assert set(inspect.signature(TaskStore.close).parameters) == {"self", "task_id"}, (
        "GATE OPENED? TaskStore.close now takes evidence: write the positive control"
    )
    acceptance_api = [name for name in dir(TaskStore) if "accept" in name.lower()]
    assert acceptance_api == [], f"GATE OPENED? TaskStore exposes {acceptance_api}: write the positive control"


# ---------------------------------------------------------------------------
# Matrix 2 - failed / refused / rejected never release
# ---------------------------------------------------------------------------


def _fail_outcome(sp: _LifecycleSpawner, task: Task, session: AgentSession, proc: subprocess.Popen[bytes]) -> None:
    assert _wait_exit(proc) == 3
    sp.driver.call(lambda s: s.fail(task.id, "agent exited 3"))


def _refuse_outcome(sp: _LifecycleSpawner, task: Task, session: AgentSession, proc: subprocess.Popen[bytes]) -> None:
    assert _wait_exit(proc) == 0
    refusal = WorkerRefusal(kind=RefusalKind.UNDERSPECIFIED, detail="spec missing", question="Which format?")
    sp.driver.call(lambda s: s.refuse(task.id, refusal))


def _reject_outcome(sp: _LifecycleSpawner, task: Task, session: AgentSession, proc: subprocess.Popen[bytes]) -> None:
    assert _wait_exit(proc) == 0
    sp.driver.call(lambda s: s.complete(task.id, "agent reported DONE"))
    sp.driver.call(lambda s: s.reopen(task.id, "janitor: verification failed"))


@pytest.mark.parametrize(
    ("mode", "outcome", "native_status"),
    [
        ("3", _fail_outcome, TaskStatus.FAILED),
        ("0", _refuse_outcome, TaskStatus.REFUSED),
        ("0", _reject_outcome, TaskStatus.OPEN),
    ],
    ids=["failed", "refused", "verification_rejected"],
)
def test_failed_refused_or_rejected_attempt_never_releases_dependents(
    env: _Env, mode: str, outcome: Callable[..., None], native_status: TaskStatus
) -> None:
    """[PROPOSED #2] Failure, refusal and rejected verification neither release nor auto-retry."""
    sp = env.spawner(_Plan(mode=mode, after_launch=outcome))
    run = env.start(lambda: env.runner(sp).run(_spec(env), run_id=f"p2-{native_status.value}"))
    task = _observe_dispatched_attempt(env, sp, run)
    assert task.status is native_status, "precondition: native lifecycle outcome recorded"
    produce = run.node("produce")
    assert produce is None or produce.status is not NodeStatus.SUCCESS, (
        f"{native_status.value} attempt reported as node success; runner reported {run.statuses()}"
    )
    assert env.sentinel_writes() == 0, f"consumer released after {native_status.value}"
    assert sp.spawn_count() <= 1, "runner retried the attempt automatically"


def test_spawn_exception_fails_node_and_blocks_dependents(env: _Env) -> None:
    """[EXISTING] A dispatcher exception is a FAILED node, the consumer is SKIPPED, nothing is released."""

    class _Boom:
        def spawn_for_tasks(self, tasks: list[Task], model_override: str | None = None) -> AgentSession:
            raise RuntimeError("synthetic dispatch failure")

    execution = WorkflowRunner(spawner=_Boom(), workdir=env.workdir).run(_spec(env), run_id="p2-raise")  # type: ignore[arg-type]
    by_id = {n.node_id: n for n in execution.nodes}
    assert by_id["produce"].status is NodeStatus.FAILED
    assert "spawn failed" in by_id["produce"].error
    assert by_id["consume"].status is NodeStatus.SKIPPED
    assert env.sentinel_writes() == 0


def test_missing_completion_authority_blocks_before_dispatch(env: _Env) -> None:
    """[PROPOSED] A configured spawner alone cannot authorize agent dispatch."""
    spawner = env.spawner(_Plan(mode="block"))
    # Construct the runner directly so this case still lacks authority when
    # the shared contract fixture starts supplying one.
    runner = WorkflowRunner(spawner=spawner, workdir=env.workdir)  # type: ignore[arg-type]
    run = env.start(lambda: runner.run(_spec(env), run_id="no-authority"))
    assert run.wait(BOUND_S), "runner did not return without completion authority"
    run.raise_unexpected()
    assert spawner.spawn_count() == 0, "agent dispatched without completion authority"
    assert env.sentinel_writes() == 0
    produce = run.node("produce")
    consume = run.node("consume")
    assert produce is not None and produce.status is NodeStatus.FAILED
    assert consume is not None and consume.status is NodeStatus.SKIPPED


# ---------------------------------------------------------------------------
# Matrix 3 - deadline survives a runner restart
# ---------------------------------------------------------------------------


def _runner_process_main(config_path: str) -> None:
    """Crash-restart phase 1: one runner process, SIGKILLed while its attempt is in flight."""
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    driver = _StoreDriver(Path(cfg["sdd"]))
    try:
        spawner = _LifecycleSpawner(driver, Path(cfg["fixture_dir"]), _Plan(mode="block"))
        runner = _contract_runner(workdir=Path(cfg["workdir"]), spawner=spawner)
        execution = runner.run(load_workflow_spec_from_text(cfg["spec"]), run_id=cfg["run_id"])
        result = [[n.node_id, n.status.value] for n in execution.nodes]
        Path(cfg["result"]).write_text(json.dumps(result), encoding="utf-8")
    finally:
        driver.close()


def test_agent_deadline_survives_runner_restart(env: _Env, tmp_path: Path) -> None:
    """[PROPOSED #3] A restart neither resets the attempt's timeout budget nor skips its cancellation."""
    rid = "p3-deadline"
    spec_text = _spec_text(env.sentinel)
    cfg = {
        "sdd": str(tmp_path / "sdd"),
        "fixture_dir": str(env.fixture_dir),
        "workdir": str(env.workdir),
        "spec": spec_text,
        "run_id": rid,
        "result": str(env.fixture_dir / "phase1.json"),
    }
    cfg_path = env.fixture_dir / "phase1-config.json"
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    env.driver.close()  # hand store ownership to the runner process for phase 1
    with (env.fixture_dir / "phase1.log").open("wb") as log:
        runner_proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import runpy, sys; runpy.run_path(sys.argv[1])['_runner_process_main'](sys.argv[2])",
                __file__,
                str(cfg_path),
            ],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    env.processes.append(runner_proc)
    started = env.fixture_dir / "started-1"
    _wait_for(lambda: started.exists() or runner_proc.poll() is not None, "dispatch inside the runner process")
    dispatched_at = time.monotonic()
    with contextlib.suppress(subprocess.TimeoutExpired):
        runner_proc.wait(timeout=0.5)
    phase1 = (
        json.loads(Path(cfg["result"]).read_text(encoding="utf-8")) if Path(cfg["result"]).exists() else "unfinished"
    )
    assert started.exists(), f"precondition: phase 1 dispatched the agent (log: {env.fixture_dir / 'phase1.log'})"
    assert env.sentinel_writes() == 0, (
        f"phase 1: consumer ran before the attempt was accepted or timed out; runner process reported {phase1}"
    )

    # Contract path: the runner is waiting inside the attempt. Crash it, let the deadline lapse with no runner alive.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(runner_proc.pid, signal.SIGKILL)
    runner_proc.wait(timeout=BOUND_S)
    remaining = dispatched_at + AGENT_TIMEOUT_S + 0.3 - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)

    env.driver = _StoreDriver(tmp_path / "sdd")  # restarted owner replays the phase-1 journal
    sp = env.spawner(_Plan(mode="block"))
    resumed_at = time.monotonic()
    run = env.start(lambda: env.runner(sp).resume(load_workflow_spec_from_text(spec_text), run_id=rid))
    assert run.wait(BOUND_S), "resume did not return within the harness bound"
    elapsed = time.monotonic() - resumed_at
    run.raise_unexpected()
    assert sp.spawn_count() == 1, f"resume dispatched the agent again: {sp.spawned_task_ids()}"
    assert elapsed < AGENT_TIMEOUT_S * 0.75, f"resume waited {elapsed:.2f}s: the timeout budget restarted"
    produce = run.node("produce")
    assert produce is not None
    assert produce.status is not NodeStatus.SUCCESS
    assert env.sentinel_writes() == 0
    assert not any(_pid_alive(pid) for pid in env.logged_children()), "expired attempt was not cancelled"


# ---------------------------------------------------------------------------
# Matrix 4 - cancellation, acknowledged or not, is never success or retry
# ---------------------------------------------------------------------------


def _cancel_acknowledged(
    sp: _LifecycleSpawner, task: Task, session: AgentSession, proc: subprocess.Popen[bytes]
) -> None:
    sp.driver.call(lambda s: s.cancel_cascade(task.id, "operator cancel", cooperative=True))
    waiter = threading.Thread(target=proc.wait, daemon=True)  # reap the zombie so the group is observed gone
    waiter.start()
    receipt = reap_process_group(proc.pid, grace_seconds=0.5)
    waiter.join(timeout=BOUND_S)
    assert receipt.confirmed_dead, f"precondition: native reap acknowledged ({receipt})"
    sp.driver.call(lambda s: s.confirm_stop(task.id, "process group confirmed dead"))


def _cancel_unacknowledged(
    sp: _LifecycleSpawner, task: Task, session: AgentSession, proc: subprocess.Popen[bytes]
) -> None:
    sp.driver.call(lambda s: s.cancel_cascade(task.id, "operator cancel", cooperative=True))
    os.killpg(proc.pid, signal.SIGTERM)
    time.sleep(0.2)
    assert proc.poll() is None, "precondition: the agent ignored the stop (no acknowledgement)"


@pytest.mark.parametrize(
    ("ignore_sigterm", "cancel", "native_status"),
    [
        (False, _cancel_acknowledged, TaskStatus.CANCELLED),
        (True, _cancel_unacknowledged, TaskStatus.STOP_REQ),
    ],
    ids=["acknowledged", "unacknowledged"],
)
def test_cancelled_attempt_never_releases_or_retries(
    env: _Env, ignore_sigterm: bool, cancel: Callable[..., None], native_status: TaskStatus
) -> None:
    """[PROPOSED #4] Native cooperative cancellation never becomes success; an unknown ack never becomes retry."""
    rid = f"p4-{native_status.value}"
    spec = _spec(env)
    sp = env.spawner(_Plan(mode="block", ignore_sigterm=ignore_sigterm, after_launch=cancel))
    run = env.start(lambda: env.runner(sp).run(spec, run_id=rid))
    task = _observe_dispatched_attempt(env, sp, run)
    if ignore_sigterm:
        assert task.status in {TaskStatus.STOP_REQ, TaskStatus.CANCELLED}, "precondition: cancellation requested"
    else:
        assert task.status is TaskStatus.CANCELLED, "precondition: cancellation acknowledged"
    produce = run.node("produce")
    assert produce is None or produce.status is not NodeStatus.SUCCESS, (
        f"cancelled attempt reported as node success: {run.statuses()}"
    )
    assert env.sentinel_writes() == 0, "consumer released after cancellation"
    assert sp.spawn_count() == 1, "cancelled attempt was retried"


def test_pending_cancellation_keeps_run_resumable(env: _Env) -> None:
    """[PROPOSED #4] A returned run with no stop acknowledgement can resume without a new dispatch."""
    rid = "p4-pending-resume"
    spec = _spec(env)
    sp = env.spawner(_Plan(mode="block", ignore_sigterm=True, after_launch=_cancel_unacknowledged))
    run = env.start(lambda: env.runner(sp).run(spec, run_id=rid))
    task = _observe_dispatched_attempt(env, sp, run)
    assert run.wait(BOUND_S), "run did not report the unresolved attempt"
    run.raise_unexpected()
    task = env.driver.task(task.id)
    assert task is not None and task.status is TaskStatus.STOP_REQ
    assert task.claimed_by_session == sp.sessions[0].id
    stop_version = task.version
    stop_summary = task.result_summary
    env.driver.restart()
    replayed = env.driver.task(task.id)
    assert replayed is not None and replayed.status is TaskStatus.STOP_REQ
    assert (replayed.version, replayed.result_summary) == (stop_version, stop_summary)
    assert sp.procs[0].poll() is None, "precondition: the controlled child never acknowledged the stop"
    assert run_complete_marker_exists(env.workdir, rid) is None, (
        "a pending cancellation cannot mark the workflow complete"
    )
    assert env.sentinel_writes() == 0
    produce = run.node("produce")
    assert produce is not None and produce.status is not NodeStatus.SUCCESS
    again = env.spawner(_Plan(mode="block"))
    resumed = env.start(lambda: env.runner(again).resume(spec, run_id=rid))
    assert resumed.wait(BOUND_S), "resume did not report the unresolved attempt"
    resumed.raise_unexpected()
    assert resumed.result is not None
    produce = resumed.node("produce")
    assert produce is not None and produce.status is NodeStatus.FAILED
    assert produce.error.startswith("attempt_unresolved:")
    assert run_complete_marker_exists(env.workdir, rid) is None
    assert again.spawned_task_ids() == [task.id], "resume re-dispatched a cancelled attempt"
    assert env.sentinel_writes() == 0


# ---------------------------------------------------------------------------
# Matrix 5 - resume reconciles the same attempt without respawn
# ---------------------------------------------------------------------------


def test_resume_after_crash_before_dispatch_dispatches_once(env: _Env) -> None:
    """[PROPOSED #5, already met] Known not-started: a crash before dispatch lets resume dispatch exactly once."""
    rid = "p5-before"
    spec = _spec(env)
    crashing = env.spawner(_Plan(mode="block"))
    first = env.start(
        lambda: env.runner(crashing, audit=_crash_on("workflow.node_start", "produce")).run(spec, run_id=rid)
    )
    assert first.wait(BOUND_S)
    assert isinstance(first.error, _SimulatedCrash)
    assert crashing.spawn_count() == 0, "precondition: nothing dispatched before the crash"

    env.driver.restart()
    sp = env.spawner(_Plan(mode="block"))
    run = env.start(lambda: env.runner(sp).resume(spec, run_id=rid))
    _observe(env, sp, run)
    assert sp.spawned_task_ids() == [f"wf-produce-{rid}"], "resume must dispatch the not-started attempt once"


@pytest.mark.parametrize(
    "window",
    ["lost_dispatch_ack", "before_terminal_checkpoint"],
)
def test_resume_after_crash_around_dispatch_does_not_respawn(env: _Env, window: str) -> None:
    """[PROPOSED #5] A crash after launch and before a terminal checkpoint never duplicates the agent."""
    rid = f"p5-{window.replace('_', '-')}"
    spec = _spec(env)
    if window == "lost_dispatch_ack":
        crashing = env.spawner(_Plan(mode="block", crash_after_launch=True))
        audit = None
    else:
        crashing = env.spawner(_Plan(mode="block"))
        audit = _crash_on("workflow.node_finish", "produce")
    first = env.start(lambda: env.runner(crashing, audit=audit).run(spec, run_id=rid))
    assert first.wait(BOUND_S + AGENT_TIMEOUT_S)
    assert isinstance(first.error, _SimulatedCrash), f"precondition: injected crash fired ({first.statuses()})"
    assert crashing.spawned_task_ids() == [f"wf-produce-{rid}"]
    assert len(crashing.procs) == 1 and crashing.procs[0].poll() is None
    assert not (env.workdir / "runs" / rid / "produce.node.json").exists()
    assert env.sentinel_writes() == 0

    env.driver.restart()  # the store owner restarts too; it replays the journal
    task = env.driver.task(f"wf-produce-{rid}")
    assert task is not None
    assert task.status is TaskStatus.CLAIMED, "native evidence that dispatch happened survives the restart"

    sp = env.spawner(_Plan(mode="block"))
    run = env.start(lambda: env.runner(sp).resume(spec, run_id=rid))
    _wait_for(lambda: sp.spawn_count() >= 2 or run.done(), "respawn or resume end", timeout=BOUND_S)
    run.wait(OBSERVE_S)
    run.raise_unexpected()
    assert sp.spawn_count() == 1, f"resume respawned the in-flight attempt: {sp.spawned_task_ids()}"
    produce = run.node("produce")
    assert produce is None or produce.status is not NodeStatus.SUCCESS
    assert env.sentinel_writes() == 0


# ---------------------------------------------------------------------------
# Matrix 6 - stale, foreign or tampered evidence never satisfies acceptance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evidence",
    [
        "closed_task_other_run",
        "closed_task_other_node",
        "closed_same_task_id_foreign_tenant",
        "closed_same_task_id_stale_attempt",
        "tampered_artifact_receipt_same_task_id",
    ],
)
def test_stale_or_foreign_evidence_cannot_release_dependents(env: _Env, tmp_path: Path, evidence: str) -> None:
    """[PROPOSED #6] Evidence for another run/node/tenant/attempt, or tampered evidence, authorizes nothing."""
    rid = "p6-foreign"
    ours = f"wf-produce-{rid}"
    if evidence == "closed_task_other_run":
        env.driver.seed_closed("wf-produce-otherrun")
    elif evidence == "closed_task_other_node":
        env.driver.seed_closed(f"wf-elsewhere-{rid}")
    elif evidence == "closed_same_task_id_foreign_tenant":
        env.driver.seed_closed(ours, tenant="tenant-b")
    elif evidence == "closed_same_task_id_stale_attempt":
        env.driver.seed_closed(ours)
    else:
        paths = _record_report(env.workdir / ".sdd", ours, "report body v1")
        _tamper(paths, ours)
        assert not _verify_report(paths, ours).ok, "precondition: native verifier rejects the tampered blob"

    sp = env.spawner(_Plan(mode="block"))
    run = env.start(lambda: env.runner(sp).run(_spec(env), run_id=rid))
    _observe(env, sp, run)
    if sp.procs:
        assert sp.procs[0].poll() is None, "precondition: the current attempt is live and unaccepted"
    assert env.sentinel_writes() == 0, f"consumer released on {evidence}; runner reported {run.statuses()}"


# ---------------------------------------------------------------------------
# Matrix 7 - native artifact acceptance is required
# ---------------------------------------------------------------------------


def test_native_artifact_rejection_blocks_dependents(env: _Env) -> None:
    """[PROPOSED #7] A natively rejected artifact (verify_artifact fails, janitor reopens) releases nothing."""
    verdicts: list[Any] = []

    def rejected(sp: _LifecycleSpawner, task: Task, session: AgentSession, proc: subprocess.Popen[bytes]) -> None:
        assert _wait_exit(proc) == 0
        paths = _record_report(env.workdir / ".sdd", task.id, "report body")
        _tamper(paths, task.id)
        verdict = _verify_report(paths, task.id)
        verdicts.append(verdict)
        sp.driver.call(lambda s: s.complete(task.id, "agent reported DONE"))
        sp.driver.call(lambda s: s.reopen(task.id, f"artifact verification failed: {verdict.failures[:1]}"))

    sp = env.spawner(_Plan(mode="0", after_launch=rejected))
    run = env.start(lambda: env.runner(sp).run(_spec(env), run_id="p7-reject"))
    task = _observe_dispatched_attempt(env, sp, run)
    assert verdicts and verdicts[0].ok is False, "precondition: native artifact verifier rejected the output"
    assert task.status is TaskStatus.OPEN, "precondition: verification rejection reopened the task"
    assert env.sentinel_writes() == 0, f"consumer released on a rejected artifact; runner reported {run.statuses()}"


def test_verified_artifact_and_closed_task_without_attempt_acceptance_block_dependents(env: _Env) -> None:
    """[PROPOSED #7] A valid receipt and CLOSED task do not establish attempt-bound acceptance."""
    verified_task_ids: list[str] = []

    def close_without_acceptance(
        sp: _LifecycleSpawner, task: Task, session: AgentSession, proc: subprocess.Popen[bytes]
    ) -> None:
        assert _wait_exit(proc) == 0
        paths = _record_report(env.workdir / ".sdd", task.id, "report body")
        assert _verify_report(paths, task.id).ok
        verified_task_ids.append(task.id)
        sp.driver.call(lambda s: s.complete(task.id, "agent reported DONE"))
        sp.driver.call(lambda s: s.close(task.id))

    sp = env.spawner(_Plan(mode="0", after_launch=close_without_acceptance))
    run = env.start(lambda: env.runner(sp).run(_spec(env), run_id="p7-unbound"))
    task = _observe_dispatched_attempt(env, sp, run)
    assert verified_task_ids == [task.id], "precondition: native artifact verification passed for the current task"
    assert task.status is TaskStatus.CLOSED and task.closed_at is not None
    assert task.claimed_by_session == sp.sessions[0].id
    assert env.sentinel_writes() == 0, (
        f"consumer released on a verified receipt and CLOSED without attempt acceptance; runner reported {run.statuses()}"
    )


# ---------------------------------------------------------------------------
# Matrix 8 - agent loops advance only after acceptance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reported_done", [False, True], ids=["live", "reported-done"])
def test_agent_loop_waits_for_the_current_attempt(env: _Env, reported_done: bool) -> None:
    """[PROPOSED #8] No predicate evaluation and no next iteration while the current attempt is unresolved."""
    predicate_log = env.workdir / "predicate.log"
    flag = env.workdir / "loop.done"
    loop = (
        "    fresh_context: true\n"
        "    loop:\n"
        f'      until: "printf p >> {shlex.quote(str(predicate_log))}; test -f {shlex.quote(str(flag))}"\n'
        "      max_iterations: 3"
    )

    def report_done(sp: _LifecycleSpawner, task: Task, session: AgentSession, proc: subprocess.Popen[bytes]) -> None:
        assert _wait_exit(proc) == 0
        sp.driver.call(lambda s: s.complete(task.id, "agent reported DONE"))

    plan = _Plan(mode="0", after_launch=report_done) if reported_done else _Plan(mode="block")
    sp = env.spawner(plan)
    rid = "p8-done" if reported_done else "p8-live"
    run = env.start(lambda: env.runner(sp).run(_spec(env, produce_extra=loop), run_id=rid))
    task = _observe_dispatched_attempt(env, sp, run)
    assert task.claimed_by_session == sp.sessions[0].id
    assert task.closed_at is None
    if reported_done:
        assert task.status is TaskStatus.DONE and sp.procs[0].poll() == 0
    else:
        assert task.status is TaskStatus.CLAIMED and sp.procs[0].poll() is None
    predicate_runs = len(predicate_log.read_text(encoding="utf-8")) if predicate_log.exists() else 0
    assert (predicate_runs, sp.spawn_count()) == (0, 1), (
        f"loop advanced on an unresolved attempt: {predicate_runs} predicate runs, "
        f"{sp.spawn_count()} dispatches {sp.spawned_task_ids()}"
    )
    assert env.sentinel_writes() == 0


# ---------------------------------------------------------------------------
# Matrix 9 - legacy checkpoints
# ---------------------------------------------------------------------------


def _write_v1_checkpoint(
    e: _Env,
    rid: str,
    spec: Any,
    *,
    status: str = "success",
    session_id: str = "",
    iterations: int = 0,
    exit_code: int | None = None,
    condition_skipped: bool = False,
) -> None:
    """Seed the original on-disk format independently of the current writer."""
    digest = spec_digest(spec)
    state_dir = e.workdir / "runs" / rid
    state_dir.mkdir(parents=True)
    snapshot = {
        "version": 1,
        "spec_name": spec.name,
        "spec_version": spec.version,
        "spec_digest": digest,
        "node_ids": ["produce", "consume"],
        "source": spec.name,
    }
    checkpoint = {
        "version": 1,
        "spec_digest": digest,
        "node_id": "produce",
        "status": status,
        "iterations": iterations,
        "exit_code": exit_code,
        "stdout": "",
        "stderr": "",
        "session_id": session_id,
        "error": "",
        "wall_time_seconds": 0.0,
        "condition_skipped": condition_skipped,
    }
    (state_dir / "spec_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (state_dir / "produce.node.json").write_text(json.dumps(checkpoint), encoding="utf-8")


def test_legacy_unproven_agent_success_checkpoint_is_not_trusted(env: _Env) -> None:
    """[PROPOSED #9] A v1 agent 'success' with only a session id needs reconciliation: no release, no blind respawn."""
    rid = "p9-legacy-agent"
    spec = _spec(env)
    _write_v1_checkpoint(env, rid, spec, session_id="backend-1e9ac700", iterations=1)
    sp = env.spawner(_Plan(mode="block"))
    run = env.start(lambda: env.runner(sp).resume(spec, run_id=rid))
    run.wait(OBSERVE_S)
    run.raise_unexpected()
    assert sp.spawn_count() == 0, "legacy attempt was re-dispatched blindly"
    assert env.sentinel_writes() == 0, f"unproven legacy agent success released the consumer: {run.statuses()}"


def test_legacy_command_checkpoint_resume_is_unchanged(env: _Env) -> None:
    """[EXISTING] A v1 command success is trusted on resume: not re-run, and its consumer runs once."""
    rid = "p9-legacy-cmd"
    producer_log = env.workdir / "producer.log"
    text = _spec_text(env.sentinel).replace(
        'agent: backend\n    prompt: "Produce the artifact for {goal}"',
        f'command: "printf c >> {shlex.quote(str(producer_log))}"',
    )
    spec = load_workflow_spec_from_text(text)
    _write_v1_checkpoint(env, rid, spec, iterations=1, exit_code=0)
    execution = WorkflowRunner(workdir=env.workdir).resume(spec, run_id=rid)
    assert execution.succeeded is True
    assert not producer_log.exists()
    assert env.sentinel_writes() == 1


def test_legacy_condition_skip_checkpoint_resume_is_unchanged(env: _Env) -> None:
    """[EXISTING] A v1 intentional (``when``) skip still unblocks its consumer on resume."""
    rid = "p9-legacy-skip"
    predicate_log = env.workdir / "predicate.log"
    spec = _spec(env, produce_extra=f'    when: "printf c >> {shlex.quote(str(predicate_log))}; false"')
    _write_v1_checkpoint(env, rid, spec, status="skipped", condition_skipped=True)
    execution = WorkflowRunner(workdir=env.workdir).resume(spec, run_id=rid)
    assert execution.succeeded is True
    assert not predicate_log.exists(), "resuming a persisted skip re-evaluated the predicate"
    assert env.sentinel_writes() == 1


# ---------------------------------------------------------------------------
# Native facts the review decisions rely on
# ---------------------------------------------------------------------------


def test_native_task_store_replay_does_not_preserve_attempt_identity(tmp_path: Path) -> None:
    """[NATIVE FACT] Decision D1: attempt identity cannot be re-derived from a TaskStore restart,
    and create-by-id silently replaces an existing record (lookup before create)."""
    driver = _StoreDriver(tmp_path / "sdd")
    try:
        driver.seed_closed("wf-produce-nf", session="backend-aaaa0001")
        live = driver.task("wf-produce-nf")
        assert live is not None
        assert (live.status, live.claimed_by_session) == (TaskStatus.CLOSED, "backend-aaaa0001")
        assert live.closed_at is not None
        live_version = live.version

        driver.restart()
        replayed = driver.task("wf-produce-nf")
        assert replayed is not None
        assert replayed.status is TaskStatus.CLOSED
        assert replayed.claimed_by_session is None, "session binding is not restored by replay"
        assert replayed.closed_at is None, "closure time is not restored by replay"
        assert replayed.version == live_version, "CAS version is restored by replay"

        driver.call(lambda s: s.create(TaskCreate(id="wf-produce-nf", title="again", description="d", role="backend")))
        again = driver.task("wf-produce-nf")
        assert again is not None
        assert again.status is TaskStatus.OPEN, "duplicate create replaced the closed record"
    finally:
        driver.close()


def test_native_stop_replay_does_not_preserve_claim_session(env: _Env) -> None:
    """[NATIVE FACT] Replay restores the stop request without its recorded claiming session."""
    sp = env.spawner(_Plan(mode="block", ignore_sigterm=True, after_launch=_cancel_unacknowledged))
    task_id = "wf-produce-stop-replay"
    session = sp.spawn_for_tasks([Task(id=task_id, title="produce", description="produce", role="backend")])
    task = env.driver.task(task_id)
    assert task is not None and task.status is TaskStatus.STOP_REQ
    assert task.claimed_by_session == session.id

    env.driver.restart()
    replayed = env.driver.task(task_id)
    assert replayed is not None and replayed.status is TaskStatus.STOP_REQ
    assert replayed.claimed_by_session is None
    assert sp.procs[0].poll() is None


def test_native_artifact_receipt_binds_task_and_hash_only(tmp_path: Path) -> None:
    """[NATIVE FACT] Decision D2: the artifact receipt verifies content against signed lineage for a
    task id, but carries no attempt, session, run or tenant binding."""
    paths = _record_report(tmp_path, "wf-produce-nf", "report body")
    assert _verify_report(paths, "wf-produce-nf").ok
    receipt = load_receipt(paths["sink"], "wf-produce-nf")
    assert receipt is not None
    keys = set(receipt.to_dict())
    assert keys == {
        "task_id",
        "kind",
        "content_hash",
        "entry_hash",
        "artefact_path",
        "agent_id",
        "agent_card_kid",
        "ts_ns",
    }
    assert not keys & {"attempt", "attempt_id", "session_id", "run_id", "tenant_id"}
    _tamper(paths, "wf-produce-nf")
    assert not _verify_report(paths, "wf-produce-nf").ok
