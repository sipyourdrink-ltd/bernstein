"""Workflow completion characterization for #6639; runtime changes remain pending."""

from __future__ import annotations

import sys

import pytest

from bernstein.core.tasks.models import TaskStatus
from bernstein.core.workflows import NodeStatus
from tests.support.workflow_completion import (
    _CHILD,
    AGENT_TIMEOUT_S,
    BOUND_S,
    OBSERVE_S,
    _crash_on,
    _Env,
    _known_contract_gap,
    _observe_dispatched_attempt,
    _Plan,
    _proposed_gap,
    _ProposedContractGap,
    _SimulatedCrash,
    _spec,
    _wait_for,
    assert_harness_sentinel_dispatcher_and_reaper_are_sound,
)
from tests.support.workflow_completion import (
    env as env,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group child fixtures")


def test_harness_sentinel_dispatcher_and_reaper_are_sound(env: _Env) -> None:
    """[EXISTING] Detect running children, sentinel writes and process cleanup."""
    assert_harness_sentinel_dispatcher_and_reaper_are_sound(env)


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
    task = _observe_dispatched_attempt(env, sp, run)
    assert task.status is TaskStatus.CLAIMED, "precondition: resumed dispatcher claimed the real task"
    assert len(sp.procs) == 1 and sp.procs[0].poll() is None, "precondition: resumed child is live"
    assert sp.spawned_task_ids() == [f"wf-produce-{rid}"], "resume must dispatch the not-started attempt once"


@pytest.mark.parametrize(
    "window",
    ["lost_dispatch_ack", "before_terminal_checkpoint"],
)
@_proposed_gap
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
    _wait_for(
        lambda: sp.first_dispatch_returning.is_set() or run.done(),
        "resumed dispatch return or resume end",
        timeout=BOUND_S,
    )
    run.wait(OBSERVE_S)
    run.raise_unexpected()
    # The counter logs a spawn invocation before Popen. A fixture launch
    # failure is an ordinary failure, even if the runner turns it into a
    # FAILED node, and must never count as the proposed duplicate gap.
    if sp.spawn_count() > 1:
        assert sp.first_dispatch_returning.is_set(), "precondition: resumed dispatcher actually returned"
        assert len(sp.procs) == 1 and sp.procs[0].poll() is None, "precondition: resumed child was launched"
        assert sp.sessions, "precondition: resumed dispatcher returned a real child session"
    with _known_contract_gap():
        assert sp.spawn_count() == 1, f"resume respawned the in-flight attempt: {sp.spawned_task_ids()}"
    produce = run.node("produce")
    assert produce is None or produce.status is not NodeStatus.SUCCESS
    assert env.sentinel_writes() == 0


@pytest.mark.parametrize("window", ["lost_dispatch_ack", "before_terminal_checkpoint"])
def test_resume_observes_slow_child_before_contract_gap(
    env: _Env, monkeypatch: pytest.MonkeyPatch, window: str
) -> None:
    """[EXISTING] Slow successful launches reach the proposed gap after dispatch returns."""
    monkeypatch.setattr(
        "tests.support.workflow_completion._CHILD",
        f"import time\ntime.sleep({2 * OBSERVE_S})\n{_CHILD}",
    )
    with pytest.raises(_ProposedContractGap, match="resume respawned the in-flight attempt"):
        test_resume_after_crash_around_dispatch_does_not_respawn(env, window)
