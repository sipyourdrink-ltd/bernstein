"""Terminal state for a quiescent run that produced zero terminal tasks (#3010).

Ground truth: a single-task run whose only agent produced no model output
and was reaped left its task non-terminal. The tick loop's only self-stop is
gated on ``done or failed`` being non-empty, so that gate was never
satisfied, ``_running`` was never cleared, and the orchestrator idled while
the run reported HEALTHY and exit 0.

The end-to-end test below drives real ``tick()`` calls against a task server
frozen in exactly that state. It is bounded by a fixed tick budget rather
than a timeout, so on the pre-fix tree it fails fast instead of hanging.

The unit tests around it pin the criterion itself: which way it errs, and
every input that must suppress the stop.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest
from bernstein.core.models import AgentSession, OrchestratorConfig, Task
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.orchestration.run_stall import (
    RunStallState,
    evaluate_progress_stall,
    evaluate_run_stall,
    resolve_grace_s,
    resolve_min_ticks,
    task_state_fingerprint,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

# Ticks the end-to-end test is willing to spend before calling the loop
# stuck. Ten is comfortably above the three no-progress ticks the test
# configures and far below anything that would feel like a hang.
_TICK_BUDGET = 10


# --------------------------------------------------------------------------
# Snapshot helpers for the pure-criterion tests
# --------------------------------------------------------------------------


def _task(task_id: str, status: str, *, created_at: float | None = None) -> Task:
    return Task.from_dict(
        {
            "id": task_id,
            "title": f"task {task_id}",
            "description": "d",
            "role": "manager",
            "status": status,
            # Default sits before the synthetic clock the pure tests drive,
            # so an "open" task reads as claimable rather than backing off.
            "created_at": created_at if created_at is not None else 0.0,
        }
    )


def _snapshot(**buckets: list[Task]) -> dict[str, list[Task]]:
    """Build a ``fetch_all_tasks``-shaped snapshot with the standard buckets."""
    base: dict[str, list[Task]] = {"open": [], "claimed": [], "done": [], "failed": []}
    base.update(buckets)
    return base


def _drive(
    snapshot: dict[str, list[Task]],
    *,
    ticks: int,
    grace_s: float,
    min_ticks: int,
    start: float = 1_000.0,
    step_s: float = 10.0,
) -> tuple[RunStallState, Any]:
    """Evaluate the same snapshot ``ticks`` times on a synthetic clock."""
    state = RunStallState()
    verdict = None
    for i in range(ticks):
        state, verdict = evaluate_run_stall(
            state,
            snapshot,
            now=start + i * step_s,
            grace_s=grace_s,
            min_ticks=min_ticks,
        )
    return state, verdict


def _drive_progress(
    snapshot: dict[str, list[Task]],
    *,
    ticks: int,
    grace_s: float,
    min_ticks: int,
    active_agents: int = 0,
    start: float = 1_000.0,
    step_s: float = 10.0,
) -> tuple[RunStallState, Any]:
    """Evaluate the same snapshot ``ticks`` times on a synthetic clock."""
    state = RunStallState()
    verdict = None
    for i in range(ticks):
        state, verdict = evaluate_progress_stall(
            state,
            snapshot,
            active_agents=active_agents,
            now=start + i * step_s,
            grace_s=grace_s,
            min_ticks=min_ticks,
        )
    return state, verdict


# --------------------------------------------------------------------------
# The criterion
# --------------------------------------------------------------------------


class TestStallCriterion:
    def test_stuck_claimed_task_stalls_once_both_thresholds_are_met(self) -> None:
        """The #3010 shape: one claimed task, no agent, nothing moving."""
        snapshot = _snapshot(claimed=[_task("T-1", "claimed")])

        _state, verdict = _drive(snapshot, ticks=6, grace_s=30.0, min_ticks=3)

        assert verdict is not None
        assert verdict.stalled is True
        assert verdict.stuck_task_ids == ("T-1",)
        assert "no terminal task" in verdict.reason

    def test_tick_floor_alone_is_not_enough(self) -> None:
        """Ticks satisfied, grace not: a clock that has barely moved must not stop a run."""
        snapshot = _snapshot(claimed=[_task("T-1", "claimed")])

        _state, verdict = _drive(snapshot, ticks=8, grace_s=600.0, min_ticks=3, step_s=1.0)

        assert verdict is not None
        assert verdict.stalled is False
        assert "grace window" in verdict.reason

    def test_grace_alone_is_not_enough(self) -> None:
        """Grace satisfied on the second observation, tick floor not.

        Guards the wall-clock test against a single time discontinuity: an
        NTP step or a resumed container must not be able to end a run by
        itself.
        """
        snapshot = _snapshot(claimed=[_task("T-1", "claimed")])

        _state, verdict = _drive(snapshot, ticks=2, grace_s=1.0, min_ticks=5, step_s=10_000.0)

        assert verdict is not None
        assert verdict.stalled is False
        assert "need 5" in verdict.reason

    def test_empty_backlog_never_stalls(self) -> None:
        """Startup before the seed task is ingested is not a dead run.

        This is the case the original zero-terminal guard was written for,
        and it must keep working.
        """
        _state, verdict = _drive(_snapshot(), ticks=50, grace_s=0.0, min_ticks=1)

        assert verdict is not None
        assert verdict.stalled is False
        assert "startup window" in verdict.reason

    def test_progress_resets_the_window(self) -> None:
        """A task moving between statuses is forward motion, not a stall."""
        state = RunStallState()
        moving = _snapshot(open=[_task("T-1", "open")])
        settled = _snapshot(claimed=[_task("T-1", "claimed")])

        for i in range(9):
            state, verdict = evaluate_run_stall(state, moving, now=1000.0 + i, grace_s=2.0, min_ticks=3)
        assert verdict.stalled is True, "control: the unchanged snapshot does stall"

        # Same task, new status -> the window restarts from scratch.
        state, verdict = evaluate_run_stall(state, settled, now=2000.0, grace_s=2.0, min_ticks=3)
        assert verdict.stalled is False
        assert verdict.observed_ticks == 1
        assert "progress observed" in verdict.reason

    def test_retry_backoff_suppresses_the_stall(self) -> None:
        """An open task in retry backoff is scheduled work, not a stall.

        ``open_tasks`` filters out tasks whose ``created_at`` is in the
        future, so a backing-off retry looks exactly like quiescence from
        the tick's point of view.
        """
        deferred = _snapshot(open=[_task("T-1", "open", created_at=5_000.0)])

        _state, verdict = _drive(deferred, ticks=20, grace_s=0.0, min_ticks=1, start=1_000.0)

        assert verdict is not None
        assert verdict.stalled is False
        assert "retry backoff" in verdict.reason

    @pytest.mark.parametrize(
        "parked_status",
        ["planned", "suspended", "pending_approval", "blocked", "waiting_for_subtasks"],
    )
    def test_deliberately_parked_tasks_never_stall(self, parked_status: str) -> None:
        """Waiting on a human or a dependency is by design, not a stall."""
        snapshot = _snapshot()
        snapshot[parked_status] = [_task("T-1", parked_status)]

        _state, verdict = _drive(snapshot, ticks=50, grace_s=0.0, min_ticks=1)

        assert verdict is not None
        assert verdict.stalled is False
        assert "parked or waiting by design" in verdict.reason

    def test_fingerprint_is_order_independent(self) -> None:
        a = _snapshot(claimed=[_task("T-1", "claimed"), _task("T-2", "claimed")])
        b = _snapshot(claimed=[_task("T-2", "claimed"), _task("T-1", "claimed")])

        assert task_state_fingerprint(a) == task_state_fingerprint(b)

    def test_env_vars_take_precedence_over_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_STALLED_RUN_GRACE_S", "5")
        monkeypatch.setenv("BERNSTEIN_STALLED_RUN_TICKS", "2")

        assert resolve_grace_s(1800.0) == 5.0
        assert resolve_min_ticks(10) == 2

    def test_unparseable_env_falls_back_to_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_STALLED_RUN_GRACE_S", "not-a-number")
        monkeypatch.setenv("BERNSTEIN_STALLED_RUN_TICKS", "not-a-number")

        assert resolve_grace_s(1800.0) == 1800.0
        assert resolve_min_ticks(10) == 10

    def test_default_grace_outlives_the_stale_claim_release(self) -> None:
        """The stale-claim release must always get its chance first.

        Its outcome is strictly more informative than the backstop's: it
        produces a real failed task carrying a reason. The backstop only
        exists for the shapes it cannot reach.
        """
        from bernstein.core.defaults import ORCHESTRATOR

        assert ORCHESTRATOR.stalled_run_grace_s > ORCHESTRATOR.stale_claim_timeout_s


