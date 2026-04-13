"""Git read operations for building agent context.

Provides functions to extract intelligence from git history — blame summaries,
hot file detection, co-change graphs, and recent change context.  Injected into
agent prompts as warm context before they start working.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Low-level helper
# ------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path, *, timeout: int = 10) -> str | None:
    """Run a read-only git command, returning stdout or None on failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        args_str = " ".join(str(a) for a in args)
        logger.debug("git %s failed: %s", args_str, exc)
    return None


# ------------------------------------------------------------------
# File listing
# ------------------------------------------------------------------


def ls_files(cwd: Path) -> list[str]:
    """Return all git-tracked files.

    Args:
        cwd: Repository root.

    Returns:
        List of relative file paths, or empty list on failure.
    """
    out = _run_git(["ls-files"], cwd, timeout=5)
    return out.splitlines() if out else []


def ls_files_pattern(cwd: Path, pattern: str) -> list[str]:
    """Return git-tracked files matching a glob pattern.

    Args:
        cwd: Repository root.
        pattern: Glob pattern (e.g. ``"*.py"``, ``"src/**/__init__.py"``).

    Returns:
        List of relative file paths.
    """
    out = _run_git(["ls-files", pattern], cwd, timeout=5)
    return out.splitlines() if out else []


# ------------------------------------------------------------------
# Blame
# ------------------------------------------------------------------


def _parse_blame_porcelain(output: str) -> list[tuple[str, str, str]]:
    """Parse git blame --line-porcelain output into (author, summary, time) tuples.

    Args:
        output: Raw porcelain output from ``git blame``.

    Returns:
        List of (author, summary, author-time) tuples.
    """
    changes: list[tuple[str, str, str]] = []
    current_author = ""
    current_summary = ""
    current_time = ""

    for line in output.splitlines():
        if line.startswith("author "):
            current_author = line[7:]
        elif line.startswith("author-time "):
            current_time = line[12:]
        elif line.startswith("summary "):
            current_summary = line[8:]
            if current_summary and current_author:
                changes.append((current_author, current_summary, current_time))

    return changes


def _dedup_blame_entries(changes: list[tuple[str, str, str]], max_entries: int) -> list[tuple[str, str, str]]:
    """Deduplicate blame entries by summary, keeping most recent.

    Args:
        changes: Raw (author, summary, time) tuples.
        max_entries: Maximum entries to return.

    Returns:
        Deduplicated and sorted entries.
    """
    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for author, summary, ts in changes:
        if summary not in seen:
            seen.add(summary)
            unique.append((author, summary, ts))

    unique.sort(key=lambda x: x[2], reverse=True)
    return unique[:max_entries]


def blame_summary(
    cwd: Path,
    file_path: str,
    line_range: tuple[int, int] | None = None,
    max_entries: int = 5,
) -> str:
    """Get recent authors and commit messages for key sections of a file.

    Args:
        cwd: Repository root.
        file_path: Relative path to the file.
        line_range: Optional (start, end) line range to analyze.
        max_entries: Maximum number of unique changes to summarize.

    Returns:
        Human-readable summary string.
    """
    cmd = ["blame", "--line-porcelain"]
    if line_range:
        cmd.extend([f"-L{line_range[0]},{line_range[1]}"])
    cmd.append(file_path)

    out = _run_git(cmd, cwd, timeout=15)
    if not out:
        return "(no blame data available)"

    changes = _parse_blame_porcelain(out)
    if not changes:
        return "(no blame data available)"

    entries = _dedup_blame_entries(changes, max_entries)
    lines = [f"- {_epoch_to_relative(ts)}: {summary} ({author})" for author, summary, ts in entries]
    return "\n".join(lines) if lines else "(no blame data available)"


# ------------------------------------------------------------------
# Hot files
# ------------------------------------------------------------------


def hot_files(cwd: Path, days: int = 14, max_results: int = 10) -> list[tuple[str, int]]:
    """Find files with the most commits in the last N days.

    Args:
        cwd: Repository root.
        days: Look-back period.
        max_results: Maximum number of files to return.

    Returns:
        List of (file_path, commit_count) tuples, most active first.
    """
    out = _run_git(
        ["log", f"--since={days}.days", "--name-only", "--pretty=format:"],
        cwd,
        timeout=15,
    )
    if not out:
        return []

    counts: Counter[str] = Counter()
    for line in out.splitlines():
        line = line.strip()
        if line:
            counts[line] += 1

    return counts.most_common(max_results)


# ------------------------------------------------------------------
# Co-change graph
# ------------------------------------------------------------------


