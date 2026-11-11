"""Deterministic fleet selection from explicit worker capability matrices."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from bernstein.core.fleet.directory_registry import InstanceSpec
    from bernstein.core.tasks.models import Task


def select_fleet_instance(task: Task, instances: Iterable[InstanceSpec]) -> InstanceSpec | None:
    """Return the smallest enabled instance name with one fully matching worker."""
    return min(
        (
            instance
            for instance in instances
            if not instance.disabled
            and any(
                task.task_type in worker.task_types
                and task.dispatch_tier in worker.dispatch_tiers
                and set(task.requires).issubset(worker.capabilities)
                for worker in instance.workers
            )
        ),
        key=lambda instance: instance.name,
        default=None,
    )
