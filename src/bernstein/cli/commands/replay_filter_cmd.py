"""``bernstein replay`` with filtering and search.

CLI-010: Replay events from a previous run with --filter, --search,
--event-type, and --agent filtering options.

This module provides the enhanced replay command with filtering
capabilities.  The original replay command in advanced_cmd.py is
preserved; this adds filter/search wrappers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import console


def _matches_filter(
    event: dict[str, Any],
    *,
    filter_str: str | None,
    event_type: str | None,
    agent: str | None,
    search: str | None,
) -> bool:
    """Check if an event matches the given filters.

    Args:
        event: Replay event dict.
        filter_str: Glob-style filter string (key=value).
        event_type: Filter by event type name.
        agent: Filter by agent ID prefix.
        search: Text search across all event fields.

    Returns:
        True if the event passes all filters.
    """
    if event_type and event.get("event") != event_type:
        return False

    if agent:
        agent_id = str(event.get("agent_id", ""))
        if agent not in agent_id:
            return False

    if search:
        event_str = json.dumps(event, default=str).lower()
        if search.lower() not in event_str:
            return False

    if filter_str:
        # Support key=value filters like "status=done" or "role=backend"
        for part in filter_str.split(","):
            part = part.strip()
            if "=" in part:
                key, value = part.split("=", 1)
                key = key.strip()
                value = value.strip()
                event_val = str(event.get(key, ""))
                if not re.search(value, event_val, re.IGNORECASE):
                    return False
    return True


def filter_events(
    events: list[dict[str, Any]],
    *,
    filter_str: str | None = None,
    event_type: str | None = None,
    agent: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """Filter replay events based on criteria.

    Args:
        events: List of replay event dicts.
        filter_str: Key=value filter expressions (comma-separated).
        event_type: Filter by event type.
        agent: Filter by agent ID prefix.
        search: Full-text search across event fields.

    Returns:
        Filtered list of events.
    """
    if not any([filter_str, event_type, agent, search]):
        return events
    return [
        ev
        for ev in events
        if _matches_filter(ev, filter_str=filter_str, event_type=event_type, agent=agent, search=search)
    ]


def _handle_task_trace_replay(
    sdd_path: Path,
    run_id: str,
    model: str | None,
    extra_context: str | None,
    trace_store_cls: type,
    build_request_fn: Any,
) -> None:
    """Delegate to task trace replay when no run log exists."""
    trace = trace_store_cls(sdd_path / "traces").latest_for_task(run_id)
    if trace is None:
        console.print(f"[red]No trace found for task:[/red] {run_id}")
        raise SystemExit(1)

    from bernstein.cli.helpers import server_post

    request = build_request_fn(trace, task_id=run_id, override_model=model, extra_context=extra_context)
    created = server_post("/tasks", request.to_payload())
    if created is None:
        console.print("[red]Failed to create replay task.[/red]")
        raise SystemExit(1)

    console.print(f"[green]Replay task created:[/green] {created.get('id', '')}")


def _replay_filter_find_run_dirs(runs_dir: Path, replay_jsonl: str) -> list[Path]:
    """Find run directories containing replay logs."""
    if not runs_dir.exists():
        return []
    return sorted(
        (d for d in runs_dir.iterdir() if d.is_dir() and (d / replay_jsonl).exists()),
        key=lambda d: d.name,
        reverse=True,
    )


def _replay_filter_list_runs(runs_dir: Path, replay_jsonl: str) -> None:
    """List available replay runs."""
    run_dirs = _replay_filter_find_run_dirs(runs_dir, replay_jsonl)
    if not run_dirs:
        console.print("[dim]No runs recorded yet.[/dim]")
        return
    from rich.table import Table

    table = Table(title="Available Runs", show_header=True, header_style="bold cyan")
    table.add_column("Run ID")
    table.add_column("Events", justify="right")
    for d in run_dirs:
        replay_file = d / replay_jsonl
        event_count = sum(1 for line in replay_file.read_text().splitlines() if line.strip())
        table.add_row(d.name, str(event_count))
    console.print(table)


def _replay_filter_resolve_latest(runs_dir: Path, replay_jsonl: str) -> str:
    """Resolve 'latest' run ID or exit."""
    run_dirs = _replay_filter_find_run_dirs(runs_dir, replay_jsonl)
    if not run_dirs:
        console.print("[red]No replay logs found.[/red]")
        raise SystemExit(1)
    latest = run_dirs[0].name
    console.print(f"[dim]Replaying latest run:[/dim] {latest}")
    return latest


@click.command("replay")
@click.argument("run_id")
@click.option("--sdd-dir", default=".sdd", show_default=True, help="Path to .sdd state directory.")
@click.option("--as-json", "as_json", is_flag=True, default=False, help="Output raw JSONL events.")
@click.option("--limit", type=int, default=None, help="Show only the first N events.")
@click.option("--filter", "filter_str", default=None, help="Filter events by key=value (comma-separated).")
@click.option("--event-type", default=None, help="Filter by event type (e.g. agent_spawned, task_completed).")
@click.option("--agent", default=None, help="Filter by agent ID (prefix match).")
@click.option("--search", default=None, help="Full-text search across event fields.")
@click.option("--model", default=None, help="Override model for task-trace replay.")
@click.option("--extra-context", default=None, help="Append additional hint text to the replayed task description.")
def replay_filter_cmd(
    run_id: str,
    sdd_dir: str,
    as_json: bool,
    limit: int | None,
    filter_str: str | None,
    event_type: str | None,
    agent: str | None,
    search: str | None,
    model: str | None,
    extra_context: str | None,
) -> None:
    """Replay events from a previous run with filtering and search.

    \b
    Supports all the original replay features plus:
      --filter key=value   Filter events by field values
      --event-type TYPE    Show only events of a specific type
      --agent ID           Show only events from a specific agent
      --search TEXT        Full-text search across all event fields

    \b
    Examples:
      bernstein replay latest --event-type agent_spawned
      bernstein replay latest --agent backend
      bernstein replay latest --search "failed"
      bernstein replay latest --filter "role=backend,status=done"
      bernstein replay latest --limit 20 --event-type task_completed
    """

    from bernstein.core.traces import TraceStore, build_replay_task_request

    sdd_path = Path(sdd_dir)
    runs_dir = sdd_path / "runs"

    _REPLAY_JSONL = "replay.jsonl"

    has_filters = any([filter_str, event_type, agent, search])
    is_run_replay = run_id in {"list", "latest"} or (runs_dir / run_id / _REPLAY_JSONL).exists()
    is_task_trace = not is_run_replay and not has_filters

    if is_task_trace:
        _handle_task_trace_replay(sdd_path, run_id, model, extra_context, TraceStore, build_replay_task_request)
        return

    if run_id == "list":
        _replay_filter_list_runs(runs_dir, _REPLAY_JSONL)
        return

    if run_id == "latest":
        run_id = _replay_filter_resolve_latest(runs_dir, _REPLAY_JSONL)

    replay_path = runs_dir / run_id / _REPLAY_JSONL
    if not replay_path.exists():
        console.print(f"[red]Replay log not found:[/red] {replay_path}")
        raise SystemExit(1)

    from bernstein.core.recorder import load_replay_events

    events = load_replay_events(replay_path)
    if not events:
        console.print("[yellow]Replay log is empty.[/yellow]")
        return

    # Apply filters
    events = filter_events(events, filter_str=filter_str, event_type=event_type, agent=agent, search=search)

    if not events:
        console.print("[yellow]No events match the given filters.[/yellow]")
        return

    if as_json:
        console.print_json(json.dumps({"run_id": run_id, "events": events[:limit], "total_matched": len(events)}))
        return

    # Apply limit after filtering
    displayed = events[:limit] if limit else events

    from rich.panel import Panel
    from rich.table import Table

    # Header
    filter_parts: list[str] = []
    if event_type:
        filter_parts.append(f"type={event_type}")
    if agent:
        filter_parts.append(f"agent={agent}")
    if search:
        filter_parts.append(f"search={search!r}")
    if filter_str:
        filter_parts.append(f"filter={filter_str}")
    filter_label = f"  Filters: {', '.join(filter_parts)}" if filter_parts else ""

    header_text = (
        f"Run: [bold cyan]{run_id}[/bold cyan]  "
        f"Matched: [bold]{len(events)}[/bold] / total events  "
        f"Showing: [bold]{len(displayed)}[/bold]"
        f"{filter_label}"
    )
    console.print(Panel(header_text, title="Filtered Replay", border_style="cyan"))

    # Event table
    table = Table(show_header=True, header_style="bold cyan", expand=True)
    table.add_column("TIME", style="dim", width=8)
    table.add_column("EVENT", width=24)
    table.add_column("AGENT", width=16)
    table.add_column("TASK", width=32)
    table.add_column("DETAIL")

    _EVENT_STYLES: dict[str, str] = {
        "run_started": "bold bright_green",
        "run_completed": "bold bright_green",
        "agent_spawned": "bold bright_cyan",
        "agent_reaped": "bold bright_red",
        "task_claimed": "bright_yellow",
        "task_completed": "bright_green",
        "task_verification_failed": "bright_red",
    }

    for ev in displayed:
        elapsed = float(ev.get("elapsed_s", 0))
        em, es = divmod(int(elapsed), 60)
        time_str = f"{em}:{es:02d}"

        event_name = ev.get("event", "?")
        style = _EVENT_STYLES.get(event_name, "")

        agent_id = str(ev.get("agent_id", ""))
        if agent_id and "-" in agent_id and len(agent_id.split("-", 1)[1]) > 6:
            parts = agent_id.split("-", 1)
            agent_id = f"{parts[0]}-{parts[1][:6]}"

        task_id = ev.get("task_id", "")

        detail_parts: list[str] = []
        if ev.get("model"):
            detail_parts.append(str(ev["model"]))
        if ev.get("role"):
            detail_parts.append(str(ev["role"]))
        if ev.get("cost_usd"):
            detail_parts.append(f"${ev['cost_usd']:.4f}")
        detail = "  ".join(detail_parts)

        table.add_row(
            time_str,
            f"[{style}]{event_name}[/{style}]" if style else event_name,
            agent_id,
            str(task_id),
            detail,
        )

    console.print(table)