def cochange_files(
    cwd: Path,
    file_path: str,
    depth: int = 20,
    max_results: int = 5,
) -> list[tuple[str, int]]:
    """Find files that frequently change in the same commits as *file_path*.

    Args:
        cwd: Repository root.
        file_path: The file to analyze.
        depth: Number of commits to scan.
        max_results: Maximum co-changed files to return.

    Returns:
        List of (co_changed_path, count) tuples.
    """
    out = _run_git(
        [
            "log",
            "--pretty=format:%H",
            "--name-only",
            "--follow",
            f"-{depth}",
            "--",
            file_path,
        ],
        cwd,
    )
    if not out:
        return []

    counts: Counter[str] = Counter()
    for block in out.split("\n\n"):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        for f in lines[1:]:
            if f != file_path and f.endswith(".py"):
                counts[f] += 1

    return counts.most_common(max_results)


# ------------------------------------------------------------------
# Recent changes
# ------------------------------------------------------------------


def recent_changes(
    cwd: Path,
    file_path: str,
    n: int = 5,
) -> list[dict[str, str]]:
    """Last N commits touching a file with hash and subject.

    Args:
        cwd: Repository root.
        file_path: Relative path to the file.
        n: Number of commits.

    Returns:
        List of dicts with ``hash``, ``subject``, and ``relative_date`` keys.
    """
    out = _run_git(
        [
            "log",
            "--follow",
            f"-{n}",
            "--pretty=format:%h|%s|%ar",
            "--",
            file_path,
        ],
        cwd,
    )
    if not out:
        return []

    result: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) >= 3:
            result.append(
                {
                    "hash": parts[0],
                    "subject": parts[1],
                    "relative_date": parts[2],
                }
            )
        elif len(parts) == 2:
            result.append(
                {
                    "hash": parts[0],
                    "subject": parts[1],
                    "relative_date": "",
                }
            )
    return result


def recent_changes_multi(
    cwd: Path,
    files: list[str],
    max_entries: int = 5,
) -> list[str]:
    """Get recent git log summaries touching any of the given files.

    Args:
        cwd: Repository root.
        files: Relative file paths.
        max_entries: Maximum number of log entries.

    Returns:
        List of formatted commit lines.
    """
    if not files:
        return []
    out = _run_git(
        ["log", "--pretty=format:%h: %s", f"-{max_entries}", "--", *files],
        cwd,
    )
    if not out:
        return []
    return out.splitlines()[:max_entries]


# ------------------------------------------------------------------
# Context builder
# ------------------------------------------------------------------


def build_agent_git_context(cwd: Path, owned_files: list[str]) -> str:
    """Build a markdown git context block for injection into agent prompts.

    Args:
        cwd: Repository root.
        owned_files: Files the agent will work on.

    Returns:
        Formatted markdown string with file history, hot files, co-changes.
    """
    if not owned_files:
        return ""

    sections: list[str] = ["### Git Context (auto-generated)"]

    for fpath in owned_files[:5]:  # Cap to avoid huge prompts
        sections.extend(_file_history_section(cwd, fpath))

    _append_hot_files_section(sections, cwd, owned_files)

    return "\n".join(sections)


def _file_history_section(cwd: Path, fpath: str) -> list[str]:
    """Build the history + co-change lines for a single file.

    Args:
        cwd: Repository root.
        fpath: Relative file path.

    Returns:
        Lines of markdown to append to the context.
    """
    lines: list[str] = [f"\n#### File history for {fpath}:"]
    changes = recent_changes(cwd, fpath, n=5)
    if changes:
        for ch in changes:
            date = ch.get("relative_date", "")
            lines.append(f"- {date}: {ch['subject']} ({ch['hash']})")
    else:
        lines.append("- (no history)")

    cochanges = cochange_files(cwd, fpath, max_results=3)
    if cochanges:
        parts = [f"{f} ({c}x)" for f, c in cochanges]
        lines.append(f"- Co-changes with: {', '.join(parts)}")
    return lines


def _append_hot_files_section(sections: list[str], cwd: Path, owned_files: list[str]) -> None:
    """Append hot-files section if any owned files have high churn.

    Args:
        sections: Accumulated sections list (mutated in place).
        cwd: Repository root.
        owned_files: Files owned by the agent.
    """
    hots = hot_files(cwd, days=14, max_results=5)
    if not hots:
        return
    owned_set = set(owned_files)
    hot_owned = [(f, c) for f, c in hots if f in owned_set]
    if hot_owned:
        sections.append("\n#### Hot files (high churn, last 14 days):")
        for f, c in hot_owned:
            sections.append(f"- {f}: {c} commits")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _epoch_to_relative(epoch_str: str) -> str:
    """Convert a Unix epoch string to a rough relative time."""
    try:
        import time

        now = time.time()
        epoch = int(epoch_str)
        delta = now - epoch
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        days = int(delta / 86400)
        if days == 1:
            return "1d ago"
        if days < 30:
            return f"{days}d ago"
        return f"{int(days / 30)}mo ago"
    except (ValueError, TypeError):
        return "unknown"


_BLAME_TIME_RE = re.compile(r"author-time (\d+)")
