"""Diff folding — collapsible diff display for large changes.

Provides fold/expand functionality for file diffs in the TUI,
showing summary information when folded and full diffs when expanded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DiffHunk:
    """A single diff hunk with fold state.

    Attributes:
        header: The hunk header line (e.g. @@ -10,5 +10,7 @@).
        lines: All lines in this hunk (including header).
        start_line: Starting line number in the new file.
        end_line: Ending line number in the new file.
        added: Number of added lines.
        removed: Number of removed lines.
        is_folded: Whether this hunk is currently folded.
    """

    header: str
    lines: list[str]
    start_line: int
    end_line: int
    added: int
    removed: int
    is_folded: bool = True


@dataclass
class FileDiff:
    """A diff for a single file with fold state.

    Attributes:
        filename: The file path.
        hunks: List of diff hunks.
        is_folded: Whether the entire file diff is folded.
        total_added: Total lines added.
        total_removed: Total lines removed.
    """

    filename: str
    hunks: list[DiffHunk]
    is_folded: bool = True
    total_added: int = 0
    total_removed: int = 0


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


_HUNK_RE = re.compile(r"^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@(.*)$")


def _parse_hunk_header(line: str) -> tuple[int, int] | None:
    """Parse a diff hunk header.

    Args:
        line: The hunk header line.

    Returns:
        Tuple of (start_line, end_line) or None if not a valid header.
    """
    m = _HUNK_RE.match(line)
    if not m:
        return None

    new_start = int(m.group(3))
    new_count = int(m.group(4)) if m.group(4) else 1

    return (new_start, new_start + new_count - 1)


def _count_changes(lines: list[str]) -> tuple[int, int]:
    """Count added and removed lines in a hunk.

    Args:
        lines: Lines in the hunk.

    Returns:
        Tuple of (added, removed).
    """
    added = 0
    removed = 0

    for line in lines:
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1

    return added, removed


def parse_diff(diff_text: str) -> list[FileDiff]:
    """Parse a unified diff into foldable FileDiff objects.

    Args:
        diff_text: The full unified diff text.

    Returns:
        List of FileDiff objects.
    """
    files: list[FileDiff] = []
    current_file: str | None = None
    current_hunks: list[DiffHunk] = []
    current_hunk_lines: list[str] = []
    current_hunk_header: str | None = None
    current_start: int = 0
    current_end: int = 0

    for line in diff_text.splitlines():
        # Detect file header
        if line.startswith("diff --git"):
            # Save previous file
            if current_file and current_hunks:
                total_added = sum(h.added for h in current_hunks)
                total_removed = sum(h.removed for h in current_hunks)
                files.append(
                    FileDiff(
                        filename=current_file,
                        hunks=current_hunks,
                        total_added=total_added,
                        total_removed=total_removed,
                    )
                )
            current_file = None
            current_hunks = []
            current_hunk_lines = []
            current_hunk_header = None
            continue

        # Detect filename
        if line.startswith("--- a/") or line.startswith("+++ b/"):
            if line.startswith("+++ b/"):
                current_file = line[6:]
            continue

        # Detect hunk header
        hunk_range = _parse_hunk_header(line)
        if hunk_range is not None:
            # Save previous hunk
            if current_hunk_header and current_hunk_lines:
                added, removed = _count_changes(current_hunk_lines)
                current_hunks.append(
                    DiffHunk(
                        header=current_hunk_header,
                        lines=current_hunk_lines,
                        start_line=current_start,
                        end_line=current_end,
                        added=added,
                        removed=removed,
                    )
                )

            current_hunk_header = line
            current_hunk_lines = [line]
            current_start, current_end = hunk_range
            continue

        # Accumulate hunk content
        if current_hunk_header is not None:
            current_hunk_lines.append(line)

    # Save last hunk
    if current_hunk_header and current_hunk_lines:
        added, removed = _count_changes(current_hunk_lines)
        current_hunks.append(
            DiffHunk(
                header=current_hunk_header,
                lines=current_hunk_lines,
                start_line=current_start,
                end_line=current_end,
                added=added,
                removed=removed,
            )
        )

    # Save last file
    if current_file and current_hunks:
        total_added = sum(h.added for h in current_hunks)
        total_removed = sum(h.removed for h in current_hunks)
        files.append(
            FileDiff(
                filename=current_file,
                hunks=current_hunks,
                total_added=total_added,
                total_removed=total_removed,
            )
        )

    return files


# ---------------------------------------------------------------------------
# Folding
# ---------------------------------------------------------------------------


def toggle_file_fold(file_diff: FileDiff) -> FileDiff:
    """Toggle fold state of an entire file diff.

    Args:
        file_diff: The file diff to toggle.

    Returns:
        The same FileDiff with is_folded toggled.
    """
    file_diff.is_folded = not file_diff.is_folded
    return file_diff


def toggle_hunk_fold(hunk: DiffHunk) -> DiffHunk:
    """Toggle fold state of a single hunk.

    Args:
        hunk: The hunk to toggle.

    Returns:
        The same DiffHunk with is_folded toggled.
    """
    hunk.is_folded = not hunk.is_folded
    return hunk


def fold_all(files: list[FileDiff]) -> list[FileDiff]:
    """Fold all files and hunks.

    Args:
        files: List of file diffs.

    Returns:
        The same list with all items folded.
    """
    for f in files:
        f.is_folded = True
        for h in f.hunks:
            h.is_folded = True
    return files


def expand_all(files: list[FileDiff]) -> list[FileDiff]:
    """Expand all files and hunks.

    Args:
        files: List of file diffs.

    Returns:
        The same list with all items expanded.
    """
    for f in files:
        f.is_folded = False
        for h in f.hunks:
            h.is_folded = False
    return files


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def format_file_summary(file_diff: FileDiff) -> str:
    """Format a file diff as a summary line (folded state).

    Args:
        file_diff: The file diff.

    Returns:
        Formatted summary string.
    """
    hunks_count = len(file_diff.hunks)
    parts = [f"  {file_diff.filename}"]
    parts.append(f"(+{file_diff.total_added}/-{file_diff.total_removed}")
    parts.append(f"{hunks_count} {'hunk' if hunks_count == 1 else 'hunks'})")

    return " ".join(parts)


def format_hunk_summary(hunk: DiffHunk) -> str:
    """Format a hunk as a summary line (folded state).

    Args:
        hunk: The diff hunk.

    Returns:
        Formatted summary string.
    """
    return f"    {hunk.header}  (+{hunk.added}/-{hunk.removed})"


def render_folding_diff(
    files: list[FileDiff],
    max_folded_lines: int = 3,
) -> str:
    """Render diff with folding support.

    Args:
        files: List of file diffs with fold states.
        max_folded_lines: Max lines to show when hunk is folded.

    Returns:
        Formatted diff string.
    """
    output: list[str] = []

    for file_diff in files:
        # File header
        icon = "▸" if file_diff.is_folded else "▾"
        summary = format_file_summary(file_diff)
        output.append(f"{icon} {summary}")

        if file_diff.is_folded:
            continue

        # Hunks
        for hunk in file_diff.hunks:
            hunk_icon = "▸" if hunk.is_folded else "▾"
            hunk_summary = format_hunk_summary(hunk)
            output.append(f"  {hunk_icon} {hunk_summary}")

            if hunk.is_folded:
                # Show first few lines
                for line in hunk.lines[:max_folded_lines]:
                    output.append(f"      {line}")
                remaining = len(hunk.lines) - max_folded_lines
                if remaining > 0:
                    output.append(f"      ... ({remaining} more lines)")
            else:
                # Show all lines
                for line in hunk.lines:
                    output.append(f"      {line}")

        output.append("")

    return "\n".join(output)


def render_full_diff(files: list[FileDiff]) -> str:
    """Render full diff without folding (traditional display).

    Args:
        files: List of file diffs.

    Returns:
        Full diff string.
    """
    output: list[str] = []

    for file_diff in files:
        output.append(f"diff --git a/{file_diff.filename} b/{file_diff.filename}")
        for hunk in file_diff.hunks:
            output.extend(hunk.lines)
        output.append("")

    return "\n".join(output)
