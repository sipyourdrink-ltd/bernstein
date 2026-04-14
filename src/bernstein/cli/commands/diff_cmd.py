"""diff command — show the git diff of what an agent changed for a task.

Includes side-by-side comparison mode for comparing two agents' work on
the same (or related) tasks: ``bernstein diff --compare agent1 agent2``.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import console

_NO_CHANGES = "(no changes)"

# ---------------------------------------------------------------------------
# Diff resolution helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileDiffStat:
    """Additions and deletions for a single file."""

    path: str
    additions: int
    deletions: int
    is_binary: bool = False


@dataclass(frozen=True)
class ResolvedDiff:
    """Result of resolving a diff for an agent/task.

    Attributes:
        diff_text: The raw unified diff string.
        source_label: Human-readable Rich markup describing where the diff came from.
        agent: The agent session dict (if found).
        session_id: The agent session ID (if found).
        stat_text: The ``--stat`` summary (if available).
        file_stats: List of per-file addition/deletion stats.
    """

    diff_text: str
    source_label: str
    agent: dict[str, Any] | None = None
    session_id: str | None = None
    stat_text: str = ""
    file_stats: list[FileDiffStat] = field(default_factory=list)


def _get_numstat(args: list[str], cwd: Path) -> list[FileDiffStat]:
    """Run git diff --numstat and parse results."""
    out = _run_git([*args, "--numstat"], cwd)
    stats: list[FileDiffStat] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            try:
                add = int(parts[0]) if parts[0] != "-" else 0
                dele = int(parts[1]) if parts[1] != "-" else 0
                stats.append(
                    FileDiffStat(
                        path=parts[2],
                        additions=add,
                        deletions=dele,
                        is_binary=parts[0] == "-" or parts[1] == "-",
                    )
                )
            except ValueError:
                continue
    return stats


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


def _find_agent_by_session(session_id: str, agents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find an agent session by its session ID (exact or prefix match)."""
    for agent in agents:
        aid = agent.get("id", "")
        if aid == session_id or aid.startswith(session_id) or session_id.startswith(aid[:8]):
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
            encoding="utf-8",
            errors="replace",
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


def _get_stat_from_worktree(worktree: Path, base: str = "main") -> str:
    """Get --stat from a live worktree."""
    return _run_git(["diff", f"{base}...HEAD", "--stat"], worktree)


def _get_stat_from_branch(branch: str, workdir: Path, base: str = "main") -> str:
    """Get --stat from a branch."""
    return _run_git(["diff", f"{base}...{branch}", "--stat"], workdir)


# ---------------------------------------------------------------------------
# Unified diff resolver
# ---------------------------------------------------------------------------


def _try_worktree_diff(
    session_id: str, root: Path, base: str,
) -> tuple[str, str, str, list[FileDiffStat]]:
    """Try to get diff from a live worktree. Returns (diff, label, stat, file_stats)."""
    worktree_path = root / ".sdd" / "worktrees" / session_id
    has_worktree = worktree_path.exists() and (worktree_path / ".git").exists()
    if not has_worktree:
        return "", "", "", []
    diff_text = _get_diff_from_worktree(worktree_path, base)
    if not diff_text:
        return "", "", "", []
    label = f"[dim]source:[/dim] worktree [cyan]{session_id}[/cyan] vs [yellow]{base}[/yellow]"
    stat = _get_stat_from_worktree(worktree_path, base)
    stats = _get_numstat(["diff", f"{base}...HEAD"], worktree_path)
    return diff_text, label, stat, stats


def _try_branch_diff(
    session_id: str, root: Path, base: str,
) -> tuple[str, str, str, list[FileDiffStat]]:
    """Try to get diff from a local branch. Returns (diff, label, stat, file_stats)."""
    branch = f"agent/{session_id}"
    if not _branch_exists(branch, root):
        return "", "", "", []
    diff_text = _get_diff_from_branch(branch, root, base)
    if not diff_text:
        return "", "", "", []
    label = f"[dim]source:[/dim] branch [cyan]{branch}[/cyan] vs [yellow]{base}[/yellow]"
    stat = _get_stat_from_branch(branch, root, base)
    stats = _get_numstat(["diff", f"{base}...{branch}"], root)
    return diff_text, label, stat, stats


