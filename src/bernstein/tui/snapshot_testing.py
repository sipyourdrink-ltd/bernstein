"""TUI visual regression snapshot testing utilities.

Provides helpers for capturing widget text output, comparing against saved
snapshots, and reporting diffs — enabling lightweight visual regression
tests without a full Textual pilot session.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class SnapshotConfig:
    """Configuration for TUI snapshot testing.

    Attributes:
        snapshot_dir: Directory for saved snapshots (relative or absolute).
        update_mode: When True, overwrite existing snapshots instead of comparing.
        terminal_size: Virtual terminal dimensions (columns, rows).
    """

    snapshot_dir: str = ".sdd/tui_snapshots"
    update_mode: bool = False
    terminal_size: tuple[int, int] = (120, 40)


@dataclass(frozen=True)
class SnapshotResult:
    """Result of a snapshot comparison.

    Attributes:
        widget_name: Identifier for the widget being tested.
        matched: True if current output matches the saved snapshot.
        diff_lines: Unified diff lines when there is a mismatch.
        snapshot_path: Filesystem path to the snapshot file.
    """

    widget_name: str
    matched: bool
    diff_lines: list[str] = field(default_factory=lambda: list[str]())
    snapshot_path: str = ""


# ---------------------------------------------------------------------------
# ANSI stripping
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text*."""
    return _ANSI_RE.sub("", text)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def capture_widget_text(_widget_class_name: str, render_text: str) -> str:
    """Normalize a widget's text output for snapshot comparison.

    Strips ANSI escape codes, collapses runs of whitespace within lines to a
    single space, and strips trailing whitespace from every line.

    Args:
        widget_class_name: Name of the widget class (used for documentation /
            logging, not included in the output).
        render_text: Raw text output from the widget.

    Returns:
        Normalized text ready for diffing.
    """
    cleaned = _strip_ansi(render_text)
    lines = cleaned.splitlines()
    normalized: list[str] = []
    for line in lines:
        # Collapse internal whitespace runs and strip trailing spaces.
        collapsed = re.sub(r"[ \t]+", " ", line).rstrip()
        normalized.append(collapsed)
    return "\n".join(normalized)


def compare_snapshot(
    widget_name: str,
    current: str,
    snapshot_dir: Path,
) -> SnapshotResult:
    """Compare *current* render text against a saved snapshot.

    If no snapshot exists yet the current text is saved as the baseline and
    the result is reported as matched.

    Args:
        widget_name: Identifier for the widget snapshot.
        current: Normalized text to compare.
        snapshot_dir: Directory containing snapshot files.

    Returns:
        A ``SnapshotResult`` describing match/mismatch.
    """
    snap_path = snapshot_dir / f"{widget_name}.snap"

    if not snap_path.exists():
        # First run — persist the baseline.
        update_snapshot(widget_name, current, snapshot_dir)
        return SnapshotResult(
            widget_name=widget_name,
            matched=True,
            diff_lines=[],
            snapshot_path=str(snap_path),
        )

    saved = snap_path.read_text(encoding="utf-8")
    if saved == current:
        return SnapshotResult(
            widget_name=widget_name,
            matched=True,
            diff_lines=[],
            snapshot_path=str(snap_path),
        )

    diff = list(
        difflib.unified_diff(
            saved.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile=f"snapshot/{widget_name}",
            tofile=f"current/{widget_name}",
        )
    )
    return SnapshotResult(
        widget_name=widget_name,
        matched=False,
        diff_lines=diff,
        snapshot_path=str(snap_path),
    )


def update_snapshot(
    widget_name: str,
    content: str,
    snapshot_dir: Path,
) -> Path:
    """Write or overwrite a snapshot file.

    Args:
        widget_name: Identifier for the widget snapshot.
        content: Normalized text to persist.
        snapshot_dir: Directory for snapshot files (created if missing).

    Returns:
        Path to the written snapshot file.
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snap_path = snapshot_dir / f"{widget_name}.snap"
    snap_path.write_text(content, encoding="utf-8")
    return snap_path


def list_snapshots(snapshot_dir: Path) -> list[str]:
    """List saved snapshot names (without the ``.snap`` extension).

    Args:
        snapshot_dir: Directory containing snapshot files.

    Returns:
        Sorted list of snapshot names.  Returns an empty list when the
        directory does not exist.
    """
    if not snapshot_dir.is_dir():
        return []
    return sorted(p.stem for p in snapshot_dir.glob("*.snap"))


def format_snapshot_diff(result: SnapshotResult) -> str:
    """Format a ``SnapshotResult`` diff for human-readable display.

    Args:
        result: The comparison result to format.

    Returns:
        Multi-line string describing the outcome.  For matches the string
        simply states "OK"; for mismatches it includes the unified diff.
    """
    if result.matched:
        return f"[OK] {result.widget_name}: snapshot matches"

    header = f"[MISMATCH] {result.widget_name}: snapshot differs"
    diff_text = "".join(result.diff_lines)
    return f"{header}\n{diff_text}"
