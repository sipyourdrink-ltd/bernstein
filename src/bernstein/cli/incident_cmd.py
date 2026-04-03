"""Incident timeline CLI command.

Provides `bernstein incident` to view incident timelines
correlated from logs, metrics, and traces.
"""

from __future__ import annotations

import json
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import find_seed_file, server_get

console = Console()


_KIND_COLORS = {
    "error": "red",
    "task_failed": "red",
    "task_completed": "green",
    "agent_spawned": "cyan",
    "agent_crashed": "bold red",
    "slo_breach": "yellow",
    "incident_created": "bold magenta",
    "incident_mitigated": "yellow",
    "incident_resolved": "bold green",
    "trace_step": "blue",
    "metric_anomaly": "bright_red",
}

_KIND_ICONS = {
    "error": "X",
    "task_failed": "X",
    "task_completed": "+",
    "agent_spawned": ">",
    "agent_crashed": "!",
    "slo_breach": "~",
    "incident_created": "!!",
    "incident_mitigated": "-",
    "incident_resolved": "ok",
    "trace_step": ".",
    "metric_anomaly": "!",
}


@click.command("incident")
@click.argument("incident_id", required=False)
@click.option("--list", "list_mode", is_flag=True, help="List all incidents.")
@click.option("--json", "json_mode", is_flag=True, help="Output raw JSON.")
@click.option(
    "--window-before",
    default=600,
    show_default=True,
    help="Seconds before incident to include.",
)
@click.option(
    "--window-after",
    default=300,
    show_default=True,
    help="Seconds after incident to include.",
)
def incident_cmd(
    incident_id: str | None,
    list_mode: bool,
    json_mode: bool,
    window_before: int,
    window_after: int,
) -> None:
    """View incident timelines correlated from logs, metrics, and traces.

    Pass an INCIDENT_ID to view its timeline, or --list to see all incidents.
    """
    seed_path = find_seed_file()
    _ = seed_path

    if list_mode:
        _list_incidents(json_mode)
        return

    if not incident_id:
        # Show help if no args
        ctx = click.get_current_context()
        click.echo(ctx.get_help())
        ctx.exit()
        return

    _show_timeline(incident_id, json_mode, window_before, window_after)


def _list_incidents(json_mode: bool) -> None:
    """List all known incidents."""
    try:
        data = server_get("/observability/incidents")
    except Exception as exc:
        console.print(f"[red]Error fetching incidents: {exc}[/red]")
        return

    if data is None:
        console.print("[red]No response from server.[/red]")
        return

    incidents: list[dict[str, Any]] = data.get("incidents", [])

    if json_mode:
        click.echo(json.dumps(incidents, indent=2))
        return

    if not incidents:
        console.print("[dim]No incidents recorded.[/dim]")
        return

    table = Table(title="Incidents", show_lines=True)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Severity", style="bold")
    table.add_column("Status")
    table.add_column("Title")
    table.add_column("Created")

    import time as _time

    for inc in incidents:
        sev = inc.get("severity", "?")
        sev_style = {"sev1": "bold red", "sev2": "yellow", "sev3": "blue"}.get(sev, "white")
        status = inc.get("status", "?")
        status_style = {
            "open": "bold red",
            "mitigated": "yellow",
            "resolved": "green",
            "post_mortem": "dim",
        }.get(status, "white")
        created = inc.get("created_at", 0)
        created_str = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(created)) if created else "?"
        table.add_row(
            inc.get("id", "?"),
            f"[{sev_style}]{sev.upper()}[/{sev_style}]",
            f"[{status_style}]{status}[/{status_style}]",
            inc.get("title", "?"),
            created_str,
        )

    console.print(table)


def _show_timeline(incident_id: str, json_mode: bool, window_before: int, window_after: int) -> None:
    """Show the timeline for a specific incident."""
    path = (
        f"/observability/incident-timeline/{incident_id}"
        f"?window_before={window_before}&window_after={window_after}"
    )
    try:
        data = server_get(path)
    except Exception as exc:
        console.print(f"[red]Error fetching incident timeline: {exc}[/red]")
        return

    if data is None:
        console.print("[red]No response from server.[/red]")
        return

    if "error" in data:
        console.print(f"[red]{data['error']}[/red]")
        return

    if json_mode:
        click.echo(json.dumps(data, indent=2))
        return

    # Render header
    # Render header
    severity: str = str(data.get("severity", "?"))
    sev_style: str = {"sev1": "bold red", "sev2": "yellow", "sev3": "blue"}.get(severity, "white")
    title: str = str(data.get("title", "?"))
    status: str = str(data.get("status", "?"))
    event_count: int = int(data.get("event_count", 0))

    header = (
        f"Incident [bold cyan]{incident_id}[/bold cyan] "
        f"[{sev_style}]{severity.upper()}[/{sev_style}] — {title}\n"
        f"Status: {status} | Events: {event_count}"
    )

    blast_raw: list[Any] = list(data.get("blast_radius") or [])
    blast: list[str] = [str(x) for x in blast_raw]
    if blast:
        header += f"\nBlast radius: {', '.join(blast[:10])}"
        if len(blast) > 10:
            header += f" (+{len(blast) - 10} more)"

    console.print(Panel(header, title="Incident Timeline", border_style="magenta"))

    # Render timeline events
    events: list[dict[str, Any]] = data.get("events", [])
    if not events:
        console.print("[dim]No timeline events found.[/dim]")
        return

    import time as _time

    table = Table(show_lines=False, pad_edge=False)
    table.add_column("Time", style="dim", no_wrap=True, width=20)
    table.add_column("Kind", width=16)
    table.add_column("Source", width=10)
    table.add_column("Summary")

    for ev in events:
        kind: str = str(ev.get("kind", "?"))
        color: str = _KIND_COLORS.get(kind, "white")
        icon: str = _KIND_ICONS.get(kind, "?")
        ts: float = float(ev.get("timestamp") or 0)
        time_str: str = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(ts)) if ts else "?"
        source: str = str(ev.get("source", "?"))
        summary: str = str(ev.get("summary", "?"))

        table.add_row(
            time_str,
            f"[{color}]{icon} {kind}[/{color}]",
            source,
            summary,
        )

    console.print(table)

    # Print root cause and remediation if available
    root_cause: str = str(data.get("root_cause", ""))
    remediation: str = str(data.get("remediation", ""))
    if root_cause:
        console.print(Panel(root_cause, title="Root Cause", border_style="red"))
    if remediation:
        console.print(Panel(remediation, title="Remediation", border_style="green"))
