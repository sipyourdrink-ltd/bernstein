"""Formatted status output for ``bernstein status``.

Uses Rich tables and colour-coded rows to display task counts,
active agents, total cost, and elapsed time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from rich.console import Console

from bernstein.cli.ui import (
    STATUS_COLORS,
    AgentInfo,
    AgentStatusTable,
    RunStats,
    TaskSummary,
    create_summary_plain,
    create_summary_table,
    format_duration,
    make_console,
)

# ---------------------------------------------------------------------------
# Task table
# ---------------------------------------------------------------------------


def _build_task_table(tasks: list[dict[str, Any]]) -> Table:
    """Build a Rich table of individual tasks with colour-coded statuses.

    Args:
        tasks: List of raw task dicts from the server API.

    Returns:
        A Rich Table renderable.
    """
    table = Table(
        title="Tasks",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("ID", style="dim", min_width=10)
    table.add_column("Title", min_width=30)
    table.add_column("Role", min_width=10)
    table.add_column("Status", min_width=14)
    table.add_column("Priority", justify="right")
    table.add_column("Agent", min_width=12)

    for t in tasks:
        raw_status = str(t.get("status", "open"))
        color = STATUS_COLORS.get(raw_status, "white")
        table.add_row(
            str(t.get("id", "\u2014")),
            str(t.get("title", "\u2014")),
            str(t.get("role", "\u2014")),
            f"[{color}]{raw_status}[/{color}]",
            str(t.get("priority", 2)),
            str(t.get("assigned_agent") or "[dim]\u2014[/dim]"),
        )
    return table


# ---------------------------------------------------------------------------
# Cost breakdown table
# ---------------------------------------------------------------------------


def _build_cost_table(per_role: list[dict[str, Any]]) -> Table | None:
    """Build a per-role cost breakdown table.

    Args:
        per_role: List of role-cost dicts from the status API.

    Returns:
        A Rich Table, or None if there is no cost data.
    """
    roles_with_cost = [r for r in per_role if float(r.get("cost_usd", 0.0)) > 0.0]
    if not roles_with_cost:
        return None

    table = Table(
        title="Cost by Role",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("Role", min_width=12)
    table.add_column("Tasks", justify="right")
    table.add_column("Cost", justify="right")

    for r in sorted(roles_with_cost, key=lambda x: float(x.get("cost_usd", 0.0)), reverse=True):
        role_tasks = int(r.get("done", 0)) + int(r.get("failed", 0)) + int(r.get("claimed", 0)) + int(r.get("open", 0))
        table.add_row(
            str(r.get("role", "\u2014")),
            str(role_tasks),
            f"${float(r.get('cost_usd', 0.0)):.4f}",
        )
    return table


def _build_provider_table(provider_status: dict[str, Any]) -> Table | None:
    """Build a provider/quota table from persisted orchestrator status."""
    providers_obj = provider_status.get("providers")
    if not isinstance(providers_obj, dict) or not providers_obj:
        return None

    table = Table(title="Providers", show_lines=False, header_style="bold cyan")
    table.add_column("Provider", min_width=12)
    table.add_column("Health", min_width=12)
    table.add_column("Tier", min_width=10)
    table.add_column("Model", min_width=20)
    table.add_column("Quota", min_width=18)

    providers = cast("dict[str, object]", providers_obj)
    for provider_name, payload_obj in sorted(providers.items(), key=lambda item: item[0]):
        if not isinstance(payload_obj, dict):
            continue
        payload = cast("dict[str, object]", payload_obj)
        snapshot_obj = payload.get("quota_snapshot")
        snapshot = cast("dict[str, object]", snapshot_obj) if isinstance(snapshot_obj, dict) else {}
        quota = "unknown"
        rpm_obj = snapshot.get("requests_per_minute")
        tpm_obj = snapshot.get("tokens_per_minute")
        rpm = int(rpm_obj) if isinstance(rpm_obj, int) else None
        tpm = int(tpm_obj) if isinstance(tpm_obj, int) else None
        if rpm is not None or tpm is not None:
            parts: list[str] = []
            if rpm is not None:
                parts.append(f"{rpm}/m")
            if tpm is not None:
                parts.append(f"{tpm} tok/m")
            quota = " ".join(parts)
        table.add_row(
            provider_name,
            str(payload.get("health", "unknown")),
            str(payload.get("tier", "unknown")),
            str(payload.get("model", "—")),
            quota,
        )
    return table


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_status(
    data: dict[str, Any],
    *,
    console: Console | None = None,
) -> None:
    """Render a full status display from task-server response data.

    Shows: task table, agent table, summary stats, cost info.
    Falls back to plain text when stdout is not a TTY.

    Args:
        data: Dict returned by the ``/status`` endpoint.
        console: Optional Rich Console to use.
    """
    con = console or make_console()

    tasks: list[dict[str, Any]] = data.get("tasks", [])
    agents_raw: list[dict[str, Any]] = data.get("agents", [])
    summary_raw: dict[str, Any] = data.get("summary", {})
    per_role: list[dict[str, Any]] = data.get("per_role", [])
    provider_status_obj = data.get("provider_status", {})
    provider_status = cast("dict[str, Any]", provider_status_obj) if isinstance(provider_status_obj, dict) else {}

    summary = TaskSummary.from_dict(
        {
            "total": summary_raw.get("total", len(tasks)),
            "done": summary_raw.get("done", sum(1 for t in tasks if t.get("status") == "done")),
            "in_progress": summary_raw.get(
                "in_progress",
                sum(1 for t in tasks if t.get("status") == "in_progress"),
            ),
            "failed": summary_raw.get("failed", sum(1 for t in tasks if t.get("status") == "failed")),
            "open": summary_raw.get("open", sum(1 for t in tasks if t.get("status") == "open")),
        }
    )
    agents = [AgentInfo.from_dict(a) for a in agents_raw]
    elapsed = float(data.get("elapsed_seconds", 0))
    total_cost = float(data.get("total_cost_usd", 0.0))

    stats = RunStats(
        summary=summary,
        agents=agents,
        elapsed_seconds=elapsed,
        total_cost_usd=total_cost,
    )

    # Non-TTY: plain text
    if not con.is_terminal:
        con.print(create_summary_plain(stats))
        return

    # Task table
    if tasks:
        con.print(_build_task_table(tasks))

    # Agent table
    if agents:
        agent_table = AgentStatusTable()
        con.print(agent_table.render(agents))
    else:
        con.print("[dim]No active agents.[/dim]")

    # Summary line
    con.print()
    status_line = Text()
    status_line.append("Tasks: ", style="bold")
    status_line.append(f"{summary.total} total  ", style="bold")
    status_line.append(f"{summary.done} done  ", style="green")
    status_line.append(f"{summary.in_progress} in progress  ", style="yellow")
    status_line.append(f"{summary.failed} failed", style="red")
    con.print(status_line)

    if elapsed > 0:
        con.print(Text.assemble(("Elapsed: ", "bold"), (format_duration(elapsed), "dim")))

    # Cost
    if total_cost > 0 or per_role:
        con.print(
            Text.assemble(
                ("\nTotal spend: ", "bold"),
                (f"${total_cost:.4f}", "bold green"),
            )
        )
        cost_table = _build_cost_table(per_role)
        if cost_table is not None:
            con.print(cost_table)

    provider_table = _build_provider_table(provider_status)
    if provider_table is not None:
        con.print()
        con.print(provider_table)

    # Clean summary table
    con.print()
    con.print(create_summary_table(stats))


def render_status_plain(data: dict[str, Any]) -> str:
    """Return a plain-text status string for non-interactive use.

    Useful for piping output or machine-readable contexts where Rich
    markup would be undesirable.

    Args:
        data: Dict returned by the ``/status`` endpoint.

    Returns:
        A multi-line plain string.
    """
    tasks: list[dict[str, Any]] = data.get("tasks", [])
    summary_raw: dict[str, Any] = data.get("summary", {})
    agents_raw: list[dict[str, Any]] = data.get("agents", [])

    summary = TaskSummary.from_dict(
        {
            "total": summary_raw.get("total", len(tasks)),
            "done": summary_raw.get("done", 0),
            "in_progress": summary_raw.get("in_progress", 0),
            "failed": summary_raw.get("failed", 0),
        }
    )
    agents = [AgentInfo.from_dict(a) for a in agents_raw]
    elapsed = float(data.get("elapsed_seconds", 0))
    total_cost = float(data.get("total_cost_usd", 0.0))

    stats = RunStats(
        summary=summary,
        agents=agents,
        elapsed_seconds=elapsed,
        total_cost_usd=total_cost,
    )
    return create_summary_plain(stats)
