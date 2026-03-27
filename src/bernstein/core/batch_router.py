"""Batch API routing for non-urgent tasks.

Routes docs, formatting, and simple test tasks to provider batch APIs
(e.g., Anthropic Message Batches API) which offer ~50% cost discount.
Real-time tasks with interactive feedback requirements stay on the standard API.

Tagging happens at decomposition time via ``classify_batch_mode(task)``.
The orchestrator then passes ``use_batch_api=True`` to the adapter when
spawning batch-eligible agents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.models import Task

# Discount factor for provider batch APIs (Anthropic Batch API offers 50% off)
BATCH_DISCOUNT_FACTOR: float = 0.50

# Keywords in title/description that indicate a batch-eligible task
_BATCH_KEYWORDS: re.Pattern[str] = re.compile(
    r"\b("
    r"doc|docs|docstring|documentation|readme|changelog|"
    r"format|formatting|style|lint|"
    r"comment|comments|annotation|type.?hint|"
    r"simple.?test|unit.?test|add.?test|test.?stub|"
    r"migration.?note|release.?note|"
    r"update.?version|bump.?version"
    r")\b",
    re.IGNORECASE,
)

# Roles that are never batch-eligible (need real-time reasoning)
_REALTIME_ROLES: frozenset[str] = frozenset({"manager", "architect", "security", "orchestrator"})


class BatchMode(StrEnum):
    """Whether a task should use the batch API or real-time API."""

    REALTIME = "realtime"  # Standard API — interactive, no discount
    BATCH = "batch"  # Batch API — async, 50% discount


@dataclass(frozen=True)
class BatchClassification:
    """Result of classifying a task's batch eligibility.

    Attributes:
        mode: BATCH or REALTIME.
        reason: Human-readable explanation.
        discount_factor: Cost multiplier (1.0 = no discount, 0.5 = 50% off).
    """

    mode: BatchMode
    reason: str
    discount_factor: float


def classify_batch_mode(task: Task) -> BatchClassification:
    """Determine whether a task should use the batch API or real-time API.

    Criteria for BATCH eligibility (all must hold):
    - Role is not in the high-stakes realtime set
    - Priority is not critical (1)
    - Scope is not LARGE
    - Complexity is not HIGH
    - Task type is not RESEARCH or UPGRADE_PROPOSAL (those need fresh reasoning)
    - No manager-specified model override requesting premium models
    - Either: complexity==LOW, or title/description matches batch-keyword patterns

    Args:
        task: Task to evaluate.

    Returns:
        BatchClassification with mode, reason, and discount factor.
    """
    from bernstein.core.models import Complexity, Scope, TaskType

    # Hard REALTIME gates
    if task.role in _REALTIME_ROLES:
        return BatchClassification(
            mode=BatchMode.REALTIME,
            reason=f"role={task.role!r} requires real-time API",
            discount_factor=1.0,
        )

    if task.priority == 1:
        return BatchClassification(
            mode=BatchMode.REALTIME,
            reason="critical priority (1) requires real-time API",
            discount_factor=1.0,
        )

    if task.scope == Scope.LARGE:
        return BatchClassification(
            mode=BatchMode.REALTIME,
            reason="large scope requires real-time API",
            discount_factor=1.0,
        )

    if task.complexity == Complexity.HIGH:
        return BatchClassification(
            mode=BatchMode.REALTIME,
            reason="high complexity requires real-time API",
            discount_factor=1.0,
        )

    if task.task_type in (TaskType.RESEARCH, TaskType.UPGRADE_PROPOSAL):
        return BatchClassification(
            mode=BatchMode.REALTIME,
            reason=f"task_type={task.task_type.value} requires real-time API",
            discount_factor=1.0,
        )

    # Respect explicit premium model overrides from the manager
    if task.model and task.model.lower() in ("opus",):
        return BatchClassification(
            mode=BatchMode.REALTIME,
            reason="manager requested opus — real-time API",
            discount_factor=1.0,
        )

    # Explicit override from task field (set during decomposition)
    if task.batch_eligible is False:
        return BatchClassification(
            mode=BatchMode.REALTIME,
            reason="task.batch_eligible=False",
            discount_factor=1.0,
        )

    if task.batch_eligible is True:
        return BatchClassification(
            mode=BatchMode.BATCH,
            reason="task.batch_eligible=True (explicit)",
            discount_factor=BATCH_DISCOUNT_FACTOR,
        )

    # Auto-detect: LOW complexity tasks are prime batch candidates
    if task.complexity == Complexity.LOW:
        return BatchClassification(
            mode=BatchMode.BATCH,
            reason="complexity=LOW → batch eligible",
            discount_factor=BATCH_DISCOUNT_FACTOR,
        )

    # Auto-detect: keyword match in title/description
    text = f"{task.title} {task.description}"
    m = _BATCH_KEYWORDS.search(text)
    if m:
        return BatchClassification(
            mode=BatchMode.BATCH,
            reason=f"keyword match: {m.group()!r}",
            discount_factor=BATCH_DISCOUNT_FACTOR,
        )

    # Default: real-time
    return BatchClassification(
        mode=BatchMode.REALTIME,
        reason="no batch criteria matched",
        discount_factor=1.0,
    )


def apply_batch_discount(cost_usd: float, classification: BatchClassification) -> float:
    """Return the discounted cost for a routing decision.

    Args:
        cost_usd: Estimated cost at standard API rates.
        classification: Batch classification result.

    Returns:
        Discounted cost in USD.
    """
    return cost_usd * classification.discount_factor
