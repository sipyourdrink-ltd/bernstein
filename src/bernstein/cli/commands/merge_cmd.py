"""merge command — pick the best agent solution and merge it into main.

Used after comparing parallel branches (A/B testing different models on the
same task) to select a winner: ``bernstein merge --pick agent1``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from bernstein.cli.diff_cmd import (
    _branch_exists,
    _find_agent_by_session,
    _find_agent_for_task,
    _load_agents,
    _run_git,
    resolve_diff,
)
from bernstein.cli.helpers import console

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_agent_branch(
    identifier: str, root: Path, agents: list[dict[str, Any]]
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve an identifier (task ID or session ID) to a branch name.

    Returns:
        (branch_name, agent_dict) or (None, None) if not found.
    """
    agent = _find_agent_for_task(identifier, agents) or _find_agent_by_session(identifier, agents)
    if agent is None:
        return None, None

    session_id = agent.get("id", "")
    branch = f"agent/{session_id}"

    # Check if the branch or worktree exists
    worktree_path = root / ".sdd" / "worktrees" / session_id
    if worktree_path.exists() and (worktree_path / ".git").exists():
        return branch, agent
    if _branch_exists(branch, root):
        return branch, agent

    return None, agent


def _current_branch(root: Path) -> str:
    """Get the current branch name."""
    return _run_git(["rev-parse", "--abbrev-ref", "HEAD"], root)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("merge")
@click.option(
    "--pick",
    type=str,
    required=True,
    metavar="AGENT",
    help="Task ID or session ID of the agent whose solution to merge.",
)
@click.option("--base", default="main", show_default=True, help="Target branch to merge into.")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(),
    help="Project root (parent of .sdd/).",
)
@click.option("--no-ff", "no_ff", is_flag=True, default=True, show_default=True, help="Use --no-ff merge.")
@click.option("--message", "-m", default=None, help="Custom merge commit message.")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be merged without merging.")
@click.option(
    "--reject",
    "reject_others",
    multiple=True,
    metavar="AGENT",
    help="Also delete branches of rejected agents (repeatable).",
)
def _verify_merge_result(root: Path, merge_msg: str, switched: bool, original_branch: str) -> None:
    """Verify merge succeeded or handle conflicts."""
    _run_git(["rev-parse", "HEAD"], root)
    merge_log = _run_git(["log", "-1", "--oneline"], root)

    is_merge_success = merge_msg[:20] in merge_log or "Merge" in merge_log
    if is_merge_success:
        console.print(f"[green]Merged successfully:[/green] {merge_log}")
        return

    status = _run_git(["status", "--porcelain"], root)
    has_conflicts = any(line[:2] in ("UU", "AA", "DD") for line in status.splitlines() if len(line) >= 2)
    if has_conflicts:
        console.print("[red]Merge conflicts detected![/red]")
        console.print("[dim]Resolve conflicts manually, then commit.[/dim]")
        _run_git(["merge", "--abort"], root)
        if switched:
            _run_git(["checkout", original_branch], root)
        raise SystemExit(1)

    console.print(f"[green]Merge completed:[/green] {merge_log}")


def merge_cmd(
    pick: str,
    base: str,
    workdir: str,
    no_ff: bool,
    message: str | None,
    dry_run: bool,
    reject_others: tuple[str, ...],
) -> None:
    """Pick the best agent solution and merge it.

    After comparing parallel branches with ``bernstein diff --compare``,
    use this command to merge the winning solution into the target branch.

    \b
    Examples:
      bernstein merge --pick backend-abc123           # merge agent's work
      bernstein merge --pick task-id-prefix           # resolve by task ID
      bernstein merge --pick agent1 --reject agent2   # merge one, delete other
      bernstein merge --pick agent1 --dry-run         # preview only
    """
    root = Path(workdir).resolve()
    agents = _load_agents(root)

    # ------------------------------------------------------------------
    # 1. Resolve the picked agent to a branch
    # ------------------------------------------------------------------
    branch, agent = _resolve_agent_branch(pick, root, agents)

    if branch is None:
        if agent:
            console.print(
                f"[yellow]Agent found for [bold]{pick}[/bold] but no branch/worktree exists.[/yellow]\n"
                "[dim]The branch may have been cleaned up or already merged.[/dim]"
            )
        else:
            console.print(f"[red]No agent or branch found for:[/red] [bold]{pick}[/bold]")
        raise SystemExit(1)

    session_id = agent.get("id", "") if agent else pick
    role = agent.get("role", "") if agent else ""
    model = agent.get("model", "") if agent else ""

    console.print(f"[bold]Picking:[/bold] [cyan]{branch}[/cyan]  [dim]role={role}, model={model}[/dim]")

    # ------------------------------------------------------------------
    # 2. Show what will be merged
    # ------------------------------------------------------------------
    resolved = resolve_diff(pick, root, agents, base)
    if resolved.stat_text:
        console.print("\n[bold]Changes to merge:[/bold]")
        console.print(resolved.stat_text)
        console.print()

    if dry_run:
        console.print("[yellow]--dry-run: no changes made.[/yellow]")

        # Show rejected branches that would be deleted
        if reject_others:
            console.print("\n[bold]Would reject (delete branches):[/bold]")
            for rej_id in reject_others:
                rej_branch, _rej_agent = _resolve_agent_branch(rej_id, root, agents)
                status = f"[cyan]{rej_branch}[/cyan]" if rej_branch else "[dim]not found[/dim]"
                console.print(f"  {rej_id} -> {status}")
        return

    # ------------------------------------------------------------------
    # 3. Perform the merge
    # ------------------------------------------------------------------
    current = _current_branch(root)
    switched = False

    if current != base:
        console.print(f"[dim]Switching to {base}...[/dim]")
        result = _run_git(["checkout", base], root)
        if not result and "error" in _run_git(["checkout", base], root).lower():
            console.print(f"[red]Failed to checkout {base}.[/red]")
            raise SystemExit(1)
        switched = True

    merge_msg = message or f"Merge {branch}: pick best solution (session {session_id})"
    merge_args = ["merge"]
    if no_ff:
        merge_args.append("--no-ff")
    merge_args.extend(["-m", merge_msg, branch])

    _run_git(merge_args, root)
    _verify_merge_result(root, merge_msg, switched, current)

    # ------------------------------------------------------------------
    # 4. Clean up rejected branches
    # ------------------------------------------------------------------
    if reject_others:
        console.print()
        for rej_id in reject_others:
            rej_branch, _rej_agent = _resolve_agent_branch(rej_id, root, agents)
            if rej_branch:
                del_result = _run_git(["branch", "-D", rej_branch], root)
                if del_result:
                    console.print(f"[dim]Deleted rejected branch:[/dim] [red]{rej_branch}[/red]")
                else:
                    console.print(f"[yellow]Could not delete:[/yellow] {rej_branch}")
            else:
                console.print(f"[dim]No branch found for rejected agent:[/dim] {rej_id}")
