"""Single ``Task`` -> ``POST /tasks`` body builder.

The planner and the manager each hand-built this body and drifted (the manager
dropped ``completion_signals`` and ``metadata.context_files``). Both now call
:func:`build_task_body`; the backlog path shares :func:`signals_payload`.

``FORWARDED_FIELDS`` and ``NOT_FORWARDED`` classify every ``TaskCreate`` field;
``tests/unit/test_task_body.py`` fails when a new field is in neither.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from bernstein.core.models import TaskType
from bernstein.core.tasks.artifacts import ArtifactKind

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.models import Task

FORWARDED_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "role",
        "priority",
        "scope",
        "complexity",
        "estimated_minutes",
        "depends_on",
        "owned_files",
        "task_type",
        "cli",
        "model",
        "effort",
        "upgrade_details",
        "artifact_spec",
        "completion_signals",
        "metadata",  # only the ``context_files`` key
    }
)

_NOT_YET = "not carried on this wire today; forward it here when a producer needs it"
NOT_FORWARDED: dict[str, str] = {
    "id": "server assigns the id",
    "tenant_id": "server applies the request tenant",
    "eu_ai_act_risk": _NOT_YET,
    "approval_required": _NOT_YET,
    "risk_level": _NOT_YET,
    "parent_task_id": _NOT_YET,
    "depends_on_repo": _NOT_YET,
    "cell_id": _NOT_YET,
    "repo": _NOT_YET,
    "batch_eligible": _NOT_YET,
    "slack_context": "trigger-source metadata; set by the Slack routes only",
    "deadline": _NOT_YET,
    "parent_session_id": "set by the server-side self-create path",
    "parent_context": "set by the server-side self-create path",
    "retry_count": "retry bookkeeping is owned by the orchestrator",
    "max_retries": "retry bookkeeping is owned by the orchestrator",
    "retry_delay_s": "retry bookkeeping is owned by the orchestrator",
    "terminal_reason": "set by the server on failure",
    "max_output_tokens": "escalated by the orchestrator on retry",
    "meta_messages": "operational hints added at runtime",
    "max_turns": _NOT_YET,
}


def signals_payload(signals: Iterable[Any]) -> list[dict[str, str]]:
    """Serialize completion signals (objects or ``{type, value}`` dicts)."""
    out: list[dict[str, str]] = []
    for s in signals:
        if isinstance(s, dict):
            out.append({"type": str(s.get("type", "")), "value": str(s.get("value", ""))})
        else:
            out.append({"type": s.type, "value": s.value})
    return out


def build_task_body(task: Task, *, plan_mode: bool = False) -> dict[str, Any]:
    """Build the ``TaskCreate`` body for ``task``.

    Upgrade proposals get a priority boost (minus 1, floor 1).
    """
    priority = task.priority
    if task.task_type == TaskType.UPGRADE_PROPOSAL:
        priority = max(1, task.priority - 1)

    body: dict[str, Any] = {
        "title": task.title,
        "description": task.description,
        "role": task.role,
        "priority": priority,
        "scope": task.scope.value,
        "complexity": task.complexity.value,
        "estimated_minutes": task.estimated_minutes,
        "depends_on": task.depends_on,
        "owned_files": task.owned_files,
        "task_type": task.task_type.value,
    }
    # Routing hints: dropping ``model`` collapses plan-driven runs onto the
    # orchestrator default, violating per-step ``cli:``/``model:`` directives.
    if task.cli:
        body["cli"] = task.cli
    if task.model:
        body["model"] = task.model
    if task.effort:
        body["effort"] = task.effort
    if task.upgrade_details:
        body["upgrade_details"] = asdict(task.upgrade_details)
    # Issue #3110: a dropped artifact contract silently becomes code_diff.
    if task.artifact_spec.kind is not ArtifactKind.CODE_DIFF:
        body["artifact_spec"] = task.artifact_spec.to_dict()
    # Dropped signals mean the task is never gated on what it declared.
    if task.completion_signals:
        body["completion_signals"] = signals_payload(task.completion_signals)
    # Issue #3375: only the ``context_files`` key rides this wire; other
    # loader-internal metadata keys stay server-absent.
    context_files = task.metadata.get("context_files") if isinstance(task.metadata, dict) else None
    if isinstance(context_files, list) and context_files:
        body["metadata"] = {"context_files": [str(p) for p in context_files]}
    if plan_mode:
        body["status"] = "planned"
    return body
