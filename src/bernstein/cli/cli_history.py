"""CLI command history tracking and undo suggestions.

Records every CLI invocation to ``.sdd/cli_history.jsonl`` so users can
review past commands and receive suggestions for undoing destructive
operations.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HistoryEntry:
    """A single recorded CLI invocation.

    Attributes:
        command: The CLI command name (e.g. ``"stop"``, ``"task cancel"``).
        args: Positional and flag arguments passed to the command.
        timestamp: ISO-8601 timestamp of the invocation.
        cwd: Working directory at invocation time.
        exit_code: Process exit code, or ``None`` if not yet known.
    """

    command: str
    args: list[str]
    timestamp: str
    cwd: str
    exit_code: int | None


# ---------------------------------------------------------------------------
# Undo map — maps destructive commands to their logical inverse
# ---------------------------------------------------------------------------

UNDO_MAP: dict[str, str] = {
    "stop": "run",
    "task cancel": "task retry",
    "drain": "undrain",
}

# ---------------------------------------------------------------------------
# Default history path
# ---------------------------------------------------------------------------

_DEFAULT_HISTORY_PATH = Path(".sdd/cli_history.jsonl")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_command(
    command: str,
    args: list[str],
    exit_code: int | None = None,
    history_path: Path | None = None,
) -> None:
    """Append a command invocation to the JSONL history file.

    Args:
        command: CLI command name.
        args: Arguments passed to the command.
        exit_code: Process exit code (``None`` if unknown).
        history_path: Override for the history file location.
    """
    path = history_path or _DEFAULT_HISTORY_PATH
    entry = HistoryEntry(
        command=command,
        args=args,
        timestamp=datetime.now(tz=timezone.utc).isoformat(),
        cwd=str(Path.cwd()),
        exit_code=exit_code,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(entry)) + "\n")


def load_history(
    history_path: Path | None = None,
    limit: int = 50,
) -> list[HistoryEntry]:
    """Load recent history entries from the JSONL file.

    Args:
        history_path: Override for the history file location.
        limit: Maximum number of entries to return (most recent first).

    Returns:
        Up to *limit* entries ordered newest-first.
    """
    path = history_path or _DEFAULT_HISTORY_PATH
    if not path.exists():
        return []

    entries: list[HistoryEntry] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            data = json.loads(stripped)
            entries.append(HistoryEntry(**data))

    # Return most-recent entries first.
    return entries[-limit:][::-1]


def suggest_undo(command: str) -> str | None:
    """Return the inverse command for *command*, or ``None``.

    Args:
        command: The CLI command to look up.

    Returns:
        The inverse command string if one is defined, else ``None``.
    """
    return UNDO_MAP.get(command)


def is_destructive(command: str) -> bool:
    """Check whether *command* is considered destructive.

    A command is destructive if it has an entry in :data:`UNDO_MAP`.

    Args:
        command: The CLI command to check.

    Returns:
        ``True`` when an undo mapping exists for the command.
    """
    return command in UNDO_MAP


def format_history(entries: list[HistoryEntry], limit: int = 20) -> str:
    """Format history entries as a human-readable table.

    Args:
        entries: History entries to format (newest-first expected).
        limit: Maximum rows to include.

    Returns:
        A multi-line string containing a simple text table.
    """
    rows = entries[:limit]
    if not rows:
        return "No command history."

    header = f"{'Timestamp':<28} {'Command':<20} {'Exit':<6} {'Args'}"
    sep = "-" * len(header)
    lines: list[str] = [header, sep]

    for entry in rows:
        exit_str = str(entry.exit_code) if entry.exit_code is not None else "-"
        args_str = " ".join(entry.args) if entry.args else ""
        lines.append(f"{entry.timestamp:<28} {entry.command:<20} {exit_str:<6} {args_str}")

    return "\n".join(lines)