# --------------------------------------------------------------------------
# The progress-stall criterion (#4453)
# --------------------------------------------------------------------------


class TestProgressStallCriterion:
    def test_progress_stall_fires_when_claimed_wedged_with_done_tasks(self) -> None:
        """The #4453 shape: done>0, a claimed task, no agent, nothing moving."""
        snapshot = _snapshot(done=[_task("T-1", "done")], claimed=[_task("T-2", "claimed")])

        _state, verdict = _drive_progress(snapshot, ticks=6, grace_s=30.0, min_ticks=3)

        assert verdict is not None
        assert verdict.stalled is True
        assert verdict.stuck_task_ids == ("T-2",)
        assert "wedged" in verdict.reason
        assert "1 terminal task(s)" in verdict.reason

    def test_progress_stall_requires_zero_agents(self) -> None:
        """A live agent is progress by definition, even with a claimed task."""
        snapshot = _snapshot(done=[_task("T-1", "done")], claimed=[_task("T-2", "claimed")])

        _state, verdict = _drive_progress(snapshot, ticks=6, grace_s=30.0, min_ticks=3, active_agents=1)

        assert verdict is not None
        assert verdict.stalled is False
        assert "live agent" in verdict.reason

    def test_progress_stall_requires_at_least_one_claimed(self) -> None:
        """No claimed task means nothing is wedged on a dead agent."""
        snapshot = _snapshot(done=[_task("T-1", "done")], open=[_task("T-2", "open")])

        _state, verdict = _drive_progress(snapshot, ticks=6, grace_s=30.0, min_ticks=3)

        assert verdict is not None
        assert verdict.stalled is False
        assert "no claimed task" in verdict.reason

    def test_progress_stall_requires_at_least_one_terminal(self) -> None:
        """Zero terminal tasks is the zero-terminal stall, not this one."""
        snapshot = _snapshot(claimed=[_task("T-1", "claimed")])

        _state, verdict = _drive_progress(snapshot, ticks=6, grace_s=30.0, min_ticks=3)

        assert verdict is not None
        assert verdict.stalled is False
        assert "no terminal task" in verdict.reason

    def test_progress_stall_grace_window_must_elapse(self) -> None:
        """Ticks satisfied, grace not: a barely-moved clock must not stop a run."""
        snapshot = _snapshot(done=[_task("T-1", "done")], claimed=[_task("T-2", "claimed")])

        _state, verdict = _drive_progress(snapshot, ticks=8, grace_s=600.0, min_ticks=3, step_s=1.0)

        assert verdict is not None
        assert verdict.stalled is False
        assert "grace window" in verdict.reason

    def test_progress_stall_min_ticks_floor(self) -> None:
        """Grace satisfied on the second observation, tick floor not."""
        snapshot = _snapshot(done=[_task("T-1", "done")], claimed=[_task("T-2", "claimed")])

        _state, verdict = _drive_progress(snapshot, ticks=2, grace_s=1.0, min_ticks=5, step_s=10_000.0)

        assert verdict is not None
        assert verdict.stalled is False
        assert "need 5" in verdict.reason

    def test_progress_stall_retry_backoff_suppresses(self) -> None:
        """An open task in retry backoff is scheduled work, not a stall."""
        snapshot = _snapshot(
            done=[_task("T-1", "done")],
            claimed=[_task("T-2", "claimed")],
            open=[_task("T-3", "open", created_at=5_000.0)],
        )

        _state, verdict = _drive_progress(snapshot, ticks=20, grace_s=0.0, min_ticks=1, start=1_000.0)

        assert verdict is not None
        assert verdict.stalled is False
        assert "retry backoff" in verdict.reason

    def test_progress_stall_fingerprint_change_resets_window(self) -> None:
        """A task moving between statuses is forward motion, not a stall."""
        state = RunStallState()
        wedged = _snapshot(done=[_task("T-1", "done")], claimed=[_task("T-2", "claimed")])
        changed = _snapshot(
            done=[_task("T-1", "done")],
            claimed=[_task("T-2", "claimed"), _task("T-3", "claimed")],
        )

        for i in range(9):
            state, verdict = evaluate_progress_stall(
                state, wedged, active_agents=0, now=1000.0 + i, grace_s=2.0, min_ticks=3
            )
        assert verdict.stalled is True, "control: the unchanged snapshot does stall"

        # Same world plus a new claimed task -> the window restarts.
        state, verdict = evaluate_progress_stall(state, changed, active_agents=0, now=2000.0, grace_s=2.0, min_ticks=3)
        assert verdict.stalled is False
        assert verdict.observed_ticks == 1
        assert "progress observed" in verdict.reason


