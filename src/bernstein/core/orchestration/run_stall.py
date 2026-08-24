"""Terminal state for a run that reaches quiescence without finishing anything.

The orchestrator's tick loop self-stops from exactly one place: the
quiescence settle-window check in ``Orchestrator._tick_internal`` step 8b.
That check is gated on at least one task having reached a terminal state
(``done`` or ``failed``), because reading ``open == agents == 0`` on tick #1
means "nothing has been scheduled yet", not "the run finished". The gate is
correct for startup and wrong for the rest of the run: when a run reaches
quiescence having produced **zero** terminal tasks, the gate is never
satisfied, ``_running`` is never cleared, and the loop idles indefinitely.
Issue #3010 is that shape - a single-task run whose only agent produced no
model output and was reaped left its one task non-terminal, and the
orchestrator logged roughly sixteen "quiescence detected (done 0->0,
failed 0->0)" ticks without stopping.

Idling is worse here than anywhere else: the case where nothing finished is
the case an operator most needs terminated and reported.

This module supplies the missing terminal state as a pure decision function
so the criterion can be tested without a tick loop, an HTTP server, or a
real clock.

Criterion
---------

A run is declared stalled only when **every** one of these holds:

1. The caller is inside a confirmed quiescent tick with zero terminal tasks
   (``open_tasks == active_agents == 0`` and ``done == failed == 0``). The
   orchestrator establishes this before calling in; it is the structural
   precondition, not something this module can observe.
2. At least one task is declared **and** at least one of them is in an
   actively-declared-but-unfinished state (``open``, ``claimed``,
   ``in_progress``, ``orphaned``). A run with no tasks at all is the
   pre-ingest startup window the original gate protects, and a run whose
   only remaining tasks are deliberately parked (``planned``,
   ``suspended``, ``pending_approval``, ``blocked``,
   ``waiting_for_subtasks``) is waiting on a human or a dependency by
   design, not stalled.
3. No task is deferred-but-claimable - an ``open`` task whose ``created_at``
   lies in the future is in retry backoff (capped at 300 s by
   ``task_lifecycle``), so work *is* still scheduled to arrive.
4. The task-state fingerprint - every ``(task_id, status)`` pair in the
   snapshot - has not changed for ``grace_s`` seconds. Any forward motion in
   the system moves at least one task between statuses: a claim is
   ``open -> claimed``, a spawn follows a claim, a release is
   ``claimed -> open``, a completion is terminal. An unchanged fingerprint
   is therefore the absence of progress, not merely the absence of
   completions.
5. The same fingerprint has been observed on at least ``min_ticks``
   consecutive qualifying ticks, so a single wall-clock discontinuity (an
   NTP step, a suspended container resuming) cannot on its own satisfy
   condition 4.

Which way it errs
-----------------

Toward keeping the run alive. Conditions 2, 3, and 5 each independently
suppress the stop, and the default grace (see
``defaults.OrchestratorDefaults.stalled_run_grace_s``) deliberately sits
above ``stale_claim_timeout_s`` so the stale-claim release - whose outcome
is strictly more informative, since it produces a real failed task with a
reason - always gets its chance first. A legitimately slow run is never at
risk: an agent that is still working keeps ``active_agents > 0`` and a task
waiting to be claimed keeps ``open_tasks > 0``, and neither state reaches
this module at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.models import Task

#: Statuses meaning a task was declared and was expected to make progress,
#: but had not reached any terminal outcome. Mirrors the set the
#: retrospective uses to decide that a run did not meet its goal. Parking
#: states (``planned`` awaiting approval, ``suspended`` operator-parked,
#: ``pending_approval``) and wait states (``blocked``,
#: ``waiting_for_subtasks``) are excluded: those are deliberate, and a run
#: holding only such tasks is waiting by design.
ACTIVE_UNFINISHED_STATUSES: frozenset[str] = frozenset({"open", "claimed", "in_progress", "orphaned"})

#: Terminal statuses. Their presence means the normal quiescence self-stop
#: path applies and this module is not consulted.
TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "failed", "closed", "cancelled"})

#: Env overrides, checked ahead of the ``OrchestratorConfig`` fields.
#: Precedence mirrors ``stalled_manager.STALL_THRESHOLD_ENV_VAR``:
#: env var > yaml-tunable config field > ``core.defaults``.
GRACE_ENV_VAR: str = "BERNSTEIN_STALLED_RUN_GRACE_S"
MIN_TICKS_ENV_VAR: str = "BERNSTEIN_STALLED_RUN_TICKS"


@dataclass(frozen=True)
class RunStallState:
    """Carried across ticks; the only state this detector needs.

    Attributes:
        fingerprint: The ``(task_id, status)`` pairs observed when this
            no-progress window opened. ``None`` before the first
            qualifying tick.
        since_ts: Wall clock at which the current fingerprint was first
            observed.
        observed_ticks: How many consecutive qualifying ticks have carried
            this fingerprint.
    """

    fingerprint: tuple[tuple[str, str], ...] | None = None
    since_ts: float = 0.0
    observed_ticks: int = 0


@dataclass(frozen=True)
class StallVerdict:
    """Outcome of one evaluation.

    Attributes:
        stalled: True when the run should be stopped and reported as not
            having met its goal.
        reason: Single-line operator-facing explanation. Populated on every
            evaluation, including the negative ones, so the log line
            explains why the run is still alive.
        stuck_task_ids: Tasks in :data:`ACTIVE_UNFINISHED_STATUSES` at the
            moment of the verdict. Empty unless ``stalled`` is True.
        observed_ticks: Consecutive qualifying ticks behind this verdict.
        quiet_for_s: Seconds the fingerprint has been unchanged.
    """

    stalled: bool
    reason: str
    stuck_task_ids: tuple[str, ...] = ()
    observed_ticks: int = 0
    quiet_for_s: float = 0.0
    parked_task_ids: tuple[str, ...] = field(default=())


def task_state_fingerprint(tasks_by_status: dict[str, list[Task]]) -> tuple[tuple[str, str], ...]:
    """Return a stable ``(task_id, status)`` fingerprint of a task snapshot.

    Sorted so two snapshots of the same world compare equal regardless of
    the order the server returned them in.
    """
    pairs: list[tuple[str, str]] = []
    for status, tasks in tasks_by_status.items():
        for task in tasks:
            pairs.append((str(task.id), status))
    return tuple(sorted(pairs))


def _status_of(status_key: str, task: Task) -> str:
    """Return the task's own status, falling back to its snapshot bucket.

    ``fetch_all_tasks`` buckets by ``task.status.value``, so the two agree;
    the fallback only matters for hand-built test snapshots.
    """
    raw = getattr(task, "status", None)
    value = getattr(raw, "value", None)
    return str(value) if isinstance(value, str) else status_key


def resolve_grace_s(config_value: float) -> float:
    """Resolve the no-progress grace window, env var taking precedence."""
    return _resolve_float(GRACE_ENV_VAR, config_value)


def resolve_min_ticks(config_value: int) -> int:
    """Resolve the consecutive-tick floor, env var taking precedence."""
    raw = os.environ.get(MIN_TICKS_ENV_VAR)
    if raw is not None:
        try:
            return max(int(raw), 1)
        except ValueError:
            return max(int(config_value), 1)
    return max(int(config_value), 1)


def _resolve_float(env_var: str, config_value: float) -> float:
    raw = os.environ.get(env_var)
    if raw is not None:
        try:
            return max(float(raw), 0.0)
        except ValueError:
            return max(float(config_value), 0.0)
    return max(float(config_value), 0.0)


def evaluate_run_stall(
    prev: RunStallState,
    tasks_by_status: dict[str, list[Task]],
    *,
    now: float,
    grace_s: float,
    min_ticks: int,
) -> tuple[RunStallState, StallVerdict]:
    """Decide whether a zero-terminal quiescent run has stopped progressing.

    Pure: reads only its arguments, mutates nothing, and performs no IO.
    The caller supplies ``now`` so tests do not sleep.

    Args:
        prev: State returned by the previous call, or a fresh
            :class:`RunStallState` on the first quiescent tick.
        tasks_by_status: The post-settle task snapshot, bucketed by status.
        now: Current wall clock, seconds.
        grace_s: Seconds the fingerprint must stay unchanged.
        min_ticks: Consecutive qualifying ticks required.

    Returns:
        ``(next_state, verdict)``. The caller stores ``next_state`` and acts
        on ``verdict.stalled``.
    """
    active_ids: list[str] = []
    parked_ids: list[str] = []
    deferred_ids: list[str] = []
    total = 0

    for status_key, tasks in tasks_by_status.items():
        for task in tasks:
            total += 1
            status = _status_of(status_key, task)
            if status in TERMINAL_STATUSES:
                continue
            task_id = str(task.id)
            if status in ACTIVE_UNFINISHED_STATUSES:
                active_ids.append(task_id)
            else:
                parked_ids.append(task_id)
            if status == "open" and float(getattr(task, "created_at", 0.0) or 0.0) > now:
                deferred_ids.append(task_id)

    active_ids.sort()
    parked_ids.sort()

    # Condition 2a: no tasks at all is the pre-ingest startup window that
    # the quiescence gate's zero-terminal guard exists to protect. Reset
    # rather than accumulate, so the clock starts when work appears.
    if total == 0:
        return RunStallState(), StallVerdict(
            stalled=False,
            reason="no tasks declared yet - startup window, not a stall",
        )

    # Condition 2b: only deliberately-parked tasks remain. Waiting on a
    # human approval or an unmet dependency is not the run failing to make
    # progress; stopping here would discard a resumable state.
    if not active_ids:
        return RunStallState(), StallVerdict(
            stalled=False,
            reason=(f"{len(parked_ids)} task(s) remain but all are parked or waiting by design - not a stall"),
            parked_task_ids=tuple(parked_ids),
        )

    # Condition 3: retry backoff (capped at 300s) will make work claimable
    # again without anything else having to happen.
    if deferred_ids:
        return RunStallState(), StallVerdict(
            stalled=False,
            reason=f"{len(deferred_ids)} task(s) deferred by retry backoff - work is still scheduled",
        )

    fingerprint = task_state_fingerprint(tasks_by_status)

    # Condition 4: any change to the task-state fingerprint is forward
    # motion. Restart the window rather than extending the old one.
    if prev.fingerprint != fingerprint:
        return RunStallState(fingerprint=fingerprint, since_ts=now, observed_ticks=1), StallVerdict(
            stalled=False,
            reason="task state changed since the last quiescent tick - progress observed",
            observed_ticks=1,
        )

    observed_ticks = prev.observed_ticks + 1
    quiet_for_s = max(now - prev.since_ts, 0.0)
    state = replace(prev, observed_ticks=observed_ticks)

    # Condition 5: the consecutive-tick floor. Guards the wall-clock test
    # against a single time discontinuity.
    if observed_ticks < min_ticks:
        return state, StallVerdict(
            stalled=False,
            reason=(
                f"no progress for {quiet_for_s:.0f}s over {observed_ticks} tick(s) - "
                f"need {min_ticks} before declaring a stall"
            ),
            observed_ticks=observed_ticks,
            quiet_for_s=quiet_for_s,
        )

    if quiet_for_s < grace_s:
        return state, StallVerdict(
            stalled=False,
            reason=(
                f"no progress for {quiet_for_s:.0f}s over {observed_ticks} tick(s) - grace window is {grace_s:.0f}s"
            ),
            observed_ticks=observed_ticks,
            quiet_for_s=quiet_for_s,
        )

    return state, StallVerdict(
        stalled=True,
        reason=(
            f"run produced no terminal task and made no progress for {quiet_for_s:.0f}s "
            f"across {observed_ticks} quiescent tick(s); {len(active_ids)} declared task(s) "
            f"are unfinished with no live agent and no claimable work"
        ),
        stuck_task_ids=tuple(active_ids),
        observed_ticks=observed_ticks,
        quiet_for_s=quiet_for_s,
        parked_task_ids=tuple(parked_ids),
    )


def evaluate_progress_stall(
    prev: RunStallState,
    tasks_by_status: dict[str, list[Task]],
    *,
    active_agents: int,
    now: float,
    grace_s: float,
    min_ticks: int,
) -> tuple[RunStallState, StallVerdict]:
    """Decide whether a run with terminal tasks has wedged on a dead claim.

    Pure: reads only its arguments, mutates nothing, and performs no IO.
    The caller supplies ``now`` so tests do not sleep.

    This is the sibling of :func:`evaluate_run_stall` for the shape where
    the run *has* finished work but is stuck anyway: ``done > 0`` yet a
    claimed task is held by a dead agent, so quiescence can never be
    reached. The zero-terminal and progress-stall criteria are structurally
    different and deliberately not merged.

    Args:
        prev: State returned by the previous call, or a fresh
            :class:`RunStallState` on the first qualifying tick.
        tasks_by_status: The post-settle task snapshot, bucketed by status.
        active_agents: Number of live agents. Passed in, not inferred, so
            the caller's liveness view is authoritative.
        now: Current wall clock, seconds.
        grace_s: Seconds the fingerprint must stay unchanged.
        min_ticks: Consecutive qualifying ticks required.

    Returns:
        ``(next_state, verdict)``. The caller stores ``next_state`` and acts
        on ``verdict.stalled``.
    """
    terminal_count = 0
    claimed_ids: list[str] = []
    deferred_ids: list[str] = []

    for status_key, tasks in tasks_by_status.items():
        for task in tasks:
            status = _status_of(status_key, task)
            if status in TERMINAL_STATUSES:
                terminal_count += 1
                continue
            task_id = str(task.id)
            if status == "claimed":
                claimed_ids.append(task_id)
            if status == "open" and float(getattr(task, "created_at", 0.0) or 0.0) > now:
                deferred_ids.append(task_id)

    claimed_ids.sort()

    # Condition 1: at least one terminal task. Zero terminal tasks is the
    # shape ``evaluate_run_stall`` owns; this detector is only for runs that
    # finished something but still cannot quiesce.
    if terminal_count == 0:
        return RunStallState(), StallVerdict(
            stalled=False,
            reason="no terminal task yet - zero-terminal stall is handled by evaluate_run_stall",
        )

    # Condition 2: no live agent. A live agent is progress by definition.
    if active_agents > 0:
        return RunStallState(), StallVerdict(
            stalled=False,
            reason=f"{active_agents} live agent(s) - work is still in flight",
        )

    # Condition 3: at least one claimed task. A dead agent holding a claim
    # is the wedge that prevents quiescence.
    if not claimed_ids:
        return RunStallState(), StallVerdict(
            stalled=False,
            reason="no claimed task - nothing is wedged on a dead agent",
        )

    # Condition 6: retry backoff (capped at 300s) will make work claimable
    # again without anything else having to happen.
    if deferred_ids:
        return RunStallState(), StallVerdict(
            stalled=False,
            reason=f"{len(deferred_ids)} task(s) deferred by retry backoff - work is still scheduled",
        )

    fingerprint = task_state_fingerprint(tasks_by_status)

    # Condition 4: any change to the task-state fingerprint is forward
    # motion. Restart the window rather than extending the old one.
    if prev.fingerprint != fingerprint:
        return RunStallState(fingerprint=fingerprint, since_ts=now, observed_ticks=1), StallVerdict(
            stalled=False,
            reason="task state changed since the last qualifying tick - progress observed",
            observed_ticks=1,
        )

    observed_ticks = prev.observed_ticks + 1
    quiet_for_s = max(now - prev.since_ts, 0.0)
    state = replace(prev, observed_ticks=observed_ticks)

    # Condition 5: the consecutive-tick floor. Guards the wall-clock test
    # against a single time discontinuity.
    if observed_ticks < min_ticks:
        return state, StallVerdict(
            stalled=False,
            reason=(
                f"no progress for {quiet_for_s:.0f}s over {observed_ticks} tick(s) - "
                f"need {min_ticks} before declaring a stall"
            ),
            observed_ticks=observed_ticks,
            quiet_for_s=quiet_for_s,
        )

    if quiet_for_s < grace_s:
        return state, StallVerdict(
            stalled=False,
            reason=(
                f"no progress for {quiet_for_s:.0f}s over {observed_ticks} tick(s) - grace window is {grace_s:.0f}s"
            ),
            observed_ticks=observed_ticks,
            quiet_for_s=quiet_for_s,
        )

    return state, StallVerdict(
        stalled=True,
        reason=(
            f"run stalled: {terminal_count} terminal task(s) but {len(claimed_ids)} claimed task(s) "
            f"wedged with no live agent for {quiet_for_s:.0f}s across {observed_ticks} tick(s)"
        ),
        stuck_task_ids=tuple(claimed_ids),
        observed_ticks=observed_ticks,
        quiet_for_s=quiet_for_s,
    )


#: Reason recorded against each stuck task when the run is stopped. Written
#: verbatim into the task's failure reason so the retrospective, the audit
#: chain, and ``bernstein status`` all carry the same explanation.
STUCK_TASK_FAIL_REASON: str = (
    "Run stalled: the task never reached a terminal state, no agent was alive, "
    "and no claimable work remained. The run did not meet its goal."
)

#: Reason recorded against each wedged claimed task when a run that already
#: produced terminal tasks is stopped for the progress-stall pattern.
PROGRESS_STUCK_CLAIM_FAIL_REASON: str = (
    "Run stalled: the run finished work but a claimed task was held by a dead "
    "agent, so quiescence could never be reached. The run did not meet its goal."
)
