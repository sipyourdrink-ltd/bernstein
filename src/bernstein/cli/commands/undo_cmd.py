"""Bernstein undo: revert agent changes."""

from __future__ import annotations

import subprocess
import tempfile
from contextlib import nullcontext, suppress
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from bernstein.core.config.platform_compat import IS_WINDOWS, kill_process_group
from bernstein.core.worktrees.change_set import (
    TaskChangeSet,
    TaskChangeSetUnresolved,
    resolve_task_change_set,
)

console = Console()


@click.command("undo")
@click.argument("task_id", required=False)
@click.option("--all", "revert_all", is_flag=True, help="Revert all changes from the current session.")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the paths the task changed and exit without touching the tree.",
)
def undo_cmd(task_id: str | None, revert_all: bool, yes: bool, dry_run: bool) -> None:
    """Revert changes made by an agent.

    \b
      bernstein undo task-123              # revert changes from a specific task
      bernstein undo task-123 --dry-run    # show what that task changed, change nothing
      bernstein undo --all                 # revert all changes in current session
    """
    if not task_id and not revert_all:
        console.print("[red]Error:[/red] Specify a task ID or use --all")
        return

    if dry_run:
        _print_change_set(task_id)
        return

    commits_to_revert = _find_commits_to_revert(task_id, revert_all)
    if not commits_to_revert:
        console.print("[yellow]No matching commits found to undo.[/yellow]")
        return

    console.print(
        Panel(
            "\n".join([f"- [cyan]{h[:8]}[/cyan] {s}" for h, s in commits_to_revert]),
            title="Commits to REVERT",
            border_style="yellow",
        )
    )

    if not yes and not click.confirm("\nProceed with revert?", default=False):
        console.print("[dim]Cancelled.[/dim]")
        return

    success_count, uncertain_commit = _execute_reverts(commits_to_revert)

    if success_count > 0:
        console.print(f"\n[green]✓[/green] Successfully reverted {success_count} commit(s).")
    if uncertain_commit is not None:
        _log_undo_audit(task_id, revert_all, success_count, uncertain_commit=uncertain_commit)
        raise click.ClickException(
            f"Revert of {uncertain_commit[:8]} timed out. Check git status and recent commits before resuming."
        )
    if success_count > 0:
        _log_undo_audit(task_id, revert_all, success_count)
        _run_post_revert_tests()


def _print_change_set(task_id: str | None) -> None:
    """Print the set of paths ``task_id`` changed, without touching the tree.

    The set comes from the task's own worktree branch (see
    :func:`~bernstein.core.worktrees.change_set.resolve_task_change_set`),
    not from the commit-subject scan the revert path still uses.

    Raises:
        click.UsageError: ``--all`` was given. A change set is a property
            of one task; ``--all`` names none, so there is nothing to
            print, and printing an empty report would read as "this
            session changed nothing".
        click.ClickException: The task's change set could not be resolved.
    """
    if task_id is None:
        msg = "--dry-run needs a task ID: a change set belongs to one task, and --all names none"
        raise click.UsageError(msg)
    try:
        change_set = resolve_task_change_set(Path.cwd(), task_id)
    except TaskChangeSetUnresolved as exc:
        raise click.ClickException(str(exc)) from exc

    console.print(
        Panel(
            _render_change_set(change_set),
            title=f"Change set for {escape(task_id)} ({escape(change_set.branch)})",
            border_style="cyan",
        )
    )
    console.print("[dim]Dry run: nothing was reverted.[/dim]")


def _render_change_set(change_set: TaskChangeSet) -> str:
    """Render one line per changed path: kind, path, and pre -> post blob."""
    if not change_set.paths:
        return f"[dim]{escape(change_set.branch)} changed no files.[/dim]"
    lines: list[str] = []
    for entry in change_set.paths:
        pre = entry.pre_hash[:8] if entry.pre_hash else "-"
        post = entry.post_hash[:8] if entry.post_hash else "-"
        lines.append(f"[yellow]{entry.change_type:<10}[/yellow] {escape(entry.path)}  [dim]{pre} -> {post}[/dim]")
    return "\n".join(lines)


def _find_commits_to_revert(task_id: str | None, revert_all: bool) -> list[tuple[str, str]]:
    """Find commits matching the revert criteria."""
    try:
        res = subprocess.run(
            ["git", "log", "-n", "50", "--pretty=format:%H %s"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=".",
            timeout=30,
        )
        commits: list[tuple[str, str]] = []
        for line in res.stdout.splitlines():
            h, s = line.split(" ", 1)
            if (revert_all and "[bernstein]" in s) or (task_id and f"task:{task_id}" in s):
                commits.append((h, s))
        return commits
    except subprocess.TimeoutExpired as exc:
        raise click.ClickException("Timed out reading Git history for undo.") from exc
    except Exception as exc:
        console.print(f"[red]Error:[/red] Failed to read git log: {exc}")
        return []


def _execute_reverts(commits: list[tuple[str, str]]) -> tuple[int, str | None]:
    """Return the completed revert count and a commit whose outcome is uncertain."""
    success_count = 0
    for h, s in commits:
        console.print(f"Reverting [cyan]{h[:8]}[/cyan] {s}...")
        try:
            _run_revert(h)
            success_count += 1
        except subprocess.TimeoutExpired:
            return success_count, h
        except subprocess.CalledProcessError as e:
            console.print(f"[red]Failed to revert {h[:8]}:[/red] {e.stderr.decode()}")
            if not click.confirm("Continue with remaining reverts?", default=True):
                break
    return success_count, None


def _run_revert(commit: str) -> None:
    """Run a Git revert, keeping ordinary hook descendants in its process group."""
    argv = ["git", "revert", "--no-edit", commit]
    # On Windows a surviving child can keep a communicate reader blocked.
    with (
        tempfile.TemporaryFile() if IS_WINDOWS else nullcontext() as error_file,
        subprocess.Popen(
            argv,
            stdout=subprocess.PIPE if error_file is None else subprocess.DEVNULL,
            stderr=subprocess.PIPE if error_file is None else error_file,
            start_new_session=not IS_WINDOWS,
        ) as proc,
    ):
        try:
            _, stderr = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            if not kill_process_group(proc.pid, sig=9):
                proc.kill()
            raise
        if proc.returncode != 0:
            if error_file is not None:
                error_file.seek(0)
                stderr = error_file.read()
            raise subprocess.CalledProcessError(proc.returncode, argv, stderr=stderr)


def _log_undo_audit(
    task_id: str | None,
    revert_all: bool,
    success_count: int,
    *,
    uncertain_commit: str | None = None,
) -> None:
    """Log undo action to audit trail (best-effort).

    The import below must resolve for any correctly-installed bernstein
    package, so it is allowed to raise if it doesn't - that would signal a
    broken install, not a normal runtime condition. Only the write itself is
    best-effort: a misbehaving audit backend must not fail the undo.
    """
    from bernstein.core.tasks.lifecycle import get_audit_log

    audit = get_audit_log()
    if audit is None:
        return
    with suppress(Exception):
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
                **({"outcome": "unknown", "uncertain_commit": uncertain_commit} if uncertain_commit else {}),
            },
        )


def _run_post_revert_tests() -> None:
    """Run verification tests after revert."""
    console.print("\n[bold]Running verification tests...[/bold]")
    try:
        test_cmd = ["pytest", "-q"] if Path("tests").exists() else ["npm", "test"]
        subprocess.run(test_cmd, check=True)
        console.print("[green]✓[/green] Tests passed after rollback.")
    except Exception:
        console.print("[yellow]⚠ Tests failed after rollback. Manual inspection required.[/yellow]")