def _try_merge_commit_diff(
    session_id: str, root: Path,
) -> tuple[str, str, str, list[FileDiffStat]]:
    """Try to get diff from a merge commit. Returns (diff, label, stat, file_stats)."""
    merge_commit = _find_merge_commit(session_id, root)
    if not merge_commit:
        return "", "", "", []
    diff_text = _get_diff_from_merge_commit(merge_commit, root)
    if not diff_text:
        return "", "", "", []
    label = f"[dim]source:[/dim] merge commit [cyan]{merge_commit}[/cyan] ([dim]agent/{session_id}[/dim])"
    stats = _get_numstat(["diff", f"{merge_commit}^..{merge_commit}"], root)
    return diff_text, label, "", stats


def resolve_diff(identifier: str, root: Path, agents: list[dict[str, Any]], base: str = "main") -> ResolvedDiff:
    """Resolve a diff for a task ID or agent session ID.

    Tries multiple strategies: live worktree, local branch, merge commit,
    commit search. The *identifier* can be a task ID or session ID.

    Args:
        identifier: Task ID or agent session ID (prefix match OK).
        root: Project root directory.
        agents: List of agent session dicts from agents.json.
        base: Base branch to diff against.

    Returns:
        ResolvedDiff with the diff text and metadata.
    """
    agent = _find_agent_for_task(identifier, agents) or _find_agent_by_session(identifier, agents)
    session_id = None if agent is None else agent.get("id")

    diff_text = ""
    source_label = ""
    stat_text = ""
    file_stats: list[FileDiffStat] = []

    if session_id:
        for try_fn in [
            lambda: _try_worktree_diff(session_id, root, base),
            lambda: _try_branch_diff(session_id, root, base),
            lambda: _try_merge_commit_diff(session_id, root),
        ]:
            diff_text, source_label, stat_text, file_stats = try_fn()
            if diff_text:
                break

    # Last resort: search commits
    if not diff_text:
        diff_text = _search_commits_by_task_id(identifier, root)
        if diff_text:
            source_label = f"[dim]source:[/dim] commit search for [cyan]{identifier}[/cyan]"

    return ResolvedDiff(
        diff_text=diff_text,
        source_label=source_label,
        agent=agent,
        session_id=session_id,
        stat_text=stat_text,
        file_stats=file_stats,
    )


# ---------------------------------------------------------------------------
# Side-by-side comparison renderer
# ---------------------------------------------------------------------------


def _parse_diff_files(diff_text: str) -> dict[str, list[str]]:
    """Parse a unified diff into a dict of {filepath: [lines]}."""
    files: dict[str, list[str]] = {}
    current_file: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            # Extract b/path
            parts = line.split(" b/", 1)
            current_file = parts[1] if len(parts) > 1 else line
            files[current_file] = []
        elif current_file is not None:
            files[current_file].append(line)
    return files


