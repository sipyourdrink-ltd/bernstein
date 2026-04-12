"""Task priority aging to prevent starvation of low-priority tasks.

Low-priority tasks receive a priority boost over time so they eventually
get scheduled.  The aging rate is configurable.

Usage::

    from bernstein.core.priority_aging import AgingConfig, apply_aging

    config = AgingConfig(threshold_seconds=300, boost_per_interval=1, min_priority=1)
    boosted = apply_aging(tasks, config)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.models import TaskStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgingConfig:
    """Configuration for priority aging.

    Attributes:
        threshold_seconds: How long a task must wait before each boost.
        boost_per_interval: Priority decrease per aging interval (lower = higher priority).
        min_priority: Minimum priority value (highest urgency). Aging will not
            boost a task beyond this value.
        max_boosts: Maximum number of boosts a single task can receive.
            0 means unlimited.
    """

    threshold_seconds: float = 300.0
    boost_per_interval: int = 1
    min_priority: int = 1
    max_boosts: int = 0


@dataclass(frozen=True)
class AgingResult:
    """Result of applying aging to a single task.

    Attributes:
        task_id: The task identifier.
        original_priority: Priority before aging.
        new_priority: Priority after aging.
        age_seconds: How long the task has been waiting.
        boosts_applied: Number of boost intervals applied.
    """

    task_id: str
    original_priority: int
    new_priority: int
    age_seconds: float
    boosts_applied: int


_AGING_ELIGIBLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.OPEN,
        TaskStatus.BLOCKED,
    }
)


def compute_aged_priority(
    original_priority: int,
    age_seconds: float,
    config: AgingConfig,
) -> tuple[int, int]:
    """Compute the new priority and number of boosts for a given age.

    Args:
        original_priority: The task's original priority value.
        age_seconds: How long the task has been in an eligible status.
        config: Aging configuration.

    Returns:
        Tuple of (new_priority, boosts_applied).
    """
    if config.threshold_seconds <= 0 or age_seconds < config.threshold_seconds:
        return original_priority, 0

    boosts = int(age_seconds // config.threshold_seconds)
    if config.max_boosts > 0:
        boosts = min(boosts, config.max_boosts)

    total_boost = boosts * config.boost_per_interval
    new_priority = max(original_priority - total_boost, config.min_priority)
    return new_priority, boosts


def apply_aging(
    tasks: Sequence[Task],
    config: AgingConfig | None = None,
    now: float | None = None,
) -> list[AgingResult]:
    """Apply priority aging to a sequence of tasks.

    Mutates the ``priority`` field on each eligible task in-place and returns
    a list of AgingResult records describing what changed.

    Only tasks with status in OPEN or BLOCKED are eligible for aging.

    Args:
        tasks: Tasks to apply aging to.
        config: Aging configuration. Defaults to AgingConfig().
        now: Current time as epoch seconds. Defaults to time.time().

    Returns:
        List of AgingResult for tasks that received a boost.
    """
    if config is None:
        config = AgingConfig()
    if now is None:
        now = time.time()

    results: list[AgingResult] = []
    for task in tasks:
        if task.status not in _AGING_ELIGIBLE_STATUSES:
            continue

        age_seconds = now - task.created_at
        original = task.priority
        new_priority, boosts = compute_aged_priority(original, age_seconds, config)

        if boosts > 0 and new_priority != original:
            task.priority = new_priority
            result = AgingResult(
                task_id=task.id,
                original_priority=original,
                new_priority=new_priority,
                age_seconds=age_seconds,
                boosts_applied=boosts,
            )
            results.append(result)
            logger.debug(
                "Aged task %s: priority %d -> %d (%d boosts, %.0fs old)",
                task.id,
                original,
                new_priority,
                boosts,
                age_seconds,
            )

    if results:
        logger.info("Priority aging: boosted %d task(s)", len(results))
    return results
