"""CLI commands for managing event-driven triggers.

Commands:
  bernstein triggers list     — Show all configured triggers and status
  bernstein triggers history  — Show recent trigger fire log
  bernstein triggers fire     — Manually fire a trigger (for testing)
"""
# TODO(D6): Not yet wired into main.py CLI group. WIP — `fire` subcommand
# uses hard-coded http://127.0.0.1:8052 instead of helpers.SERVER_URL.
# Fix URL handling and wire `cli.add_command(triggers_group, "triggers")`
# in main.py. See p0-documentation-overhaul.md.

from __future__ import annotations

import time
from pathlib import Path

import click


@click.group("triggers")
def triggers_group() -> None:
    """Manage event-driven agent triggers."""


@triggers_group.command("list")
def triggers_list() -> None:
    """Show all configured triggers and their status."""
    from rich.console import Console
    from rich.table import Table

    from bernstein.core.trigger_manager import TriggerManager

    console = Console()
    sdd_dir = Path.cwd() / ".sdd"

    if not sdd_dir.exists():
        console.print("[red]No .sdd/ directory found. Run 'bernstein init' first.[/red]")
        raise SystemExit(1)

    config_path = sdd_dir / "config" / "triggers.yaml"
    if not config_path.exists():
        console.print("[yellow]No triggers configured.[/yellow]")
        console.print(f"[dim]Create {config_path} to define trigger rules.[/dim]")
        return

    mgr = TriggerManager(sdd_dir)
    triggers = mgr.list_triggers()

    if not triggers:
        console.print("[yellow]No triggers defined in triggers.yaml.[/yellow]")
        return

    table = Table(title="Configured Triggers", show_lines=True)
    table.add_column("Name", style="bold cyan")
    table.add_column("Source", style="green")
    table.add_column("Enabled", style="yellow")
    table.add_column("Schedule")
    table.add_column("Last Fired")
    table.add_column("Filters", max_width=40)

    for t in triggers:
        enabled_str = "[green]yes[/green]" if t["enabled"] else "[red]no[/red]"
        schedule = t.get("schedule") or "-"
        last_fired = "-"
        if t.get("last_fired"):
            ago = int(time.time() - t["last_fired"])
            if ago < 60:
                last_fired = f"{ago}s ago"
            elif ago < 3600:
                last_fired = f"{ago // 60}m ago"
            else:
                last_fired = f"{ago // 3600}h ago"
        filters_str = ", ".join(f"{k}={v}" for k, v in t.get("filters", {}).items())[:40]
        table.add_row(t["name"], t["source"], enabled_str, schedule, last_fired, filters_str)

    console.print(table)


@triggers_group.command("history")
@click.option("--limit", "-n", default=20, help="Number of entries to show.")
def triggers_history(limit: int) -> None:
    """Show recent trigger fire log."""
    from rich.console import Console
    from rich.table import Table

    from bernstein.core.trigger_manager import TriggerManager

    console = Console()
    sdd_dir = Path.cwd() / ".sdd"

    if not sdd_dir.exists():
        console.print("[red]No .sdd/ directory found.[/red]")
        raise SystemExit(1)

    mgr = TriggerManager(sdd_dir)
    history = mgr.get_fire_history(limit=limit)

    if not history:
        console.print("[dim]No trigger fire history.[/dim]")
        return

    table = Table(title=f"Trigger Fire History (last {limit})")
    table.add_column("Trigger", style="bold cyan")
    table.add_column("Source", style="green")
    table.add_column("Task ID", style="yellow")
    table.add_column("Fired At")
    table.add_column("Summary", max_width=50)

    for entry in reversed(history):
        fired_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry.get("fired_at", 0)))
        table.add_row(
            entry.get("trigger_name", ""),
            entry.get("source", ""),
            entry.get("task_id", "")[:12],
            fired_at,
            entry.get("event_summary", "")[:50],
        )

    console.print(table)


@triggers_group.command("fire")
@click.argument("name")
def triggers_fire(name: str) -> None:
    """Manually fire a trigger by name (for testing).

    Creates a synthetic event and evaluates it against the named trigger.
    """
    from rich.console import Console

    from bernstein.core.models import TriggerEvent
    from bernstein.core.trigger_manager import TriggerManager

    console = Console()
    sdd_dir = Path.cwd() / ".sdd"

    if not sdd_dir.exists():
        console.print("[red]No .sdd/ directory found.[/red]")
        raise SystemExit(1)

    mgr = TriggerManager(sdd_dir)

    # Find the trigger config
    target = None
    for cfg in mgr.configs:
        if cfg.name == name:
            target = cfg
            break

    if target is None:
        console.print(f"[red]Trigger '{name}' not found.[/red]")
        available = [c.name for c in mgr.configs]
        if available:
            console.print(f"[dim]Available: {', '.join(available)}[/dim]")
        raise SystemExit(1)

    # Synthesize a test event matching the trigger's source
    event = TriggerEvent(
        source=target.source,
        timestamp=time.time(),
        raw_payload={"manual_fire": True, "trigger_name": name},
        message=f"Manual fire of trigger: {name}",
        metadata={"manual": True},
    )

    task_payloads, suppressed = mgr.evaluate(event)

    if suppressed.get(name):
        console.print(f"[yellow]Trigger '{name}' was suppressed: {suppressed[name]}[/yellow]")
        return

    if not task_payloads:
        console.print(f"[yellow]Trigger '{name}' did not produce any tasks.[/yellow]")
        return

    # Print what would be created (dry-run)
    for payload in task_payloads:
        console.print("[green]Task would be created:[/green]")
        console.print(f"  Title: {payload['title']}")
        console.print(f"  Role: {payload['role']}")
        console.print(f"  Priority: {payload['priority']}")

    if not click.confirm("Create task(s) on the server?"):
        console.print("[dim]Cancelled.[/dim]")
        return

    # Create tasks via HTTP
    import httpx

    for payload in task_payloads:
        try:
            resp = httpx.post("http://127.0.0.1:8052/tasks", json=payload, timeout=5)
            if resp.status_code in (200, 201):
                task_id = resp.json().get("id", "unknown")
                console.print(f"[green]Created task {task_id}[/green]")
                mgr.record_fire(
                    trigger_name=name,
                    source=target.source,
                    task_id=task_id,
                    dedup_key="manual",
                    summary="Manual fire from CLI",
                )
            else:
                console.print(f"[red]Failed to create task: {resp.status_code} {resp.text}[/red]")
        except httpx.ConnectError:
            console.print("[red]Cannot connect to task server at http://127.0.0.1:8052[/red]")
            raise SystemExit(1)  # noqa: B904