# --------------------------------------------------------------------------
# End-to-end: the tick loop actually reaches a terminal state
# --------------------------------------------------------------------------


def _mock_adapter(*, alive: bool = False) -> MagicMock:
    adapter = MagicMock(spec=CLIAdapter)
    adapter.spawn.return_value = SpawnResult(pid=42, log_path=Path("/tmp/test.log"))
    adapter.is_alive.return_value = alive
    adapter.is_rate_limited.return_value = False
    adapter.kill.return_value = None
    adapter.name.return_value = "MockCLI"
    return adapter


def _stuck_run_transport(task_id: str) -> tuple[httpx.MockTransport, dict[str, Any]]:
    """Serve a task server frozen in the #3010 end state.

    One task, claimed, no live agent, nothing else on the server. Exactly
    the snapshot that made ``open_tasks == active_agents == 0`` while
    ``done == failed == 0``.
    """
    state: dict[str, Any] = {
        "id": task_id,
        "title": "Create hello.txt",
        "description": "write one line",
        "role": "manager",
        "status": "claimed",
        "priority": 1,
        "created_at": time.time() - 120.0,
        # Fresh claim: the 15-minute stale-claim release must NOT be what
        # ends this run, or the test would be proving the wrong mechanism.
        "claimed_at": time.time(),
        "depends_on": [],
        "owned_files": [],
        "assigned_agent": "manager-dead",
        "result_summary": None,
        "task_type": "standard",
        "retry_count": 0,
        "max_retries": 3,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if request.method == "GET" and url.path == "/tasks":
            status = url.params.get("status")
            tasks = [dict(state)] if status in (None, state["status"]) else []
            return httpx.Response(200, json=tasks)
        if request.method == "GET" and url.path == f"/tasks/{task_id}":
            return httpx.Response(200, json=dict(state))
        if request.method == "GET" and url.path == "/orchestrator/holds":
            return httpx.Response(200, json={"holds": []})
        if request.method == "POST" and url.path == f"/tasks/{task_id}/fail":
            state["status"] = "failed"
            body = request.read().decode() or "{}"
            state["result_summary"] = body
            state["terminal_reason"] = body
            return httpx.Response(200, json={})
        return httpx.Response(200, json={})

    return httpx.MockTransport(handler), state


def _wrap_as_paginated(resp: httpx.Response) -> httpx.Response:
    tasks = resp.json()
    return httpx.Response(200, json={"tasks": tasks, "total": len(tasks)})


def _paginated(inner: httpx.MockTransport) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if request.method == "GET" and url.path == "/tasks" and "limit" in url.params:
            plain_params = {k: v for k, v in url.params.items() if k not in ("limit", "offset")}
            plain_url = url.copy_with(params=plain_params or {})
            plain = httpx.Request(request.method, plain_url, headers=request.headers)
            resp = inner.handle_request(plain)
            return _wrap_as_paginated(resp) if resp.status_code == 200 else resp
        return inner.handle_request(request)

    return httpx.MockTransport(handler)


def _build_orchestrator(tmp_path: Path, transport: httpx.MockTransport, *, agent_alive: bool = False) -> Orchestrator:
    cfg = OrchestratorConfig(
        max_agents=1,
        poll_interval_s=1,
        server_url="http://testserver",
        evolve_mode=False,
        evolution_enabled=False,
    )
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True)
    spawner = AgentSpawner(_mock_adapter(alive=agent_alive), templates_dir, tmp_path, default_model="mock-model")
    client = httpx.Client(transport=_paginated(transport), base_url="http://testserver")
    return Orchestrator(cfg, spawner, tmp_path, client=client)


