"""Workflow completion characterization for #6639; runtime changes remain pending."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable

import pytest

from bernstein.core.tasks.contracts import RefusalKind, WorkerRefusal
from bernstein.core.tasks.models import AgentSession, Task, TaskStatus
from bernstein.core.workflows import NodeStatus, WorkflowRunner
from tests.support.workflow_completion import (
    BOUND_S,
    _Env,
    _known_contract_gap,
    _LifecycleSpawner,
    _observe_dispatched_attempt,
    _Plan,
    _proposed_gap,
    _spec,
    _wait_exit,
    assert_harness_sentinel_dispatcher_and_reaper_are_sound,
)
from tests.support.workflow_completion import (
    env as env,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group child fixtures")


def test_harness_sentinel_dispatcher_and_reaper_are_sound(env: _Env) -> None:
    """[EXISTING] Detect running children, sentinel writes and process cleanup."""
    assert_harness_sentinel_dispatcher_and_reaper_are_sound(env)


@_proposed_gap
def test_live_claimed_attempt_does_not_release_dependents(env: _Env) -> None:
    """[PROPOSED #1] A returned session id for a live, claimed agent is not completion."""
    sp = env.spawner(_Plan(mode="block"))
    run = env.start(lambda: env.runner(sp).run(_spec(env), run_id="p1-live"))
    task = _observe_dispatched_attempt(env, sp, run)
    assert sp.procs[0].poll() is None, "precondition: the agent child is still running"
    assert task.status is TaskStatus.CLAIMED
    assert task.claimed_by_session == sp.sessions[0].id
    with _known_contract_gap():
        assert env.sentinel_writes() == 0, (
            f"consumer ran while the agent attempt was still live and claimed; runner reported {run.statuses()}"
        )


@_proposed_gap
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
    with _known_contract_gap():
        assert env.sentinel_writes() == 0, (
            f"consumer ran on exit 0 + reported DONE with no acceptance; runner reported {run.statuses()}"
        )


def test_closed_producer_runs_its_dependent_once(env: _Env) -> None:
    """Control for the sentinel fixture when its producer is already closed at dispatch return."""

    def close_before_return(
        sp: _LifecycleSpawner, task: Task, session: AgentSession, proc: subprocess.Popen[bytes]
    ) -> None:
        assert _wait_exit(proc) == 0
        sp.driver.call(lambda store: store.complete(task.id, "worker finished"))
        sp.driver.call(lambda store: store.close(task.id))

    sp = env.spawner(_Plan(mode="0", after_launch=close_before_return))
    run = env.start(lambda: env.runner(sp).run(_spec(env), run_id="closed-producer"))
    task = _observe_dispatched_attempt(env, sp, run)
    assert task.closed_at is not None
    assert run.wait(BOUND_S)
    run.raise_unexpected()
    assert env.sentinel_writes() == 1
    assert sp.spawn_count() == 1


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
@_proposed_gap
def test_failed_refused_or_rejected_attempt_never_releases_dependents(
    env: _Env, mode: str, outcome: Callable[..., None], native_status: TaskStatus
) -> None:
    """[PROPOSED #2] Failure, refusal and rejected verification neither release nor auto-retry."""
    sp = env.spawner(_Plan(mode=mode, after_launch=outcome))
    run = env.start(lambda: env.runner(sp).run(_spec(env), run_id=f"p2-{native_status.value}"))
    task = _observe_dispatched_attempt(env, sp, run)
    assert task.status is native_status, "precondition: native lifecycle outcome recorded"
    produce = run.node("produce")
    with _known_contract_gap():
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


@_proposed_gap
def test_missing_completion_authority_blocks_before_dispatch(env: _Env) -> None:
    """[PROPOSED] A configured spawner alone cannot authorize agent dispatch."""
    spawner = env.spawner(_Plan(mode="block"))
    # Construct the runner directly so this case still lacks authority when
    # the shared contract fixture starts supplying one.
    runner = WorkflowRunner(spawner=spawner, workdir=env.workdir)  # type: ignore[arg-type]
    run = env.start(lambda: runner.run(_spec(env), run_id="no-authority"))
    assert run.wait(BOUND_S), "runner did not return without completion authority"
    run.raise_unexpected()
    with _known_contract_gap():
        assert spawner.spawn_count() == 0, "agent dispatched without completion authority"
    assert env.sentinel_writes() == 0
    produce = run.node("produce")
    consume = run.node("consume")
    assert produce is not None and produce.status is NodeStatus.FAILED
    assert consume is not None and consume.status is NodeStatus.SKIPPED
