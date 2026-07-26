"""The gates shared by every MCP surface that exposes an approval or completion verb.

``bernstein_approve`` and ``bernstein_complete`` are reachable both through
the in-process FastMCP server (:mod:`bernstein.mcp.server`) and through the
streamable HTTP transport (:mod:`bernstein.mcp.remote_transport`). Both
enforce the gates from here, so a caller cannot pick a transport to get a
weaker rule, and both sets are projected from the task state machine
(:mod:`bernstein.core.tasks.lifecycle`) rather than restated per surface.

Two separate questions are answered here, because the two verbs fail in
different directions:

* *Is there an approval to grant?* Only a task holding a finished result for
  sign-off (``pending_approval``) has one. A ``planned`` task is waiting on
  the plan gate, whose decision is recorded per plan and not per task, so a
  per-task verb refuses it and names the route that owns the decision.
* *Is this the caller's work to finish?* A worker reports completion for a
  task it holds. A parent waiting on its subtasks, a task whose worker was
  declared gone, and a result awaiting sign-off are none of those, so the
  completion verb refuses them rather than marking work done that the caller
  did not do.

Both refusals name the current status, because a caller that cannot see what
the task is doing can only retry the same call.
"""

from __future__ import annotations

from typing import Any

from bernstein.core.tasks.lifecycle import (
    APPROVABLE_TASK_STATUSES,
    PLAN_GATED_TASK_STATUSES,
    WORKER_COMPLETABLE_TASK_STATUSES,
)

#: Status values an approval may act on, sorted for a stable wire payload.
APPROVABLE_STATUS_VALUES: tuple[str, ...] = tuple(sorted(s.value for s in APPROVABLE_TASK_STATUSES))

#: Status values whose decision belongs to the plan gate, not to the task.
PLAN_GATED_STATUS_VALUES: tuple[str, ...] = tuple(sorted(s.value for s in PLAN_GATED_TASK_STATUSES))

#: Status values a worker may report its own completion from.
COMPLETABLE_STATUS_VALUES: tuple[str, ...] = tuple(sorted(s.value for s in WORKER_COMPLETABLE_TASK_STATUSES))

#: Error code carried by the approval refusal payload.
REFUSAL_ERROR: str = "task_not_awaiting_approval"

#: Error code carried by the completion refusal payload.
COMPLETION_REFUSAL_ERROR: str = "task_not_completable"

_PLAN_GATE_HINT: str = (
    "This task is held by plan mode and is released when the plan it belongs to is "
    "approved, not one task at a time. Decide the plan instead: "
    "POST /plans/{plan_id}/approve, or 'bernstein plan approve <plan_id>'. "
    "Approving the task on its own would start the work while the plan is still "
    "undecided, so a later rejection of the plan would have nothing left to cancel."
)

_GENERIC_HINT: str = (
    "To finish work you are executing, use bernstein_complete. "
    "To report that the task is stuck, post to the task mailbox with bernstein_post_message. "
    "To abandon the work, cancel the task (bernstein task cancel <task_id>)."
)


def is_approvable(status: str) -> bool:
    """Return True when *status* is a state an approval is defined for.

    An unknown or empty status is not approvable, so a task payload without a
    readable status fails closed rather than being completed.
    """
    return status in APPROVABLE_STATUS_VALUES


def is_plan_gated(status: str) -> bool:
    """Return True when *status* waits on a decision the plan gate owns."""
    return status in PLAN_GATED_STATUS_VALUES


def is_worker_completable(status: str) -> bool:
    """Return True when a worker may report its own completion from *status*.

    An unknown or empty status is not completable, so a task payload without a
    readable status fails closed rather than being completed.
    """
    return status in COMPLETABLE_STATUS_VALUES


def refusal_payload(task_id: str, current_status: str) -> dict[str, Any]:
    """Build the structured refusal for a task with no approval to grant.

    The payload names the current status so the caller can pick a different
    action instead of retrying the approval, and lists the states an approval
    is defined for. A task held by the plan gate gets a hint naming the route
    that owns its decision instead of the generic one.

    Args:
        task_id: The task the approval was attempted on.
        current_status: The status the task server reported, or an empty
            string when the task payload carried none.

    Returns:
        The refusal as a JSON-serialisable dict.
    """
    approvable = ", ".join(APPROVABLE_STATUS_VALUES)
    reported = current_status or "unknown"
    plan_gated = is_plan_gated(reported)
    if plan_gated:
        message = (
            f"Task {task_id} is in status '{reported}' and is waiting on a plan decision, "
            f"not on a per-task approval. bernstein_approve does not release a task from "
            f"plan mode."
        )
    else:
        message = (
            f"Task {task_id} is in status '{reported}'. bernstein_approve only acts on a task "
            f"holding a finished result for sign-off ({approvable}), and never forces another "
            f"state forward."
        )
    return {
        "error": REFUSAL_ERROR,
        "task_id": task_id,
        "current_status": reported,
        "approvable_statuses": list(APPROVABLE_STATUS_VALUES),
        "message": message,
        "hint": _PLAN_GATE_HINT if plan_gated else _GENERIC_HINT,
    }


def completion_refusal_payload(task_id: str, current_status: str) -> dict[str, Any]:
    """Build the structured refusal for a task the caller may not complete.

    Args:
        task_id: The task the completion was attempted on.
        current_status: The status the task server reported, or an empty
            string when the task payload carried none.

    Returns:
        The refusal as a JSON-serialisable dict.
    """
    completable = ", ".join(COMPLETABLE_STATUS_VALUES)
    reported = current_status or "unknown"
    return {
        "error": COMPLETION_REFUSAL_ERROR,
        "task_id": task_id,
        "current_status": reported,
        "completable_statuses": list(COMPLETABLE_STATUS_VALUES),
        "message": (
            f"Task {task_id} is in status '{reported}'. bernstein_complete reports the result "
            f"of work you are executing ({completable}), and does not finish a task that is "
            f"waiting on its subtasks, whose worker is gone, or whose result is already "
            f"awaiting a decision."
        ),
        "hint": (
            "A parent in 'waiting_for_subtasks' completes when its subtasks do. "
            "An 'orphaned' task belongs to crash recovery. "
            "To report what you found without claiming the work is finished, post to the "
            "task mailbox with bernstein_post_message."
        ),
    }
