"""Agent utilization tracking and display.

Computes per-agent active vs idle time from status transition logs
and renders a Rich table showing utilization percentages with colored
bars.  Used by the CLI dashboard to surface idle-heavy agents that
may need model or scope adjustments.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from rich.console import Console
from rich.table import Table
from rich.text import Text


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UtilizationRecord:
    """Per-agent utilization snapshot.

    Attributes:
        agent_id: Unique agent identifier.
        role: Agent role (e.g. "backend", "qa").
        model: Model string used by the agent.
        active_seconds: Cumulative time in "working" status.
        idle_seconds: Cumulative time in "idle" or "starting" status.
        total_seconds: ``active_seconds + idle_seconds``.
        utilization_pct: ``active_seconds / total_seconds * 100``.
    """

    agent_id: str
    role: str
    model: str
    active_seconds: float
    idle_seconds: float
    total_seconds: float
    utilization_pct: float


@dataclass(frozen=True)
class UtilizationSummary:
    """Aggregate utilization across all tracked agents.

    Attributes:
        total_agents: Number of agents included.
        avg_utilization_pct: Mean utilization percentage.
        most_utilized: Agent ID with highest utilization.
        least_utilized: Agent ID with lowest utilization.
        total_active_seconds: Sum of active seconds across agents.
        total_idle_seconds: Sum of idle seconds across agents.
    """

    total_agents: int
    avg_utilization_pct: float
    most_utilized: str
    least_utilized: str
    total_active_seconds: float
    total_idle_seconds: float


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES: frozenset[str] = frozenset({"working"})
_IDLE_STATUSES: frozenset[str] = frozenset({"idle", "starting"})


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------


def compute_utilization(
    agent_id: str,
    status_transitions: list[tuple[float, str]],
    *,
    role: str = "",
    model: str = "",
) -> UtilizationRecord:
    """Compute utilization from a chronological list of status transitions.

    Each element is ``(timestamp, status)`` where *status* is one of
    ``"starting"``, ``"working"``, ``"idle"``, or ``"dead"``.  The last
    transition's timestamp marks the end of observation.

    Args:
        agent_id: Unique agent identifier.
        status_transitions: Ordered ``(unix_ts, status)`` pairs.
        role: Agent role for display purposes.
        model: Model string for display purposes.

    Returns:
        A frozen ``UtilizationRecord``.
    """
    active: float = 0.0
    idle: float = 0.0

    if len(status_transitions) < 2:
        # Cannot compute intervals from fewer than 2 data points.
        return UtilizationRecord(
            agent_id=agent_id,
            role=role,
            model=model,
            active_seconds=0.0,
            idle_seconds=0.0,
            total_seconds=0.0,
            utilization_pct=0.0,
        )

    for i in range(len(status_transitions) - 1):
        ts, status = status_transitions[i]
        next_ts = status_transitions[i + 1][0]
        duration = max(next_ts - ts, 0.0)

        if status in _ACTIVE_STATUSES:
            active += duration
        elif status in _IDLE_STATUSES:
            idle += duration
        # "dead" or unknown statuses are not counted toward either bucket.

    total = active + idle
    pct = (active / total * 100.0) if total > 0.0 else 0.0

    return UtilizationRecord(
        agent_id=agent_id,
        role=role,
        model=model,
        active_seconds=round(active, 2),
        idle_seconds=round(idle, 2),
        total_seconds=round(total, 2),
        utilization_pct=round(pct, 1),
    )


def summarize_utilization(
    records: list[UtilizationRecord],
) -> UtilizationSummary:
    """Aggregate multiple ``UtilizationRecord`` objects into a summary.

    Args:
        records: List of per-agent records.

    Returns:
        A frozen ``UtilizationSummary``.
    """
    if not records:
        return UtilizationSummary(
            total_agents=0,
            avg_utilization_pct=0.0,
            most_utilized="",
            least_utilized="",
            total_active_seconds=0.0,
            total_idle_seconds=0.0,
        )

    total_active = sum(r.active_seconds for r in records)
    total_idle = sum(r.idle_seconds for r in records)
    avg_pct = sum(r.utilization_pct for r in records) / len(records)

    most = max(records, key=lambda r: r.utilization_pct)
    least = min(records, key=lambda r: r.utilization_pct)

    return UtilizationSummary(
        total_agents=len(records),
        avg_utilization_pct=round(avg_pct, 1),
        most_utilized=most.agent_id,
        least_utilized=least.agent_id,
        total_active_seconds=round(total_active, 2),
        total_idle_seconds=round(total_idle, 2),
    )


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

_BAR_WIDTH: int = 20


def _utilization_bar(pct: float) -> Text:
    """Render a colored bar representing utilization percentage."""
    filled = round(pct / 100.0 * _BAR_WIDTH)
    filled = max(0, min(filled, _BAR_WIDTH))
    empty = _BAR_WIDTH - filled

    if pct >= 70.0:
        color = "green"
    elif pct >= 40.0:
        color = "yellow"
    else:
        color = "red"

    bar = Text()
    bar.append("\u2588" * filled, style=color)
    bar.append("\u2591" * empty, style="dim")
    return bar


def format_utilization_table(
    records: list[UtilizationRecord],
) -> str:
    """Render a Rich table showing per-agent utilization.

    Args:
        records: Per-agent utilization records.

    Returns:
        A string containing the rendered table.
    """
    table = Table(
        title="Agent Utilization",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Agent", style="cyan")
    table.add_column("Role", style="dim")
    table.add_column("Model", style="dim")
    table.add_column("Active (s)", justify="right")
    table.add_column("Idle (s)", justify="right")
    table.add_column("Util %", justify="right")
    table.add_column("Bar")

    for r in sorted(records, key=lambda r: r.utilization_pct, reverse=True):
        table.add_row(
            r.agent_id,
            r.role,
            r.model,
            f"{r.active_seconds:.1f}",
            f"{r.idle_seconds:.1f}",
            f"{r.utilization_pct:.1f}%",
            _utilization_bar(r.utilization_pct),
        )

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    console.print(table)
    return buf.getvalue()
