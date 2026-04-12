"""Worker badge identity module — format worker metadata into Rich badge strings.

Status icons and tier color coding. Provides a deterministic badge for each
worker that shows role, model and tier with status indication.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class WorkerStatus(StrEnum):
    """Status of a worker process."""

    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class TierColor(StrEnum):
    """Terminal color names for tier badges."""

    FREE = "green"
    PAID = "blue"
    ENTERPRISE = "gold"


STATUS_ICONS = {
    WorkerStatus.RUNNING: "✓",
    WorkerStatus.PAUSED: "⏸",
    WorkerStatus.STOPPED: "✗",
    WorkerStatus.ERROR: "⚠",
}

TIER_COLORS = {
    "free": TierColor.FREE,
    "paid": TierColor.PAID,
    "enterprise": TierColor.ENTERPRISE,
}


@dataclass(frozen=True)
class WorkerBadge:
    """Immutable badge describing a worker identity."""

    worker_id: str
    role: str
    model: str
    tier: str
    start_time: datetime
    status: WorkerStatus = WorkerStatus.RUNNING

    @property
    def status_icon(self) -> str:
        """Icon representing the current worker status."""
        return STATUS_ICONS[self.status]

    @property
    def tier_color(self) -> str:
        """Terminal color for the tier badge."""
        return TIER_COLORS.get(self.tier, TierColor.PAID).value

    @property
    def tier_display(self) -> str:
        """Displayable tier string: 'free-tier', 'paid-tier', 'enterprise-tier'."""
        return f"{self.tier}-tier"


STATUS_ICON_COLORS = {
    WorkerStatus.RUNNING: "green",
    WorkerStatus.PAUSED: "yellow",
    WorkerStatus.STOPPED: "red",
    WorkerStatus.ERROR: "red",
}


def format_worker_badge(badge: WorkerBadge) -> str:
    """Produce a Rich-formatted badge string for the worker.

    Args:
        badge: Complete worker badge data.

    Returns:
        Formatted Rich markup string like
        ``[green]✓[/] backend [sonnet] [blue]free-tier[/]``
        with status icon and tier color.
    """
    icon_color = STATUS_ICON_COLORS[badge.status]
    return (
        f"[{icon_color}]{badge.status_icon}[/] {badge.role} [{badge.model}] [{badge.tier_color}]{badge.tier_display}[/]"
    )


def get_badge_for_worker(
    worker_id: str,
    role: str,
    model: str,
    tier: str,
    start_time: datetime | None = None,
    status: WorkerStatus = WorkerStatus.RUNNING,
) -> WorkerBadge:
    """Build a WorkerBadge from worker metadata.

    Args:
        worker_id: Unique worker identifier (12-char hex).
        role: Worker role (backend, qa, security, etc.).
        model: Model name (sonnet, haiku, gpt-5.4, etc.).
        tier: Tier name (free, paid, enterprise).
        start_time: When the worker started. Defaults to now if None.
        status: Current worker status. Defaults to RUNNING.

    Returns:
        Immutable WorkerBadge dataclass.
    """
    return WorkerBadge(
        worker_id=worker_id,
        role=role,
        model=model,
        tier=tier,
        start_time=start_time or datetime.now(tz=UTC),
        status=status,
    )