def _render_compare(left: ResolvedDiff, right: ResolvedDiff, left_name: str, right_name: str) -> None:
    """Render side-by-side comparison of two diffs using Rich columns."""
    from rich.columns import Columns
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text

    # -- Header with agent info --
    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(ratio=1)

    def _agent_label(rd: ResolvedDiff, name: str) -> Text:
        t = Text()
        t.append(name, style="bold cyan")
        if rd.agent:
            role = rd.agent.get("role", "")
            model = rd.agent.get("model", "")
            if role or model:
                t.append(f"  role={role} model={model}", style="dim")
        return t

    header.add_row(_agent_label(left, left_name), _agent_label(right, right_name))
    console.print(header)
    console.print()

    # -- Stat summary comparison --
    if left.stat_text or right.stat_text:
        stat_table = Table.grid(expand=True)
        stat_table.add_column(ratio=1)
        stat_table.add_column(ratio=1)
        stat_table.add_row(
            Text(left.stat_text or _NO_CHANGES, style="dim"),
            Text(right.stat_text or _NO_CHANGES, style="dim"),
        )
        console.print(stat_table)
        console.print()

    # -- File-level comparison --
    left_files = _parse_diff_files(left.diff_text) if left.diff_text else {}
    right_files = _parse_diff_files(right.diff_text) if right.diff_text else {}
    all_files = sorted(set(left_files) | set(right_files))

    if not all_files:
        console.print("[yellow]Neither agent produced any changes.[/yellow]")
        return

    # Summary table: which files each agent touched
    summary = Table(title="Files Changed", expand=True, show_lines=True)
    summary.add_column("File", style="white")
    summary.add_column(left_name, justify="center", width=12)
    summary.add_column(right_name, justify="center", width=12)
    summary.add_column("Status", width=14)

    for f in all_files:
        in_left = f in left_files
        in_right = f in right_files
        left_mark = "[green]+[/green]" if in_left else "[dim]-[/dim]"
        right_mark = "[green]+[/green]" if in_right else "[dim]-[/dim]"
        if in_left and in_right:
            status = "[yellow]both[/yellow]"
        elif in_left:
            status = f"[cyan]{left_name} only[/cyan]"
        else:
            status = f"[magenta]{right_name} only[/magenta]"
        summary.add_row(f, left_mark, right_mark, status)

    console.print(summary)
    console.print()

    # Per-file side-by-side diffs
    for f in all_files:
        left_content = "\n".join(left_files.get(f, [_NO_CHANGES]))
        right_content = "\n".join(right_files.get(f, [_NO_CHANGES]))

        left_panel = Panel(
            Syntax(left_content, "diff", theme="monokai", line_numbers=False),
            title=f"[cyan]{left_name}[/cyan]",
            border_style="cyan",
            expand=True,
        )
        right_panel = Panel(
            Syntax(right_content, "diff", theme="monokai", line_numbers=False),
            title=f"[magenta]{right_name}[/magenta]",
            border_style="magenta",
            expand=True,
        )

        console.print(f"[bold]--- {f} ---[/bold]")
        console.print(Columns([left_panel, right_panel], expand=True, equal=True))
        console.print()


def _format_change_text(stat: FileDiffStat) -> Any:
    """Format addition/deletion counts as a Rich Text object."""
    from rich.text import Text

    changes = Text()
    if stat.is_binary:
        changes.append("binary", style="dim")
        return changes
    if stat.additions > 0:
        changes.append(f"+{stat.additions}", style="green")
    if stat.deletions > 0:
        if stat.additions > 0:
            changes.append(" ")
        changes.append(f"-{stat.deletions}", style="red")
    return changes


_SENSITIVE_KEYWORDS = ("secret", "auth", "encrypt", "key", "config")


def _assess_file_risk(stat: FileDiffStat) -> Any:
    """Assess risk level for a file diff."""
    from rich.text import Text

    path_lower = stat.path.lower()
    total_changes = stat.additions + stat.deletions

    if any(k in path_lower for k in _SENSITIVE_KEYWORDS):
        return Text("HIGH", style="red")
    if total_changes > 500:
        return Text("LARGE", style="magenta")
    if "test" in path_lower and stat.deletions > stat.additions:
        return Text("MODERATE", style="yellow")
    return Text("low", style="dim")


def _render_enhanced_summary(resolved: ResolvedDiff) -> None:
    """Render a file-level summary with risk indicators."""
    from rich.panel import Panel
    from rich.table import Table

    if not resolved.file_stats:
        return

    table = Table(title="File Summary", expand=True, show_lines=False)
    table.add_column("File", style="cyan")
    table.add_column("Changes", justify="right", width=12)
    table.add_column("Risk", justify="center", width=10)

    total_adds = 0
    total_dels = 0

    for stat in resolved.file_stats:
        total_adds += stat.additions
        total_dels += stat.deletions

        changes = _format_change_text(stat)
        risk = _assess_file_risk(stat)
        table.add_row(stat.path, changes, risk)

    summary_text = (
        f"Total: [green]+{total_adds}[/green] [red]-{total_dels}[/red] across {len(resolved.file_stats)} files"
    )
    console.print(Panel(table, subtitle=summary_text))
    console.print()


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


