"""Bernstein memory — manage persistent agent memories."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import click
from rich.console import Console
from rich.table import Table

from bernstein.core.memory.sqlite_store import MemoryType, SQLiteMemoryStore

console = Console()


@click.group("memory")
def memory_group() -> None:
    """Manage persistent memories (conventions, decisions, learnings)."""
    pass


def _coerce_memory_type(value: str | None) -> MemoryType | None:
    """Convert a validated CLI value into the narrow ``MemoryType`` union."""
    return cast("MemoryType | None", value)


@memory_group.command("list")
@click.option(
    "--type",
    "memory_type",
    type=click.Choice(["convention", "decision", "learning"]),
    help="Filter by type.",
)
@click.option("--tag", multiple=True, help="Filter by tag.")
@click.option("--limit", default=20, help="Max entries to show.")
def list_memory(memory_type: str | None, tag: list[str], limit: int) -> None:
    """List stored memories."""
    db_path = Path(".sdd/memory/memory.db")
    if not db_path.exists():
        console.print("[dim]No memory database found.[/dim]")
        return

    store = SQLiteMemoryStore(db_path)
    entries = store.list(type=_coerce_memory_type(memory_type), tags=tag if tag else None, limit=limit)

    if not entries:
        console.print("[dim]No matching memories found.[/dim]")
        return

    table = Table(title="Persistent Memory")
    table.add_column("ID", style="dim")
    table.add_column("Type", style="cyan")
    table.add_column("Tags", style="green")
    table.add_column("Content")
    table.add_column("Age", style="dim")

    import datetime

    now = datetime.datetime.now()
    for e in entries:
        dt = datetime.datetime.fromtimestamp(e.created_at)
        age = str(now - dt).split(".")[0]
        table.add_row(
            str(e.id),
            e.type,
            ", ".join(e.tags),
            e.content[:100] + ("..." if len(e.content) > 100 else ""),
            f"{age} ago",
        )

    console.print(table)


@memory_group.command("add")
@click.argument("content")
@click.option(
    "--type",
    "memory_type",
    type=click.Choice(["convention", "decision", "learning"]),
    default="convention",
)
@click.option("--tag", multiple=True, help="Tags for this memory.")
def add_memory(content: str, memory_type: str, tag: list[str]) -> None:
    """Add a new persistent memory entry."""
    db_path = Path(".sdd/memory/memory.db")
    store = SQLiteMemoryStore(db_path)
    entry_id = store.add(type=cast("MemoryType", memory_type), content=content, tags=list(tag))
    console.print(f"[green]✓[/green] Added memory entry [bold]#{entry_id}[/bold]")


@memory_group.command("remove")
@click.argument("entry_id", type=int)
def remove_memory(entry_id: int) -> None:
    """Remove a memory entry by ID."""
    db_path = Path(".sdd/memory/memory.db")
    if not db_path.exists():
        return
    store = SQLiteMemoryStore(db_path)
    if store.remove(entry_id):
        console.print(f"[green]✓[/green] Removed memory entry [bold]#{entry_id}[/bold]")
    else:
        console.print(f"[red]✗[/red] Entry [bold]#{entry_id}[/bold] not found")
