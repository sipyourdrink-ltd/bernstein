"""README update reminder: detect public API changes in git diffs.

When an agent's diff adds a new CLI command or config option, the orchestrator
should remind the implementing agent to update README.md.  This module
provides the detection logic and the reminder message generator.

Detection heuristics
--------------------
CLI commands
    Any ``+@click.command`` or ``+@app.command`` decorator on an added line
    in a file under ``src/bernstein/cli/`` signals a new top-level command.

Click options / arguments
    ``+@click.option`` or ``+@click.argument`` added to a CLI file means a
    new public flag or positional argument was introduced.

Config keys
    New string literals assigned with ``=`` in ``home.py`` or
    ``bernstein.yaml`` additions often indicate new documented settings.

Usage
-----
Typical call site (guardrails or post-merge hook)::

    from bernstein.core.readme_reminder import detect_api_changes, remind_message

    changes = detect_api_changes(diff)
    if changes:
        print(remind_message(changes))
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Added lines in a diff start with '+' but not '++' (which is the file header)
_ADDED_LINE: Final = re.compile(r"^\+(?!\+)")

# Signals a new click command decorator
_CLICK_COMMAND: Final = re.compile(r"@(?:click\.command|app\.command)\b")

# Signals a new click option or argument decorator
_CLICK_OPTION: Final = re.compile(r"@click\.(?:option|argument)\b")

# Config key: assignment of a string constant — e.g. ``"model" = "claude-sonnet"``
_CONFIG_KEY: Final = re.compile(r'["\']([a-z][a-z0-9_]{2,})["\'\s]*(?::=|=)\s*["\'\d]')

# File header in a unified diff — tells us which file follows
_FILE_HEADER: Final = re.compile(r"^\+\+\+ b/(.+)$")

# CLI source files live here
_CLI_PATH_PREFIX: Final = "src/bernstein/cli/"

# Config source files that may contain public settings
_CONFIG_PATH_SUFFIXES: Final = ("home.py", "bernstein.yaml", "config.py")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class APIChange:
    """One detected public-API addition in a git diff.

    Attributes:
        kind: ``"command"``, ``"option"``, or ``"config_key"``.
        name: The command name, option flag, or config key (best-effort).
        file: Source file path from the diff header.
    """

    kind: str
    name: str
    file: str

    def __str__(self) -> str:
        return f"{self.kind} {self.name!r} in {self.file}"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_api_changes(diff: str) -> list[APIChange]:
    """Scan a unified diff for new CLI commands, options, and config keys.

    Args:
        diff: Git diff output (``git diff`` or ``git show``).

    Returns:
        List of :class:`APIChange` objects, one per detected addition.
        Empty list when the diff contains no public API additions.
    """
    changes: list[APIChange] = []
    current_file = ""

    for raw_line in diff.splitlines():
        # Track which file we're in
        m_file = _FILE_HEADER.match(raw_line)
        if m_file:
            current_file = m_file.group(1)
            continue

        # Only process added lines
        if not _ADDED_LINE.match(raw_line):
            continue

        line = raw_line[1:]  # strip leading '+'

        # New CLI command decorator
        if _CLICK_COMMAND.search(line) and current_file.startswith(_CLI_PATH_PREFIX):
            changes.append(APIChange(kind="command", name=_extract_command_name(line), file=current_file))
            continue

        # New click option / argument
        if _CLICK_OPTION.search(line) and current_file.startswith(_CLI_PATH_PREFIX):
            changes.append(APIChange(kind="option", name=_extract_option_name(line), file=current_file))
            continue

        # New config key in a config-related file
        if any(current_file.endswith(suffix) for suffix in _CONFIG_PATH_SUFFIXES):
            m_cfg = _CONFIG_KEY.search(line)
            if m_cfg:
                changes.append(APIChange(kind="config_key", name=m_cfg.group(1), file=current_file))

    return changes


# ---------------------------------------------------------------------------
# Name extraction helpers (best-effort)
# ---------------------------------------------------------------------------


def _extract_command_name(line: str) -> str:
    """Try to extract the command name from a ``@click.command(name=...)`` line."""
    m = re.search(r'name\s*=\s*["\']([^"\']+)["\']', line)
    if m:
        return m.group(1)
    return "<new command>"


def _extract_option_name(line: str) -> str:
    """Try to extract the first flag name from a ``@click.option(...)`` line."""
    m = re.search(r'["\'](-{1,2}[a-z][a-z0-9_-]*)["\']', line)
    if m:
        return m.group(1)
    return "<new option>"


# ---------------------------------------------------------------------------
# Reminder message
# ---------------------------------------------------------------------------

_REMINDER_HEADER: Final = (
    "README update required — the following public API additions were detected:"
)


def remind_message(changes: list[APIChange]) -> str:
    """Format a human-readable reminder from a list of detected API changes.

    Returns an empty string when *changes* is empty (nothing to remind).

    Args:
        changes: Output of :func:`detect_api_changes`.

    Returns:
        Multi-line markdown reminder string, or ``""`` for an empty list.
    """
    if not changes:
        return ""

    lines = [_REMINDER_HEADER, ""]
    for change in changes:
        lines.append(f"  - {change}")
    lines.append("")
    lines.append("Please update README.md to document these additions before completing the task.")
    return "\n".join(lines)
