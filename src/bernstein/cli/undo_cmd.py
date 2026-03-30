"""Bernstein undo — revert agent changes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel

console = Console()


@click.command("undo")
@click.argument("task_id", required=False)
@click.option("--all", "revert_all", is_flag=True, help="Revert all changes from the current session.")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def undo_cmd(task_id: str | None, revert_all: bool, yes: bool) -> None:
    """Revert changes made by an agent.

    \b
      bernstein undo task-123    # revert changes from a specific task
      bernstein undo --all       # revert all changes in current session
    """
    if not task_id and not revert_all:
        console.print("[red]Error:[/red] Specify a task ID or use --all")
        return

    # Find commits to revert
    # We look for commits with "[bernstein] task:<id>" in the message
    commits_to_revert: list[tuple[str, str]] = []  # (hash, subject)

    try:
        # Get last 50 commits to search
        res = subprocess.run(
            ["git", "log", "-n", "50", "--pretty=format:%H %s"],
            capture_output=True,
            text=True,
            cwd=".",
        )
        for line in res.stdout.splitlines():
            h, s = line.split(" ", 1)
            if (revert_all and "[bernstein]" in s) or (task_id and f"task:{task_id}" in s):
                commits_to_revert.append((h, s))
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to read git log: {exc}")
        return

    if not commits_to_revert:
        console.print("[yellow]No matching commits found to undo.[/yellow]")
        return

    # Show preview
    console.print(Panel(
        "\n".join([f"- [cyan]{h[:8]}[/cyan] {s}" for h, s in commits_to_revert]),
        title="Commits to REVERT",
        border_style="yellow",
    ))

    if not yes and not click.confirm("\nProceed with revert?", default=False):
        console.print("[dim]Cancelled.[/dim]")
        return

    # Revert in reverse order (newest first)
    success_count = 0
    for h, s in commits_to_revert:
        console.print(f"Reverting [cyan]{h[:8]}[/cyan] {s}...")
        try:
            # We use git revert -n to avoid individual commits if we want to batch them,
            # but the ticket implies individual revert commits.
            subprocess.run(["git", "revert", "--no-edit", h], check=True, capture_output=True)
            success_count += 1
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Failed to revert {h[:8]}:[/red] {e.stderr.decode()}")
            if not click.confirm("Continue with remaining reverts?", default=True):
                break

    if success_count > 0:
        console.print(f"\n[green]✓[/green] Successfully reverted {success_count} commit(s).")

        # Audit trail
        try:
            from bernstein.core.lifecycle import get_audit_log
            audit = get_audit_log()
            if audit:
                audit.log(
                    event_type="git.undo",
                    actor="user",
                    resource_type="session",
                    resource_id=task_id or "all",
                    details={
                        "action": "revert",
                        "commit_count": success_count,
                        "task_id": task_id,
                        "revert_all": revert_all,
                    }
                )
        except Exception:
            pass

        # Post-revert safety: run tests

        console.print("\n[bold]Running verification tests...[/bold]")
        try:
            # Try to find a test command
            test_cmd = ["pytest", "-q"] if Path("tests").exists() else ["npm", "test"]
            subprocess.run(test_cmd, check=True)
            console.print("[green]✓[/green] Tests passed after rollback.")
        except Exception:
            console.print("[yellow]⚠ Tests failed after rollback. Manual inspection required.[/yellow]")
