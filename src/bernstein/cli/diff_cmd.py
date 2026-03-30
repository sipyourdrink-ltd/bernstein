"""diff command — show the git diff of what an agent changed for a task."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import console

# ---------------------------------------------------------------------------
# Diff resolution helpers
# ---------------------------------------------------------------------------


def _load_agents(workdir: Path) -> list[dict[str, Any]]:
    """Load agent sessions from .sdd/runtime/agents.json."""
    agents_file = workdir / ".sdd" / "runtime" / "agents.json"
    if not agents_file.exists():
        return []
    try:
        data = json.loads(agents_file.read_text())
        return data.get("agents", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


def _find_agent_for_task(task_id: str, agents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the agent session whose task_ids list contains task_id (prefix match)."""
    for agent in agents:
        for tid in agent.get("task_ids", []):
            if tid == task_id or tid.startswith(task_id) or task_id.startswith(tid[:8]):
                return agent
    return None


def _run_git(args: list[str], cwd: Path) -> str:
    """Run a git command and return stdout, or empty string on error."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _branch_exists(branch: str, workdir: Path) -> bool:
    """Return True if a local or remote branch exists."""
    out = _run_git(["branch", "--list", branch], workdir)
    return bool(out.strip())


def _get_diff_from_worktree(worktree: Path, base: str = "main") -> str:
    """Get uncommitted + committed diff from a live worktree vs base branch."""
    # Committed changes since branch point
    diff = _run_git(["diff", f"{base}...HEAD", "--"], worktree)
    if diff:
        return diff
    # Fallback: staged only
    diff = _run_git(["diff", "--cached", "--"], worktree)
    return diff


def _get_diff_from_branch(branch: str, workdir: Path, base: str = "main") -> str:
    """Diff a local branch vs base, run from the main workdir."""
    return _run_git(["diff", f"{base}...{branch}", "--"], workdir)


def _find_merge_commit(session_id: str, workdir: Path) -> str | None:
    """Find the merge commit that merged agent/{session_id} into main."""
    log = _run_git(
        ["log", "main", "--merges", "--oneline", f"--grep=agent/{session_id}"],
        workdir,
    )
    if log:
        return log.split()[0]
    # Also try matching just the session_id suffix
    log = _run_git(
        ["log", "main", "--merges", "--oneline", f"--grep={session_id}"],
        workdir,
    )
    if log:
        return log.split()[0]
    return None


def _get_diff_from_merge_commit(commit: str, workdir: Path) -> str:
    """Show the diff introduced by a merge commit."""
    return _run_git(["diff", f"{commit}^..{commit}", "--"], workdir)


def _search_commits_by_task_id(task_id: str, workdir: Path) -> str:
    """Last-resort: find any commit whose message references the task_id."""
    log = _run_git(
        ["log", "--all", "--oneline", f"--grep={task_id}"],
        workdir,
    )
    if not log:
        return ""
    first_hash = log.splitlines()[0].split()[0]
    return _run_git(["show", first_hash, "--", "--format="], workdir)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("diff")
@click.argument("task_id")
@click.option("--base", default="main", show_default=True, help="Base branch to diff against.")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(),
    help="Project root (parent of .sdd/).",
)
@click.option("--stat", "stat_only", is_flag=True, default=False, help="Show diff --stat summary only.")
@click.option("--raw", is_flag=True, default=False, help="Print raw diff without syntax highlighting.")
def diff_cmd(task_id: str, base: str, workdir: str, stat_only: bool, raw: bool) -> None:
    """Show the git diff of what an agent changed for a task.

    Looks up the agent session that handled TASK_ID, then retrieves the diff
    from the live worktree (if still active) or from the merged branch/commit.

    \b
    Examples:
      bernstein diff 90307ac2             # partial task ID OK
      bernstein diff 90307ac2 --stat      # summary only
      bernstein diff 90307ac2 --base main # diff vs a specific base branch
    """
    root = Path(workdir).resolve()
    agents = _load_agents(root)

    # ------------------------------------------------------------------
    # 1. Find the agent session for this task
    # ------------------------------------------------------------------
    agent = _find_agent_for_task(task_id, agents)
    session_id = None if agent is None else agent.get("id")

    diff_text = ""
    source_label = ""

    if session_id:
        branch = f"agent/{session_id}"
        worktree_path = root / ".sdd" / "worktrees" / session_id

        # ------------------------------------------------------------------
        # 2a. Live worktree (task still in progress)
        # ------------------------------------------------------------------
        if worktree_path.exists() and (worktree_path / ".git").exists():
            diff_text = _get_diff_from_worktree(worktree_path, base)
            if diff_text:
                source_label = f"[dim]source:[/dim] worktree [cyan]{session_id}[/cyan] vs [yellow]{base}[/yellow]"

        # ------------------------------------------------------------------
        # 2b. Local branch still exists (not yet merged)
        # ------------------------------------------------------------------
        if not diff_text and _branch_exists(branch, root):
            diff_text = _get_diff_from_branch(branch, root, base)
            if diff_text:
                source_label = f"[dim]source:[/dim] branch [cyan]{branch}[/cyan] vs [yellow]{base}[/yellow]"

        # ------------------------------------------------------------------
        # 2c. Merged branch — find the merge commit
        # ------------------------------------------------------------------
        if not diff_text:
            merge_commit = _find_merge_commit(session_id, root)
            if merge_commit:
                diff_text = _get_diff_from_merge_commit(merge_commit, root)
                if diff_text:
                    source_label = (
                        f"[dim]source:[/dim] merge commit [cyan]{merge_commit}[/cyan] ([dim]agent/{session_id}[/dim])"
                    )

    # ------------------------------------------------------------------
    # 3. Last resort: search all commits for the task_id
    # ------------------------------------------------------------------
    if not diff_text:
        diff_text = _search_commits_by_task_id(task_id, root)
        if diff_text:
            source_label = f"[dim]source:[/dim] commit search for [cyan]{task_id}[/cyan]"

    # ------------------------------------------------------------------
    # 4. Render
    # ------------------------------------------------------------------
    if not diff_text:
        agent_hint = f" (agent: [cyan]{session_id}[/cyan])" if session_id else ""
        console.print(f"[yellow]No diff found for task:[/yellow] [bold]{task_id}[/bold]{agent_hint}")
        console.print("[dim]The task may not have made any changes yet, or the branch/worktree was cleaned up.[/dim]")
        raise SystemExit(1)

    # Header
    if agent:
        task_ids_str = ", ".join(agent.get("task_ids", []))
        role = agent.get("role", "")
        model = agent.get("model", "")
        console.print(f"[bold]Task:[/bold] [cyan]{task_ids_str}[/cyan]  [dim]role={role}, model={model}[/dim]")
    if source_label:
        console.print(source_label)
    console.print()

    if stat_only:
        # Show only the stat summary
        stat_lines = [
            line for line in diff_text.splitlines() if "|" in line or "changed" in line or "insertion" in line
        ]
        if stat_lines:
            console.print("\n".join(stat_lines))
        else:
            # Re-run with --stat if we have enough info
            if session_id:
                worktree_path = root / ".sdd" / "worktrees" / session_id
                if worktree_path.exists():
                    stat = _run_git(["diff", f"{base}...HEAD", "--stat"], worktree_path)
                else:
                    branch = f"agent/{session_id}"
                    stat = _run_git(["diff", f"{base}...{branch}", "--stat"], root)
                console.print(stat or "[dim](no stat available)[/dim]")
            else:
                console.print("[dim](stat not available for this source)[/dim]")
        return

    if raw:
        console.print(diff_text)
        return

    # Syntax-highlighted diff
    from rich.syntax import Syntax

    syntax = Syntax(diff_text, "diff", theme="monokai", line_numbers=False)
    console.print(syntax)
