"""Response-cache inspection and maintenance commands."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.semantic_cache import ResponseCacheManager, SemanticCacheEntry


def _entry_to_dict(entry: SemanticCacheEntry) -> dict[str, Any]:
    """Convert a cache entry into a JSON-safe summary payload."""
    return {
        "source_task_id": entry.source_task_id,
        "verified": entry.verified,
        "git_diff_lines": entry.git_diff_lines,
        "hit_count": entry.hit_count,
        "created_at": entry.created_at,
        "last_used_at": entry.last_used_at,
        "response": entry.response,
        "key_text": entry.key_text,
    }


def _format_age(ts: float | None) -> str:
    """Return a short age label for a cache timestamp."""
    if ts is None:
        return "never"
    age_s = max(0, int(time.time() - ts))
    if age_s < 60:
        return f"{age_s}s"
    if age_s < 3600:
        return f"{age_s // 60}m"
    if age_s < 86_400:
        return f"{age_s // 3600}h"
    return f"{age_s // 86_400}d"


@click.group("cache")
def cache_group() -> None:
    """Inspect and manage the response cache."""


@cache_group.command("list")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    show_default=True,
    help="Project root containing .sdd/caching/response_cache.jsonl.",
)
@click.option("--limit", type=int, default=25, show_default=True, help="Maximum number of entries to show.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def list_cache_entries(workdir: Path, limit: int, as_json: bool) -> None:
    """List cached task-result entries."""
    mgr = ResponseCacheManager(workdir.resolve())
    entries = mgr.list_entries()
    if limit > 0:
        entries = entries[:limit]

    if as_json:
        click.echo(json.dumps({"entries": [_entry_to_dict(entry) for entry in entries]}, indent=2))
        return

    if not entries:
        console.print("[dim]No response-cache entries found.[/dim]")
        return

    table = Table(title="Response Cache", header_style="bold cyan")
    table.add_column("Task", style="dim", min_width=12)
    table.add_column("Verified", justify="center")
    table.add_column("Diff", justify="right")
    table.add_column("Hits", justify="right")
    table.add_column("Age", justify="right")
    table.add_column("Summary", min_width=30)

    for entry in entries:
        table.add_row(
            entry.source_task_id or "—",
            "yes" if entry.verified else "no",
            str(entry.git_diff_lines),
            str(entry.hit_count),
            _format_age(entry.last_used_at or entry.created_at),
            entry.response[:120],
        )
    console.print(table)


@cache_group.command("inspect")
@click.argument("task_id")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    show_default=True,
    help="Project root containing .sdd/caching/response_cache.jsonl.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def inspect_cache_entry(task_id: str, workdir: Path, as_json: bool) -> None:
    """Inspect the cached result produced by a specific task."""
    mgr = ResponseCacheManager(workdir.resolve())
    entry = mgr.inspect_task(task_id)
    if entry is None:
        if as_json:
            click.echo(json.dumps({"error": f"No cache entry found for task {task_id}"}, indent=2))
        else:
            console.print(f"[red]No cache entry found for task {task_id}.[/red]")
        raise SystemExit(1)

    payload = _entry_to_dict(entry)
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    console.print(f"[bold]Task:[/bold] {task_id}")
    console.print(f"[bold]Verified:[/bold] {'yes' if entry.verified else 'no'}")
    console.print(f"[bold]Diff lines:[/bold] {entry.git_diff_lines}")
    console.print(f"[bold]Hits:[/bold] {entry.hit_count}")
    console.print(f"[bold]Created:[/bold] {_format_age(entry.created_at)} ago")
    console.print(f"[bold]Last used:[/bold] {_format_age(entry.last_used_at)} ago")
    console.print(f"[bold]Key:[/bold] {entry.key_text}")
    console.print(f"[bold]Response:[/bold] {entry.response}")


@cache_group.command("clear")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    show_default=True,
    help="Project root containing .sdd/caching/response_cache.jsonl.",
)
@click.option(
    "--unverified",
    "unverified_only",
    is_flag=True,
    default=False,
    help="Remove only unverified cache entries.",
)
@click.option("--yes", is_flag=True, default=False, help="Skip confirmation prompt.")
def clear_cache_entries(workdir: Path, unverified_only: bool, yes: bool) -> None:
    """Clear response-cache entries."""
    target = "unverified entries" if unverified_only else "all entries"
    if not yes and not click.confirm(f"Delete {target} from the response cache?"):
        raise SystemExit(1)

    mgr = ResponseCacheManager(workdir.resolve())
    removed = mgr.clear(unverified_only=unverified_only)
    console.print(f"[green]Removed {removed} response-cache entr{'y' if removed == 1 else 'ies'}.[/green]")
