"""Task retry, escalation, and failure handling.

Thin re-export module -- all implementations live in
:mod:`bernstein.core.tasks.task_lifecycle`.  This module exists so that
imports of the form ``from bernstein.core.tasks.task_retry import ...``
continue to work after the code was consolidated.
"""

from bernstein.core.tasks.task_lifecycle import (
    _EFFORT_LADDER,
    _MODEL_LADDER,
    _XL_ROLES,
    _bump_effort,
    _choose_retry_escalation,
    _escalate_model,
    _extract_failure_context,
    maybe_retry_task,
    retry_or_fail_task,
)

__all__ = [
    "_EFFORT_LADDER",
    "_MODEL_LADDER",
    "_XL_ROLES",
    "_bump_effort",
    "_choose_retry_escalation",
    "_escalate_model",
    "_extract_failure_context",
    "maybe_retry_task",
    "retry_or_fail_task",
]