def _render_stat_only(resolved: ResolvedDiff, root: Path, base: str) -> None:
    """Render stat-only output for a resolved diff."""
    if resolved.stat_text:
        console.print(resolved.stat_text)
        return
    if not resolved.session_id:
        console.print("[dim](stat not available for this source)[/dim]")
        return
    worktree_path = root / ".sdd" / "worktrees" / resolved.session_id
    if worktree_path.exists():
        stat = _run_git(["diff", f"{base}...HEAD", "--stat"], worktree_path)
    else:
        branch = f"agent/{resolved.session_id}"
        stat = _run_git(["diff", f"{base}...{branch}", "--stat"], root)
    console.print(stat or "[dim](no stat available)[/dim]")


@click.command("diff")
@click.argument("task_id", required=False, default=None)
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
@click.option(
    "--compare",
    nargs=2,
    type=str,
    default=None,
    metavar="AGENT1 AGENT2",
    help="Compare two agents side-by-side (task IDs or session IDs).",
)
def diff_cmd(
    task_id: str | None,
    base: str,
    workdir: str,
    stat_only: bool,
    raw: bool,
    compare: tuple[str, str] | None,
) -> None:
    """Show the git diff of what an agent changed for a task.

    Looks up the agent session that handled TASK_ID, then retrieves the diff
    from the live worktree (if still active) or from the merged branch/commit.

    \b
    Examples:
      bernstein diff 90307ac2                         # single task diff
      bernstein diff 90307ac2 --stat                  # summary only
      bernstein diff --compare backend-abc qa-def     # side-by-side
      bernstein diff --compare task1 task2 --stat     # stat comparison
    """
    root = Path(workdir).resolve()
    agents = _load_agents(root)

    # ------------------------------------------------------------------
    # Compare mode: side-by-side diff of two agents
    # ------------------------------------------------------------------
    if compare is not None:
        left_id, right_id = compare
        left = resolve_diff(left_id, root, agents, base)
        right = resolve_diff(right_id, root, agents, base)

        if not left.diff_text and not right.diff_text:
            console.print(
                f"[yellow]No diffs found for either [bold]{left_id}[/bold] or [bold]{right_id}[/bold].[/yellow]"
            )
            raise SystemExit(1)

        if stat_only:
            from rich.table import Table

            t = Table(title="Stat Comparison", expand=True)
            t.add_column(left_id, style="cyan")
            t.add_column(right_id, style="magenta")
            t.add_row(
                left.stat_text or _NO_CHANGES,
                right.stat_text or _NO_CHANGES,
            )
            console.print(t)
            return

        _render_compare(left, right, left_id, right_id)
        return

    # ------------------------------------------------------------------
    # Single-task mode (original behavior)
    # ------------------------------------------------------------------
    if task_id is None:
        console.print("[red]Error:[/red] TASK_ID is required when not using --compare.")
        raise SystemExit(1)

    resolved = resolve_diff(task_id, root, agents, base)

    if not resolved.diff_text:
        agent_hint = f" (agent: [cyan]{resolved.session_id}[/cyan])" if resolved.session_id else ""
        console.print(f"[yellow]No diff found for task:[/yellow] [bold]{task_id}[/bold]{agent_hint}")
        console.print("[dim]The task may not have made any changes yet, or the branch/worktree was cleaned up.[/dim]")
        raise SystemExit(1)

    # Header
    if resolved.agent:
        task_ids_str = ", ".join(resolved.agent.get("task_ids", []))
        role = resolved.agent.get("role", "")
        model = resolved.agent.get("model", "")
        console.print(f"[bold]Task:[/bold] [cyan]{task_ids_str}[/cyan]  [dim]role={role}, model={model}[/dim]")
    if resolved.source_label:
        console.print(resolved.source_label)
    console.print()

    # Show file-level summary by default
    if not stat_only and not raw:
        _render_enhanced_summary(resolved)

    if stat_only:
        _render_stat_only(resolved, root, base)
        return

    if raw:
        console.print(resolved.diff_text)
        return

    # Syntax-highlighted diff
    from rich.syntax import Syntax

    syntax = Syntax(resolved.diff_text, "diff", theme="monokai", line_numbers=False)
    console.print(syntax)
