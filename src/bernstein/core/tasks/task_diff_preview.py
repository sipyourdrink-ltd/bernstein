"""Task diff preview before merge approval (TASK-016).

Parses ``git diff --stat --numstat`` output into structured data and
produces Rich-formatted or compact summaries for review before a task
branch is merged.

Usage::

    diff_output = subprocess.check_output(
        ["git", "diff", "--stat", "--numstat", "main...HEAD"],
        text=True, encoding="utf-8", errors="replace",
    )
    summary = build_diff_summary("task-042", diff_output, {"pytest": "passed"})
    print(format_diff_preview(summary))
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileDiff:
    """Diff statistics for a single file.

    Attributes:
        path: File path relative to the repo root.
        status: Kind of change applied to the file.
        lines_added: Number of lines added.
        lines_removed: Number of lines removed.
        old_path: Previous path when the file was renamed.
    """

    path: str
    status: Literal["added", "modified", "deleted", "renamed"]
    lines_added: int
    lines_removed: int
    old_path: str | None = None


@dataclass(frozen=True)
class DiffSummary:
    """Aggregated diff summary for a task.

    Attributes:
        task_id: Identifier of the task this diff belongs to.
        total_files: Number of files changed.
        files: Per-file diff statistics.
        total_added: Aggregate lines added across all files.
        total_removed: Aggregate lines removed across all files.
        test_results: Tool name to outcome mapping.
        generated_at: ISO-8601 timestamp when the summary was created.
    """

    task_id: str
    total_files: int
    files: list[FileDiff] = field(default_factory=list)
    total_added: int = 0
    total_removed: int = 0
    test_results: dict[str, str] = field(default_factory=dict)
    generated_at: str = ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Matches a --numstat line: <added>\t<removed>\t<path>
# Binary files show "-" for both counts.
_NUMSTAT_RE = re.compile(
    r"^(?P<added>\d+|-)\t(?P<removed>\d+|-)\t(?P<path>.+)$",
)

# Rename pattern inside --numstat: {old => new} or old => new
_RENAME_BRACE_RE = re.compile(
    r"^(?P<prefix>[^{\n]{0,500})\{(?P<old>[^}\n]{0,500}) => (?P<new>[^}\n]{0,500})\}(?P<suffix>[^\n]{0,500})$",
)
_RENAME_ARROW_RE = re.compile(
    r"^(?P<old>[^\n]{1,500}?) => (?P<new>[^\n]{1,500})$",
)


def _parse_rename(raw_path: str) -> tuple[str, str]:
    """Return (old_path, new_path) from a numstat rename entry."""
    m = _RENAME_BRACE_RE.match(raw_path)
    if m:
        prefix = m.group("prefix")
        suffix = m.group("suffix")
        old = prefix + m.group("old") + suffix
        new = prefix + m.group("new") + suffix
        return old, new

    m = _RENAME_ARROW_RE.match(raw_path)
    if m:
        return m.group("old"), m.group("new")

    return raw_path, raw_path


def _classify_file(
    added: int,
    removed: int,
    is_rename: bool,
) -> Literal["added", "modified", "deleted", "renamed"]:
    """Determine the file change status from line counts."""
    if is_rename:
        return "renamed"
    if removed == 0 and added > 0:
        return "added"
    if added == 0 and removed > 0:
        return "deleted"
    return "modified"


def parse_git_diff_stat(diff_output: str) -> list[FileDiff]:
    """Parse ``git diff --numstat`` output into a list of :class:`FileDiff`.

    Args:
        diff_output: Raw stdout from ``git diff --stat --numstat``.

    Returns:
        Ordered list of per-file diff records.
    """
    results: list[FileDiff] = []
    for line in diff_output.splitlines():
        line = line.strip()
        m = _NUMSTAT_RE.match(line)
        if not m:
            continue

        raw_added = m.group("added")
        raw_removed = m.group("removed")

        # Binary files report "-"; treat as zero.
        added = int(raw_added) if raw_added != "-" else 0
        removed = int(raw_removed) if raw_removed != "-" else 0
        raw_path = m.group("path")

        is_rename = "=>" in raw_path
        old_path: str | None = None
        if is_rename:
            old_path, path = _parse_rename(raw_path)
        else:
            path = raw_path

        status = _classify_file(added, removed, is_rename)
        results.append(
            FileDiff(
                path=path,
                status=status,
                lines_added=added,
                lines_removed=removed,
                old_path=old_path if is_rename else None,
            ),
        )
    return results


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def build_diff_summary(
    task_id: str,
    diff_output: str,
    test_results: dict[str, str] | None = None,
) -> DiffSummary:
    """Build a :class:`DiffSummary` from raw ``git diff --numstat`` output.

    Args:
        task_id: Task identifier.
        diff_output: Raw stdout from ``git diff --stat --numstat``.
        test_results: Optional mapping of tool name to outcome string.

    Returns:
        Populated :class:`DiffSummary`.
    """
    files = parse_git_diff_stat(diff_output)
    total_added = sum(f.lines_added for f in files)
    total_removed = sum(f.lines_removed for f in files)
    now = datetime.now(tz=UTC).isoformat()
    return DiffSummary(
        task_id=task_id,
        total_files=len(files),
        files=files,
        total_added=total_added,
        total_removed=total_removed,
        test_results=test_results or {},
        generated_at=now,
    )


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_diff_preview(summary: DiffSummary) -> str:
    """Produce a Rich-compatible multi-line diff preview.

    The output includes a header, a file table with per-file +/- counts,
    aggregate statistics, and test results (when available).

    Args:
        summary: The diff summary to format.

    Returns:
        Multi-line string suitable for Rich console output.
    """
    lines: list[str] = []
    lines.append(f"[bold]Diff Preview: {summary.task_id}[/bold]")
    lines.append(f"Generated: {summary.generated_at}")
    lines.append("")

    # File table
    if summary.files:
        # Determine column width for alignment.
        max_path = max(len(_display_path(f)) for f in summary.files)
        col_width = max(max_path, 4) + 2

        lines.append(f"{'File':<{col_width}} {'Status':<10} {'Added':>6} {'Removed':>7}")
        lines.append("-" * (col_width + 10 + 6 + 7 + 3))
        for f in summary.files:
            display = _display_path(f)
            lines.append(
                f"{display:<{col_width}} {f.status:<10} "
                f"[green]+{f.lines_added}[/green]{' ':>2}"
                f"[red]-{f.lines_removed}[/red]",
            )
    else:
        lines.append("(no files changed)")

    # Totals
    lines.append("")
    lines.append(
        f"[bold]{summary.total_files} file(s) changed, "
        f"[green]+{summary.total_added}[/green] "
        f"[red]-{summary.total_removed}[/red][/bold]",
    )

    # Test results
    if summary.test_results:
        lines.append("")
        lines.append("[bold]Test Results:[/bold]")
        for tool, outcome in sorted(summary.test_results.items()):
            lines.append(f"  {tool}: {outcome}")

    return "\n".join(lines)


def format_compact_diff(summary: DiffSummary) -> str:
    """One-line compact summary of a diff.

    Example output::

        3 files changed, +45 -12, tests: passed

    Args:
        summary: The diff summary to format.

    Returns:
        Single-line human-readable summary string.
    """
    parts: list[str] = [
        f"{summary.total_files} file(s) changed",
        f"+{summary.total_added} -{summary.total_removed}",
    ]
    if summary.test_results:
        outcomes = ", ".join(f"{k}: {v}" for k, v in sorted(summary.test_results.items()))
        parts.append(outcomes)
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _display_path(f: FileDiff) -> str:
    """Format path for display, including rename arrow when applicable."""
    if f.old_path is not None:
        return f"{f.old_path} -> {f.path}"
    return f.path
