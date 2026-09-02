"""Hermetic re-derivation of a run's coordination sequence (issue #4213).

``bernstein replay <run> --verify`` recomputes the Merkle chain over the rows
that are already on disk. That proves the journal was not edited after the
fact; it does not prove the coordination sequence those rows record is the
sequence the scheduler's own rules produce. The determinism promise was
therefore enforced by code structure alone, with no executable check an
operator could run.

This module supplies that check. It takes the two things the coordination
layer does not choose - the **recorded plan** (``plan.graph.full``) and the
**recorded per-task outcomes** (``task_completed`` / ``task_verification_failed``
/ ``task_retried``) - and walks the recorded run through the coordination state
machine the tick loop drives, refusing any step the rules could not have
produced at that point. Accepted steps are appended to a *fresh* journal in a
caller-supplied sandbox, so the re-derivation ends with a head computed from
inputs rather than copied from the recorded chain. Exit criterion: the
re-derived head equals the recorded head.

What is an input and what is a decision
---------------------------------------

* **Inputs, carried verbatim.** The plan graph, the outcome each leaf agent
  produced, and the observations that come with it (agent id, model, cost).
  Coordination did not pick these; an operator re-deriving a run must be able
  to hold them fixed.
* **Decisions, re-derived.** Whether a task was claimable at the point it was
  claimed, whether an outcome belonged to a task that was actually running,
  and the chain that binds the resulting sequence together.

Re-derivation deliberately does **not** re-execute agents (that is a live
re-run) and needs no adapter binary, no task server and no network: it reads
one JSONL file and writes another.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.replay.diff import (
    REASON_CODE_NONE,
    diff_event_logs,
)
from bernstein.core.replay.journal import (
    _NON_DETERMINISTIC_FIELDS,  # pyright: ignore[reportPrivateUsage] - shared journal projection
    EventJournal,
    JournalPathError,
    load_events,
)

if TYPE_CHECKING:
    from pathlib import Path

#: Journal event carrying the recorded planning output: the executed task
#: graph with one entry per node (``id`` / ``role`` / ``title`` /
#: ``depends_on``). Written by the orchestrator whenever the graph changes.
PLAN_GRAPH_FULL_EVENT = "plan.graph.full"

#: Journal event recording that coordination handed a task to an agent.
TASK_CLAIMED_EVENT = "task_claimed"

#: Recorded leaf outcomes. These are inputs to coordination, not decisions.
TASK_COMPLETED_EVENT = "task_completed"
TASK_VERIFICATION_FAILED_EVENT = "task_verification_failed"
TASK_RETRIED_EVENT = "task_retried"

# -- Refusal vocabulary ----------------------------------------------------
# Machine-readable, so a caller branches on the class of failure rather than
# on prose. ``REASON_CODE_NONE`` is re-exported from the divergence module so
# the "nothing wrong" value has exactly one spelling repo-wide.

#: The recorded sequence contains a step the coordination rules cannot produce.
REASON_CODE_UNDERIVABLE_STEP = "underivable_step"

#: Every step was derivable but the re-derived head differs from the recorded
#: one, so some payload the chain commits to is not what was recorded.
REASON_CODE_HEAD_MISMATCH = "head_mismatch"

#: The run recorded no planning output, so there is no input to re-derive from.
REASON_CODE_PLAN_MISSING = "plan_missing"

#: The journal could not be read as a clean sequence of rows.
REASON_CODE_JOURNAL_UNREADABLE = "journal_unreadable"

#: No journal exists for the named run.
REASON_CODE_JOURNAL_NOT_FOUND = "journal_not_found"

# -- Rule names ------------------------------------------------------------
# Which coordination rule refused the step. Reported alongside the index so
# the operator is told *why* a step is underivable, not only where.

#: A task was claimed while one of its recorded dependencies had not completed.
RULE_DEPENDENCY_NOT_COMPLETED = "dependency_not_completed"

#: A task was claimed while an earlier claim of it was still outstanding.
RULE_TASK_ALREADY_CLAIMED = "task_already_claimed"

#: A task outside the recorded plan was claimed.
RULE_TASK_NOT_IN_PLAN = "task_not_in_plan"

#: An outcome arrived for a task that was not claimed at that point.
RULE_OUTCOME_FOR_UNCLAIMED_TASK = "outcome_for_unclaimed_task"

#: No rule refused the step.
RULE_NONE = ""

_OUTCOME_EVENTS = frozenset(
    {
        TASK_COMPLETED_EVENT,
        TASK_VERIFICATION_FAILED_EVENT,
        TASK_RETRIED_EVENT,
    }
)


@dataclass(frozen=True)
class RederiveResult:
    """Outcome of re-deriving one run's coordination sequence.

    Attributes:
        ok: ``True`` only when every step was derivable *and* the re-derived
            head equals the recorded head.
        run_id: The run whose journal was re-derived.
        recorded_head: The head the recorded journal carries on its last row.
        derived_head: The head of the freshly built sandbox journal, or the
            empty string when the walk stopped before producing one.
        step_count: Number of steps the re-derivation accepted.
        divergent_index: 0-based index of the first step that could not be
            derived, or at which the two journals first differ. ``None`` when
            nothing diverged or the run was refused before the walk.
        reason: Human-readable explanation.
        reason_code: One of the ``REASON_CODE_*`` constants.
        rule: The ``RULE_*`` constant naming the coordination rule that
            refused the step, or :data:`RULE_NONE`.
        derived_journal_path: Filesystem path of the sandbox journal.
    """

    ok: bool
    run_id: str
    recorded_head: str = ""
    derived_head: str = ""
    step_count: int = 0
    divergent_index: int | None = None
    reason: str = ""
    reason_code: str = REASON_CODE_NONE
    rule: str = RULE_NONE
    derived_journal_path: str = ""


class _CoordinationState:
    """The per-task status the tick loop carries between steps.

    Deliberately the smallest state that decides claimability: which tasks the
    recorded plan declared, what each depends on, which have completed, and
    which are claimed right now. Anything else on a row is an observation and
    is carried through untouched.
    """

    def __init__(self) -> None:
        self.depends_on: dict[str, tuple[str, ...]] = {}
        self.planned: set[str] = set()
        self.completed: set[str] = set()
        self.claimed: set[str] = set()

    def absorb_plan(self, payload: dict[str, Any]) -> None:
        """Fold a recorded ``plan.graph.full`` payload into the known plan.

        A run re-plans as work lands, so later graph events extend or restate
        the node set. Folding rather than replacing keeps a task that was
        claimed under an earlier revision of the graph derivable.
        """
        nodes: object = payload.get("nodes")
        if not isinstance(nodes, list):
            return
        for node in cast("list[object]", nodes):
            if not isinstance(node, dict):
                continue
            fields = cast("dict[str, object]", node)
            task_id = str(fields.get("id", ""))
            if not task_id:
                continue
            raw_deps: object = fields.get("depends_on")
            deps = tuple(str(d) for d in cast("list[object]", raw_deps)) if isinstance(raw_deps, list) else ()
            self.planned.add(task_id)
            self.depends_on[task_id] = deps

    def refuse_claim(self, task_id: str) -> tuple[str, str]:
        """Return ``(rule, reason)`` when this claim is underivable, else ``(RULE_NONE, "")``.

        The plan gate is skipped while no plan has been seen yet: the
        orchestrator records the graph on the tick that builds it, so an
        opening claim can legitimately precede the first graph event, and
        refusing it would report a missing input as a coordination fault.
        """
        if self.planned and task_id not in self.planned:
            return (
                RULE_TASK_NOT_IN_PLAN,
                f"task {task_id} was claimed but the recorded plan does not contain it",
            )
        if task_id in self.claimed:
            return (
                RULE_TASK_ALREADY_CLAIMED,
                f"task {task_id} was claimed while an earlier claim of it was still outstanding",
            )
        unmet = [dep for dep in self.depends_on.get(task_id, ()) if dep not in self.completed]
        if unmet:
            return (
                RULE_DEPENDENCY_NOT_COMPLETED,
                f"task {task_id} was claimed before its dependencies completed: {', '.join(sorted(unmet))}",
            )
        return (RULE_NONE, "")

    def refuse_outcome(self, event: str, task_id: str) -> tuple[str, str]:
        """Return ``(rule, reason)`` when this outcome is underivable."""
        if task_id not in self.claimed:
            return (
                RULE_OUTCOME_FOR_UNCLAIMED_TASK,
                f"{event} arrived for task {task_id}, which was not claimed at this step",
            )
        return (RULE_NONE, "")

    def apply(self, event: str, task_id: str) -> None:
        """Advance the state for an already-accepted step."""
        if event == TASK_CLAIMED_EVENT:
            self.claimed.add(task_id)
        elif event == TASK_COMPLETED_EVENT:
            self.claimed.discard(task_id)
            self.completed.add(task_id)
        elif event == TASK_RETRIED_EVENT:
            # A retry hands the task back to the pool, so the next claim of it
            # is derivable again and its dependents stay blocked.
            self.claimed.discard(task_id)
            self.completed.discard(task_id)
        elif event == TASK_VERIFICATION_FAILED_EVENT:
            # Verification failure does not release the claim - the
            # orchestrator decides between retry and reap on a later step -
            # but it does mean the task has not completed.
            self.completed.discard(task_id)


def _payload_of(row: dict[str, Any]) -> dict[str, Any]:
    """Strip the chain and wall-clock envelope, leaving the recorded payload."""
    return {k: v for k, v in row.items() if k not in _NON_DETERMINISTIC_FIELDS and k != "event"}


def rederive_run(journal_path: Path, *, sandbox: Path) -> RederiveResult:
    """Re-derive a recorded run's coordination sequence into *sandbox*.

    Args:
        journal_path: Path to the recorded ``journal.jsonl``.
        sandbox: Directory to build the fresh journal under. Treated as an
            ``.sdd`` root, so the re-derived journal lands at
            ``<sandbox>/runs/<run_id>/journal.jsonl``. Nothing is written
            outside it and the recorded run directory is never touched.

    Returns:
        A :class:`RederiveResult`. ``ok`` is ``True`` only when every step was
        derivable and the re-derived head equals the recorded head.
    """
    run_id = journal_path.parent.name

    loaded = load_events(journal_path)
    if loaded.discarded_line_indices:
        joined = ", ".join(str(i) for i in loaded.discarded_line_indices)
        return RederiveResult(
            ok=False,
            run_id=run_id,
            reason=f"journal has unreadable physical line(s): {joined}",
            reason_code=REASON_CODE_JOURNAL_UNREADABLE,
        )

    events = loaded.events
    if not any(row.get("event") == PLAN_GRAPH_FULL_EVENT for row in events):
        return RederiveResult(
            ok=False,
            run_id=run_id,
            reason=(
                f"run {run_id} recorded no {PLAN_GRAPH_FULL_EVENT} event, so there is no "
                "planning output to re-derive the coordination sequence from"
            ),
            reason_code=REASON_CODE_PLAN_MISSING,
        )

    recorded_head = str(events[-1].get("event_hash", "")) if events else ""

    try:
        derived = EventJournal(run_id=run_id, sdd_dir=sandbox)
    except JournalPathError as exc:
        return RederiveResult(
            ok=False,
            run_id=run_id,
            reason=f"cannot build a sandbox journal for run id {run_id!r}: {exc}",
            reason_code=REASON_CODE_JOURNAL_UNREADABLE,
        )

    state = _CoordinationState()
    for index, row in enumerate(events):
        event = str(row.get("event", ""))
        payload = _payload_of(row)

        if event == PLAN_GRAPH_FULL_EVENT:
            state.absorb_plan(payload)
        elif event == TASK_CLAIMED_EVENT:
            rule, reason = state.refuse_claim(str(payload.get("task_id", "")))
            if rule != RULE_NONE:
                return RederiveResult(
                    ok=False,
                    run_id=run_id,
                    recorded_head=recorded_head,
                    derived_head=derived.head(),
                    step_count=index,
                    divergent_index=index,
                    reason=f"step {index}: {reason}",
                    reason_code=REASON_CODE_UNDERIVABLE_STEP,
                    rule=rule,
                    derived_journal_path=str(derived.path),
                )
        elif event in _OUTCOME_EVENTS:
            rule, reason = state.refuse_outcome(event, str(payload.get("task_id", "")))
            if rule != RULE_NONE:
                return RederiveResult(
                    ok=False,
                    run_id=run_id,
                    recorded_head=recorded_head,
                    derived_head=derived.head(),
                    step_count=index,
                    divergent_index=index,
                    reason=f"step {index}: {reason}",
                    reason_code=REASON_CODE_UNDERIVABLE_STEP,
                    rule=rule,
                    derived_journal_path=str(derived.path),
                )

        derived.record(event, **payload)
        state.apply(event, str(payload.get("task_id", "")))

    derived_head = derived.head()
    if derived_head == recorded_head:
        return RederiveResult(
            ok=True,
            run_id=run_id,
            recorded_head=recorded_head,
            derived_head=derived_head,
            step_count=len(events),
            reason=f"re-derived {len(events)} coordination step(s) to the recorded head",
            derived_journal_path=str(derived.path),
        )

    # Every step was derivable, so the divergence is in what a step carried
    # rather than in whether it could happen. Hand that to the pairwise
    # divergence locator instead of growing a second comparator here.
    divergence = diff_event_logs(journal_path, derived.path)
    return RederiveResult(
        ok=False,
        run_id=run_id,
        recorded_head=recorded_head,
        derived_head=derived_head,
        step_count=len(events),
        divergent_index=divergence.index,
        reason=(
            f"re-derived head does not match the recorded head; {divergence.reason}"
            if divergence.diverged
            else "re-derived head does not match the recorded head"
        ),
        reason_code=REASON_CODE_HEAD_MISMATCH,
        derived_journal_path=str(derived.path),
    )


__all__ = [
    "PLAN_GRAPH_FULL_EVENT",
    "REASON_CODE_HEAD_MISMATCH",
    "REASON_CODE_JOURNAL_NOT_FOUND",
    "REASON_CODE_JOURNAL_UNREADABLE",
    "REASON_CODE_NONE",
    "REASON_CODE_PLAN_MISSING",
    "REASON_CODE_UNDERIVABLE_STEP",
    "RULE_DEPENDENCY_NOT_COMPLETED",
    "RULE_NONE",
    "RULE_OUTCOME_FOR_UNCLAIMED_TASK",
    "RULE_TASK_ALREADY_CLAIMED",
    "RULE_TASK_NOT_IN_PLAN",
    "RederiveResult",
    "rederive_run",
]
