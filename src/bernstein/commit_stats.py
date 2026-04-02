"""Commit attribution stats — gather per-role commit stats via git log."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from io import StringIO
from typing import Any

from rich.console import Console

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoleStats:
    """Accumulated commit stats for a single agent role."""

    commits: int = 0
    lines_added: int = 0
    lines_deleted: int = 0

    def merge(self, other: RoleStats) -> RoleStats:
        """Return a new RoleStats with both sets summed."""
        return type(self)(
            commits=self.commits + other.commits,
            lines_added=self.lines_added + other.lines_added,
            lines_deleted=self.lines_deleted + other.lines_deleted,
        )


@dataclass
class CommitStatsResult:
    """Top-level result of a commit-stats query."""

    roles: dict[str, RoleStats] = field(default_factory=dict)
    total_commits: int = 0
    total_lines_added: int = 0
    total_lines_deleted: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (for JSON output or testing)."""
        return {
            "roles": {
                role: {"commits": rs.commits, "lines_added": rs.lines_added, "lines_deleted": rs.lines_deleted}
                for role, rs in self.roles.items()
            },
            "total_commits": self.total_commits,
            "total_lines_added": self.total_lines_added,
            "total_lines_deleted": self.total_lines_deleted,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Author-to-role mapping
# ---------------------------------------------------------------------------

# Heuristic: author name / email contains a role keyword.
_ROLE_KEYWORDS = (
    "backend",
    "frontend",
    "qa",
    "security",
    "devops",
    "docs",
    "manager",
    "architect",
)


def _author_to_role(author: str) -> str:
    """Map a git author string to a role label.

    Falls back to the full author name (lowercased) if no known keyword is
    found.
    """
    lower = author.lower()
    for keyword in _ROLE_KEYWORDS:
        if keyword in lower:
            return keyword
    return lower.strip()


# ---------------------------------------------------------------------------
# Core: git log runner
# ---------------------------------------------------------------------------

# Format: one line per commit — author<TAB>additions<TAB>deletions
_GIT_LOG_FMT = "%an <%ae>%t%ad"
_GIT_NUMSTAT_FMT = "%aN%n"  # author name then numstat block


def _run_git_log(
    repo_dir: str = ".",
    since: str | None = None,
    until: str | None = None,
) -> list[tuple[str, int, int]]:
    """Run ``git log --numstat`` and return rows of (author, added, deleted).

    We invoke git once and parse the combined output to avoid N+1 subprocess
    calls.
    """
    cmd: list[str] = [
        "git",
        "-C",
        repo_dir,
        "log",
        "--numstat",
        "--format=%an <%ae>",
        "--date=short",
    ]
    if since:
        cmd.extend(["--since", since])
    if until:
        cmd.extend(["--until", until])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return []

    lines = result.stdout.splitlines()
    rows: list[tuple[str, int, int]] = []
    current_author: str | None = None

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # Author lines are in "<Name <email>>" format — not numstat (which has tabs)
        if "\t" not in line:
            current_author = line
            continue
        if current_author is None:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        try:
            added = int(parts[0]) if parts[0] != "-" else 0
            deleted = int(parts[1]) if parts[1] != "-" else 0
        except ValueError:
            continue
        rows.append((current_author, added, deleted))

    return rows


def collect_commit_stats(
    repo_dir: str = ".",
    since: str | None = None,
    until: str | None = None,
) -> CommitStatsResult:
    """Collect commit attribution stats grouped by author role.

    Args:
        repo_dir: Path to the git repository.
        since: Date string for ``--since`` (e.g. ``"2025-01-01"``).
        until: Date string for ``--until`` (e.g. ``"2025-12-31"``).

    Returns:
        A ``CommitStatsResult`` with per-role and total stats.

    Raises:
        Nothing — returns ``error`` field on failure instead.
    """
    try:
        rows = _run_git_log(repo_dir, since, until)
    except (OSError, subprocess.SubprocessError) as exc:
        return CommitStatsResult(error=str(exc))

    if not rows:
        return CommitStatsResult()

    role_stats: dict[str, RoleStats] = {}
    total_added = 0
    total_deleted = 0
    total_commits_map: dict[str, int] = {}

    for author, added, deleted in rows:
        role = _author_to_role(author)
        if role not in role_stats:
            role_stats[role] = RoleStats()
        role_stats[role] = role_stats[role].merge(RoleStats(lines_added=added, lines_deleted=deleted))

        # Count commits — we count unique (author, changed-lines) groups
        # A simpler approach: increment per numstat line but track unique commits
        total_added += added
        total_deleted += deleted

    # Count unique commits via a second git log call (cheap)
    commit_cmd: list[str] = ["git", "-C", repo_dir, "log", "--oneline"]
    if since:
        commit_cmd.extend(["--since", since])
    if until:
        commit_cmd.extend(["--until", until])

    try:
        commit_result = subprocess.run(commit_cmd, capture_output=True, text=True)
        if commit_result.returncode == 0:
            author_cmd: list[str] = ["git", "-C", repo_dir, "log", "--format=%an <%ae>"]
            if since:
                author_cmd.extend(["--since", since])
            if until:
                author_cmd.extend(["--until", until])
            author_result = subprocess.run(author_cmd, capture_output=True, text=True)
            if author_result.returncode == 0:
                author_lines = [line.strip() for line in author_result.stdout.splitlines() if line.strip()]
                for author_line in author_lines:
                    role = _author_to_role(author_line)
                    total_commits_map[role] = total_commits_map.get(role, 0) + 1
    except (OSError, subprocess.SubprocessError) as exc:
        return CommitStatsResult(error=str(exc))

    # Set commit counts on role stats
    final_roles: dict[str, RoleStats] = {}
    for role in sorted(role_stats):
        stats = role_stats[role]
        final_roles[role] = RoleStats(
            commits=total_commits_map.get(role, 0),
            lines_added=stats.lines_added,
            lines_deleted=stats.lines_deleted,
        )

    return CommitStatsResult(
        roles=final_roles,
        total_commits=sum(total_commits_map.values()),
        total_lines_added=total_added,
        total_lines_deleted=total_deleted,
    )


# ---------------------------------------------------------------------------
# Display helper (Rich table)
# ---------------------------------------------------------------------------


def _make_table(result: CommitStatsResult) -> str:
    """Return a Rich-formatted table string.

    This is a separate pure function so tests can verify formatting without
    invoking ``console.print``.
    """
    from rich.table import Table

    if result.error:
        return f"[red]Error: {result.error}[/red]"

    table = Table(title="Commit Attribution", header_style="bold cyan", show_lines=False)
    table.add_column("Role", style="bold")
    table.add_column("Commits", justify="right")
    table.add_column("Lines Added", justify="right")
    table.add_column("Lines Deleted", justify="right")

    for role in sorted(result.roles.keys()):
        rs = result.roles[role]
        table.add_row(
            role,
            str(rs.commits),
            f"[green]+{rs.lines_added}[/green]",
            f"[red]-{rs.lines_deleted}[/red]",
        )

    # Totals row
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{result.total_commits}[/bold]",
        f"[bold][green]+{result.total_lines_added}[/green][/bold]",
        f"[bold][red]-{result.total_lines_deleted}[/red][/bold]",
    )

    # Render to string (no console needed)
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    console.print(table)
    return buf.getvalue()


def render_commit_stats(result: CommitStatsResult) -> None:
    """Print commit stats to stdout using Rich."""
    from bernstein.cli.helpers import console

    output = _make_table(result)
    console.print(output)
