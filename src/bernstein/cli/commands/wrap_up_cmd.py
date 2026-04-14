"""Wrap-up command — end the current session with a structured brief."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console, server_get
from bernstein.core.session import WrapUpBrief, save_wrapup


def _get_git_diff_stat(start_sha: str) -> str:
    """Return ``git diff --stat <start_sha>..HEAD``, or fallback output."""
    if start_sha:
        try:
            result = subprocess.run(
                ["git", "diff", "--stat", f"{start_sha}..HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except FileNotFoundError:
            pass  # git not available
    # Fallback: uncommitted changes vs last commit
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "(no uncommitted changes)"
    except FileNotFoundError:
        pass  # git not available
    return ""


def _find_session_start_sha(saved_at: float) -> str:
    """Find the commit SHA that was HEAD at approximately session start time.

    Looks for commits newer than *saved_at* and returns the parent of the
    oldest one (i.e. HEAD before this session began).  Falls back to empty
    string so callers can handle gracefully.
    """
    try:
        import datetime

        iso = datetime.datetime.fromtimestamp(saved_at, tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S")
        result = subprocess.run(
            ["git", "log", "--format=%H", "--reverse", f"--after={iso}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0:
            return ""
        commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not commits:
            return ""
        first_commit = commits[0]
        # Get the parent of the first commit in this session
        parent_result = subprocess.run(
            ["git", "rev-parse", f"{first_commit}^"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if parent_result.returncode == 0:
            return parent_result.stdout.strip()
    except (FileNotFoundError, ValueError):
        pass  # git not available or invalid ref
    return ""


def _load_session_saved_at() -> float:
    """Read saved_at from .sdd/runtime/session.json, or return 0.0."""
    session_path = Path(".sdd/runtime/session.json")
    if not session_path.exists():
        return 0.0
    try:
        data = json.loads(session_path.read_text())
        return float(data.get("saved_at", 0.0))
    except (ValueError, OSError):
        return 0.0


def _fetch_tasks_by_status(status: str) -> list[dict[str, Any]]:
    """Return task dicts from the server filtered by *status*."""
    data = server_get(f"/tasks?status={status}")
    if not data or not isinstance(data, list):
        return []
    return [t for t in data if isinstance(t, dict)]


def _build_changes_summary(done_tasks: list[dict[str, Any]]) -> str:
    """Summarise completed tasks into a human-readable string."""
    if not done_tasks:
        return "No tasks completed this session."
    lines: list[str] = []
    for task in done_tasks:
        title = task.get("title", task.get("id", "unknown"))
        summary = task.get("result_summary") or ""
        if summary:
            lines.append(f"- {title}: {summary}")
        else:
            lines.append(f"- {title}")
    return "\n".join(lines)


def _extract_learnings(failed_tasks: list[dict[str, Any]]) -> list[str]:
    """Extract learnings from failed tasks."""
    learnings: list[str] = []
    for task in failed_tasks:
        title = task.get("title", task.get("id", "?"))
        summary = task.get("result_summary") or ""
        if summary:
            learnings.append(f"Task '{title}' failed: {summary}")
        else:
            learnings.append(f"Task '{title}' failed without a recorded reason.")
    return learnings


def _build_next_session_brief(open_tasks: list[dict[str, Any]]) -> str:
    """Generate a next-session brief from remaining open tasks."""
    if not open_tasks:
        return "No open tasks remaining. Consider running `bernstein evolve` to generate new work."
    priority_tasks = sorted(open_tasks, key=lambda t: t.get("priority", 2))
    lines = ["Remaining open tasks (by priority):"]
    for task in priority_tasks[:10]:
        title = task.get("title", task.get("id", "?"))
        role = task.get("role", "")
        prio = task.get("priority", 2)
        lines.append(f"  [{prio}] {title}" + (f" ({role})" if role else ""))
    if len(priority_tasks) > 10:
        lines.append(f"  … and {len(priority_tasks) - 10} more")
    return "\n".join(lines)


@click.command("wrap-up")
@click.option(
    "--stop",
    "do_stop",
    is_flag=True,
    default=False,
    help="Also perform a soft stop after saving the wrap-up brief.",
)
@click.option(
    "--timeout",
    default=30,
    show_default=True,
    help="Soft-stop timeout in seconds (only used with --stop).",
)
def _render_wrapup_brief(
    workdir: Path,
    saved_path: Path,
    start_sha: str,
    done_tasks: list[dict[str, Any]],
    learnings: list[str],
    git_diff_stat: str,
    next_session_brief: str,
) -> None:
    """Print the Rich-formatted wrap-up brief."""
    console.print()
    console.print(Panel("[bold]Session Wrap-Up[/bold]", border_style="green", expand=False))

    meta_table = Table(show_header=False, box=None, padding=(0, 2))
    meta_table.add_column("Key", style="dim", no_wrap=True, min_width=16)
    meta_table.add_column("Value")
    meta_table.add_row("Saved to", str(saved_path.relative_to(workdir)))
    if start_sha:
        meta_table.add_row("Session start SHA", start_sha[:12])
    console.print(meta_table)
    console.print()

    console.print(f"  [bold green]Completed[/bold green]   {len(done_tasks)} task(s)")
    for task in done_tasks:
        title = task.get("title", task.get("id", "?"))
        summary = task.get("result_summary") or ""
        short = summary[:60] + "…" if len(summary) > 60 else summary
        suffix = f"  [dim]{short}[/dim]" if short else ""
        console.print(f"    [green]✓[/green] {title}{suffix}")

    if learnings:
        console.print(f"\n  [bold red]Learnings[/bold red]   {len(learnings)} item(s)")
        for item in learnings:
            console.print(f"    [red]![/red] {item}")

    if git_diff_stat and git_diff_stat != "(no uncommitted changes)":
        console.print("\n  [bold cyan]Changes[/bold cyan]")
        stat_lines = git_diff_stat.splitlines()
        for line in stat_lines[:20]:
            console.print(f"    [dim]{line}[/dim]")
        if len(stat_lines) > 20:
            console.print(f"    [dim]… {len(stat_lines) - 20} more lines[/dim]")

    console.print("\n  [bold yellow]Next session[/bold yellow]")
    for line in next_session_brief.splitlines():
        console.print(f"    {line}")

    console.print()


def wrap_up(do_stop: bool, timeout: int) -> None:
    """End the current session with a structured wrap-up brief.

    \b
      bernstein wrap-up           # save brief, keep running
      bernstein wrap-up --stop    # save brief, then soft-stop
    """
    workdir = Path.cwd()

    # 1. Check server reachability
    if server_get("/status") is None:
        console.print("[red]Cannot reach task server.[/red] Is Bernstein running? Run [bold]bernstein[/bold] to start.")
        raise SystemExit(1)

    # 2. Gather tasks
    done_tasks = _fetch_tasks_by_status("done")
    failed_tasks = _fetch_tasks_by_status("failed")
    open_tasks = _fetch_tasks_by_status("open")

    # 3. Determine session start SHA via session.json
    saved_at = _load_session_saved_at()
    start_sha = _find_session_start_sha(saved_at) if saved_at else ""

    # 4. Git diff stat
    git_diff_stat = _get_git_diff_stat(start_sha)

    # 5. Build brief fields
    changes_summary = _build_changes_summary(done_tasks)
    learnings = _extract_learnings(failed_tasks)
    next_session_brief = _build_next_session_brief(open_tasks)

    # 6. Build WrapUpBrief
    brief = WrapUpBrief(
        timestamp=time.time(),
        session_id=str(int(saved_at)) if saved_at else str(int(time.time())),
        changes_summary=changes_summary,
        learnings=learnings,
        next_session_brief=next_session_brief,
        git_diff_stat=git_diff_stat,
    )

    # 7. Save to .sdd/sessions/<timestamp>-wrapup.json
    saved_path = save_wrapup(workdir, brief)

    # 8. Print Rich-formatted wrap-up
    _render_wrapup_brief(
        workdir, saved_path, start_sha, done_tasks, learnings,
        git_diff_stat, next_session_brief,
    )

    # 9. Optionally soft-stop
    if do_stop:
        from bernstein.cli.stop_cmd import soft_stop

        console.print("[bold]Running soft stop…[/bold]\n")
        soft_stop(timeout)
