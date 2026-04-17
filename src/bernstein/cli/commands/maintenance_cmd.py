"""Maintenance commands for runtime cleanup and archive inspection."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TypedDict

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.git_hygiene import run_hygiene
from bernstein.core.models import TaskStatus
from bernstein.core.task_store import TaskStore
from bernstein.core.worktree import WorktreeManager

_ACTIVE_TASK_STATUSES = frozenset(
    {
        TaskStatus.PLANNED,
        TaskStatus.OPEN,
        TaskStatus.CLAIMED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.BLOCKED,
        TaskStatus.PENDING_APPROVAL,
    }
)


class HistoryRow(TypedDict):
    """Rendered file-history entry."""

    task_id: str
    title: str
    role: str
    status: str
    completed_at: float
    assigned_agent: str | None


def _load_task_store(workdir: Path) -> TaskStore:
    """Load the persisted task store for *workdir*.

    Args:
        workdir: Repository root containing ``.sdd``.

    Returns:
        Replayed task store backed by the project's runtime files.
    """
    sdd_dir = workdir / ".sdd"
    store = TaskStore(
        jsonl_path=sdd_dir / "runtime" / "tasks.jsonl",
        archive_path=sdd_dir / "archive" / "tasks.jsonl",
    )
    store.replay_jsonl()
    return store


def _normalize_repo_path(workdir: Path, target: Path | str) -> str:
    """Normalize an archive path or CLI argument to repo-relative POSIX form.

    Args:
        workdir: Repository root.
        target: Absolute or relative path string.

    Returns:
        Repo-relative path when possible, otherwise a normalized POSIX string.
    """
    candidate = Path(target)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(workdir.resolve()).as_posix()
        except ValueError:
            return candidate.as_posix()
    return candidate.as_posix().lstrip("./")


def _history_rows(workdir: Path, file_path: Path, *, limit: int) -> list[HistoryRow]:
    """Build file-history rows from archived terminal tasks.

    Args:
        workdir: Repository root.
        file_path: File to inspect.
        limit: Maximum number of rows to return.

    Returns:
        Newest-first history rows for the requested file.
    """
    normalized_path = _normalize_repo_path(workdir, file_path)
    store = _load_task_store(workdir)
    matches: list[HistoryRow] = []
    for record in reversed(store.read_archive(limit=max(limit * 20, 1000))):
        record_path_matches = {_normalize_repo_path(workdir, path) for path in record.get("owned_files", [])}
        if normalized_path not in record_path_matches:
            continue
        matches.append(
            {
                "task_id": str(record.get("task_id", "")),
                "title": str(record.get("title", "")),
                "role": str(record.get("role", "")),
                "status": str(record.get("status", "")),
                "completed_at": float(record.get("completed_at", 0.0) or 0.0),
                "assigned_agent": (str(record["assigned_agent"]) if record.get("assigned_agent") else None),
            }
        )
        if len(matches) >= limit:
            break
    return matches


def _active_session_ids(store: TaskStore) -> set[str]:
    """Return session ids still attached to non-terminal tasks.

    Args:
        store: Replayed task store.

    Returns:
        Session ids that should retain their worktrees.
    """
    session_ids: set[str] = set()
    for task in store.list_tasks():
        if task.status in _ACTIVE_TASK_STATUSES and task.assigned_agent:
            session_ids.add(task.assigned_agent)
    return session_ids


def _cleanup_candidates(workdir: Path) -> list[str]:
    """Return inactive worktree session ids safe to clean up.

    Args:
        workdir: Repository root.

    Returns:
        Sorted list of session ids whose worktrees are no longer needed.
    """
    store = _load_task_store(workdir)
    live_sessions = _active_session_ids(store)
    manager = WorktreeManager(workdir)
    return sorted(session_id for session_id in manager.list_active() if session_id not in live_sessions)


def _format_relative_age(timestamp: float) -> str:
    """Render a short relative age string.

    Args:
        timestamp: Unix timestamp.

    Returns:
        Human-readable age such as ``"2m ago"``.
    """
    delta_s = max(0, int(time.time() - timestamp))
    if delta_s < 60:
        return f"{delta_s}s ago"
    if delta_s < 3600:
        return f"{delta_s // 60}m ago"
    if delta_s < 86_400:
        return f"{delta_s // 3600}h ago"
    return f"{delta_s // 86_400}d ago"


@click.command("cleanup")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    show_default=True,
    help="Project root containing .sdd/runtime and .sdd/worktrees.",
)
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Also delete agent branches that are NOT merged into main. Dangerous — "
        "may discard in-flight work. Only use after manually confirming the "
        "branches contain nothing you want to keep."
    ),
)
def cleanup_cmd(workdir: Path, yes: bool, force: bool) -> None:
    """Remove Bernstein worktrees that no longer back active tasks."""
    resolved_workdir = workdir.resolve()
    if not yes and not click.confirm("Remove inactive Bernstein worktrees and prune git state?", default=True):
        raise SystemExit(1)
    if force and not yes and not click.confirm("--force will DELETE unmerged agent branches. Continue?", default=False):
        raise SystemExit(1)

    store = _load_task_store(resolved_workdir)
    active_sessions = _active_session_ids(store)
    manager = WorktreeManager(resolved_workdir)
    candidates = sorted(session_id for session_id in manager.list_active() if session_id not in active_sessions)
    for session_id in candidates:
        manager.cleanup(session_id)

    hygiene = run_hygiene(
        resolved_workdir,
        full=True,
        active_session_ids=active_sessions,
        force_unmerged=force,
    )
    skipped = hygiene.get("branches_skipped", 0)
    console.print(
        "[green]Cleanup complete.[/green] "
        f"Removed {len(candidates)} inactive worktree(s); "
        f"pruned {hygiene['branches_deleted']} branch(es), "
        f"preserved {skipped} unmerged branch(es), "
        f"dropped {hygiene['stash_dropped']} stash(es)."
    )


@click.command("history")
@click.argument("file_path", type=click.Path(path_type=Path))
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    show_default=True,
    help="Project root containing .sdd/archive/tasks.jsonl.",
)
@click.option("--limit", type=int, default=10, show_default=True, help="Maximum number of matching tasks to show.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def history_cmd(file_path: Path, workdir: Path, limit: int, as_json: bool) -> None:
    """Show archived tasks that modified a specific file."""
    resolved_workdir = workdir.resolve()
    rows = _history_rows(resolved_workdir, file_path, limit=max(1, limit))

    if as_json:
        click.echo(json.dumps({"file": _normalize_repo_path(resolved_workdir, file_path), "tasks": rows}, indent=2))
        return

    if not rows:
        console.print(f"[dim]No archived tasks recorded for {_normalize_repo_path(resolved_workdir, file_path)}.[/dim]")
        return

    table = Table(title=f"Task History: {_normalize_repo_path(resolved_workdir, file_path)}", header_style="bold cyan")
    table.add_column("When", style="dim", min_width=10)
    table.add_column("Status")
    table.add_column("Agent")
    table.add_column("Role")
    table.add_column("Task")

    for row in rows:
        status = row["status"]
        match status:
            case "done":
                status_color = "green"
            case "failed":
                status_color = "red"
            case _:
                status_color = "yellow"
        table.add_row(
            _format_relative_age(row["completed_at"]),
            f"[{status_color}]{status}[/{status_color}]",
            row["assigned_agent"] or "—",
            row["role"],
            f"{row['task_id'][:8]}  {row['title']}",
        )

    console.print(table)
