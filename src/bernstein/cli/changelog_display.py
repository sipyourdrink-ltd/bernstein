"""Changelog display — parse, filter, and format CHANGELOG.md content.

Provides structured parsing of CHANGELOG.md into ``ChangelogEntry`` objects
and Rich-formatted output for the upgrade command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangelogEntry:
    """A single changelog version entry.

    Attributes:
        version: Semantic version string, e.g. ``"1.6.3"``.
        date: Release date string, e.g. ``"2026-04-10"``.
        changes: List of non-breaking change descriptions.
        breaking: List of breaking-change descriptions.
    """

    version: str
    date: str
    changes: list[str] = field(default_factory=list)
    breaking: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(
    r"^##\s+\[(?P<version>[^\]]+)\]\s*-\s*(?P<date>\S+)",
)

_BREAKING_RE = re.compile(
    r"^\s*[-*]\s*(?:BREAKING:\s*|⚠\s*)(.*)",
)

_CHANGE_RE = re.compile(
    r"^\s*[-*]\s+(.*)",
)


def parse_changelog(content: str) -> list[ChangelogEntry]:
    """Parse CHANGELOG.md content into structured entries.

    Expected format::

        ## [1.2.0] - 2026-03-15
        - Added widget support
        - BREAKING: Removed legacy API

        ## [1.1.0] - 2026-02-01
        - Fixed login timeout

    Args:
        content: Raw CHANGELOG.md text.

    Returns:
        List of ``ChangelogEntry`` objects, in document order (newest first
        when the changelog follows the conventional newest-first layout).
    """
    entries: list[ChangelogEntry] = []
    current_changes: list[str] = []
    current_breaking: list[str] = []
    current_version: str | None = None
    current_date: str | None = None

    for line in content.splitlines():
        header_match = _HEADER_RE.match(line)
        if header_match:
            # Flush the previous entry
            if current_version is not None and current_date is not None:
                entries.append(
                    ChangelogEntry(
                        version=current_version,
                        date=current_date,
                        changes=current_changes,
                        breaking=current_breaking,
                    )
                )
            current_version = header_match.group("version")
            current_date = header_match.group("date")
            current_changes = []
            current_breaking = []
            continue

        # Only process bullet lines when inside a version section
        if current_version is None:
            continue

        breaking_match = _BREAKING_RE.match(line)
        if breaking_match:
            text = breaking_match.group(1).strip()
            if text:
                current_breaking.append(text)
            continue

        change_match = _CHANGE_RE.match(line)
        if change_match:
            text = change_match.group(1).strip()
            if text:
                current_changes.append(text)

    # Flush the last entry
    if current_version is not None and current_date is not None:
        entries.append(
            ChangelogEntry(
                version=current_version,
                date=current_date,
                changes=current_changes,
                breaking=current_breaking,
            )
        )

    return entries


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def _parse_version_tuple(ver: str) -> tuple[int, ...]:
    """Parse a version string into a comparable integer tuple.

    Non-numeric segments become 0.  At most four segments are kept.

    Args:
        ver: Version string, e.g. ``"1.2.3"``.

    Returns:
        Tuple of ints, e.g. ``(1, 2, 3)``.
    """
    parts: list[int] = []
    for segment in ver.split(".")[:4]:
        try:
            parts.append(int(segment))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def filter_changelog(
    entries: list[ChangelogEntry],
    from_version: str,
    to_version: str,
) -> list[ChangelogEntry]:
    """Return entries whose version is in ``(from_version, to_version]``.

    Args:
        entries: Full list of changelog entries.
        from_version: Exclusive lower bound (the currently installed version).
        to_version: Inclusive upper bound (the target version).

    Returns:
        Filtered entries preserving their original order.
    """
    from_tuple = _parse_version_tuple(from_version)
    to_tuple = _parse_version_tuple(to_version)

    return [
        entry
        for entry in entries
        if from_tuple < _parse_version_tuple(entry.version) <= to_tuple
    ]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_changelog_display(entries: list[ChangelogEntry]) -> str:
    """Format changelog entries as Rich-markup text for terminal display.

    Each version is rendered with a bold header, followed by bullet-point
    changes.  Breaking changes are highlighted in red with a warning prefix.

    Args:
        entries: Changelog entries to format.

    Returns:
        Rich-markup string ready for ``console.print()``.
    """
    if not entries:
        return "[dim]No changelog entries to display.[/dim]"

    sections: list[str] = []
    for entry in entries:
        lines: list[str] = []
        lines.append(f"[bold cyan]v{entry.version}[/bold cyan]  [dim]{entry.date}[/dim]")

        for change in entry.changes:
            lines.append(f"  [green]-[/green] {change}")

        for brk in entry.breaking:
            lines.append(f"  [bold red]BREAKING:[/bold red] {brk}")

        sections.append("\n".join(lines))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def has_breaking_changes(entries: list[ChangelogEntry]) -> bool:
    """Check whether any entry contains breaking changes.

    Args:
        entries: Changelog entries to inspect.

    Returns:
        ``True`` if at least one entry has a non-empty ``breaking`` list.
    """
    return any(len(entry.breaking) > 0 for entry in entries)