@pytest.fixture
def fast_stall_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Collapse the real 30-minute window so the test runs in milliseconds."""
    monkeypatch.setenv("BERNSTEIN_QUIESCENCE_SETTLE_S", "0")
    monkeypatch.setenv("BERNSTEIN_STALLED_RUN_GRACE_S", "0")
    monkeypatch.setenv("BERNSTEIN_STALLED_RUN_TICKS", "3")
    yield


class TestZeroTerminalRunReachesATerminalState:
    def test_run_with_nothing_finished_stops_instead_of_idling(self, tmp_path: Path, fast_stall_env: None) -> None:
        """Regression for #3010: the loop must exit, not idle forever.

        Pre-fix this fails: ``_had_any_terminal_task`` is False on every
        tick, the self-stop block is skipped entirely, ``_running`` stays
        True, and the loop condition at the top of ``run()`` never becomes
        False. The tick budget bounds the failure so it reports rather than
        hangs.
        """
        transport, _state = _stuck_run_transport("t-3010")
        orch = _build_orchestrator(tmp_path, transport)
        orch._running = True

        ticks_used = 0
        for _ in range(_TICK_BUDGET):
            ticks_used += 1
            orch.tick()
            if not orch._running:
                break

        assert not orch._running, (
            f"orchestrator still running after {ticks_used} quiescent ticks with zero terminal "
            "tasks - the run has no terminal state and would idle until the container is torn down"
        )
        # And nothing is left alive to keep run()'s loop condition true.
        assert orch._has_active_agents() is False

    def test_the_stuck_task_is_reported_as_failed_not_evaporated(self, tmp_path: Path, fast_stall_env: None) -> None:
        """`Total tasks: 1` with `Done: 0 / Failed: 0` was the dishonest part.

        A task that will never run again must not be left frozen in
        ``claimed``; that is what let the tally read 0/0.
        """
        transport, state = _stuck_run_transport("t-3010")
        orch = _build_orchestrator(tmp_path, transport)
        orch._running = True

        for _ in range(_TICK_BUDGET):
            orch.tick()
            if not orch._running:
                break

        assert state["status"] == "failed"
        assert "did not meet its goal" in str(state["result_summary"])

    def test_final_retrospective_is_regenerated_and_not_healthy(self, tmp_path: Path, fast_stall_env: None) -> None:
        """The run's own report must say the goal was not met."""
        transport, _state = _stuck_run_transport("t-3010")
        orch = _build_orchestrator(tmp_path, transport)
        orch._running = True

        for _ in range(_TICK_BUDGET):
            orch.tick()
            if not orch._running:
                break

        assert orch._final_retrospective_regenerated is True
        retro = (tmp_path / ".sdd" / "runtime" / "retrospective.md").read_text()
        assert "INTERIM" not in retro, "the FINAL retrospective must not be labelled interim"
        assert "**Verdict:** UNHEALTHY" in retro
        assert "No issues detected; run looks healthy." not in retro

    def test_a_slow_start_is_not_terminated(self, tmp_path: Path, fast_stall_env: None) -> None:
        """The guard that matters most: do not kill a run that has not begun.

        Zero tasks on the server is the pre-ingest window, and it reaches
        quiescence with zero terminal tasks on every single tick. Even with
        the grace collapsed to zero it must never stop the run.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/tasks":
                return httpx.Response(200, json=[])
            if request.method == "GET" and request.url.path == "/orchestrator/holds":
                return httpx.Response(200, json={"holds": []})
            return httpx.Response(200, json={})

        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler))
        orch._running = True

        for _ in range(_TICK_BUDGET):
            orch.tick()

        assert orch._running is True, "an empty backlog is 'nothing scheduled yet', not a dead run"

    def test_a_working_agent_is_not_terminated(self, tmp_path: Path, fast_stall_env: None) -> None:
        """A long model call must not look like a stall.

        A live agent keeps ``active_agents > 0``, so the quiescence gate
        never opens and the backstop is never consulted. Pinned here
        because it is the failure mode that would be worse than the bug.
        """
        transport, _state = _stuck_run_transport("t-3010")
        orch = _build_orchestrator(tmp_path, transport, agent_alive=True)
        orch._running = True
        orch._agents["manager-alive"] = AgentSession(
            id="manager-alive",
            role="manager",
            pid=4242,
            task_ids=["t-3010"],
            status="working",
            spawn_ts=time.time(),
        )
        orch._task_to_session["t-3010"] = "manager-alive"

        for _ in range(_TICK_BUDGET):
            orch.tick()
            orch._agents["manager-alive"].status = "working"

        assert orch._running is True, "a run with a live agent must never be stopped as stalled"

    def test_an_active_hold_blocks_the_stop(self, tmp_path: Path, fast_stall_env: None) -> None:
        """A hold is an explicit external 'stay alive' and outranks the backstop.

        With immediate dead-claim reclaim (fix #4453), the claimed task is
        reclaimed via retry_or_fail_task *before* the stall check runs, so
        the task may be failed or retried. The hold still prevents the
        *run* from stopping.
        """
        transport, state = _stuck_run_transport("t-3010")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/orchestrator/holds":
                return httpx.Response(200, json={"holds": [{"reason": "dashboard review in progress"}]})
            return transport.handle_request(request)

        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler))
        orch._running = True

        for _ in range(_TICK_BUDGET):
            orch.tick()

        assert orch._running is True, "a held run must not be stopped by the stall backstop"
        assert state["status"] in ("failed", "open", "claimed"), (
            f"the task should have been reclaimed (retried/failed) or still claimed, but got {state['status']!r}"
        )

    def test_work_arriving_during_the_settle_window_aborts_the_stop(
        self, tmp_path: Path, fast_stall_env: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The confirmation pass must be able to change the answer.

        The stall verdict is computed against the snapshot the tick already
        fetched. If a task lands between that read and the settle re-check,
        the run is not over and must not be stopped.
        """
        transport, state = _stuck_run_transport("t-3010")
        late_task = {
            "id": "t-late",
            "title": "arrived during the settle window",
            "description": "d",
            "role": "worker",
            "status": "open",
            "created_at": time.time() - 1.0,
            "depends_on": [],
            "owned_files": [],
        }
        # Serve the late task on exactly one read: the settle re-check.
        # By then ``evaluate_run_stall`` has already recorded the third
        # qualifying tick, which the 8b refetch one step earlier had not.
        holder: dict[str, Any] = {"orch": None, "fired": False}

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if request.method == "GET" and url.path == "/tasks":
                tasks = [dict(state)]
                orch = holder["orch"]
                if orch is not None and not holder["fired"] and orch._run_stall_state.observed_ticks >= 3:
                    holder["fired"] = True
                    tasks.append(dict(late_task))
                return httpx.Response(200, json=tasks)
            return transport.handle_request(request)

        orch = _build_orchestrator(tmp_path, httpx.MockTransport(handler))
        holder["orch"] = orch
        orch._running = True

        # Stop at the tick that served the late task. Later ticks legitimately
        # stall again - the window resets on the abort, and the late task is
        # served only once - so running past it would be testing the wrong thing.
        with caplog.at_level(logging.INFO, logger="bernstein.core.orchestration.orchestrator"):
            for _ in range(_TICK_BUDGET):
                orch.tick()
                if holder["fired"]:
                    break

        assert holder["fired"] is True, "the late task must actually have been served"
        assert orch._running is True, "work arriving during the settle window must abort the stop"
        assert state["status"] == "claimed", "no task may be failed when the stall is not confirmed"
        assert "run_stall_check: NOT confirmed" in caplog.text, (
            "the confirmation pass must be the thing that aborted the stop, not an earlier guard"
        )
