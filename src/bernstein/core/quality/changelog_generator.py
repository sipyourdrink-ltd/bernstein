"""Automated changelog generation from agent-produced diffs.

Reads the task archive (``.sdd/archive/tasks.jsonl``) for a given run,
infers components from file paths, detects breaking changes in diffs,
and renders a structured Markdown changelog.

Typical usage::

    from pathlib import Path
    from bernstein.core.quality.changelog_generator import generate_changelog, render_markdown

    changelog = generate_changelog("run-20260412", Path(".sdd/archive/tasks.jsonl"))
    md = render_markdown(changelog)
    Path("CHANGELOG.md").write_text(md)
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

# Regex patterns for detecting breaking changes in unified diffs.
# A removed line starting with ``def `` or ``class `` in a public module
# (no leading underscore) is treated as a removed public API.
_RE_REMOVED_DEF = re.compile(r"^-\s*def\s+([a-zA-Z]\w*)\s*\(")
_RE_REMOVED_CLASS = re.compile(r"^-\s*class\s+([a-zA-Z]\w*)\s*[\(:]")

# A changed function signature: a removed ``def`` line followed (within a
# small window) by an added ``def`` line with the same name but different
# parameters.
_RE_ADDED_DEF = re.compile(r"^\+\s*def\s+([a-zA-Z]\w*)\s*\((.+)\)")
_RE_REMOVED_DEF_PARAMS = re.compile(r"^-\s*def\s+([a-zA-Z]\w*)\s*\((.+)\)")

# Component inference: map top-level path segments to logical components.
_COMPONENT_MAP: dict[str, str] = {
    "src/bernstein/core": "core",
    "src/bernstein/adapters": "adapters",
    "src/bernstein/cli": "cli",
    "templates": "templates",
    "tests": "tests",
    "docs": "docs",
    "scripts": "scripts",
    "sdk": "sdk",
}


@dataclass(frozen=True)
class ChangelogEntry:
    """A single change entry in the changelog.

    Attributes:
        component: Logical component name (e.g. ``"core"``, ``"cli"``).
        summary: Human-readable summary of the change.
        task_id: Originating task identifier.
        is_breaking: Whether this change is a breaking API change.
        files_changed: List of file paths modified by this entry.
    """

    component: str
    summary: str
    task_id: str
    is_breaking: bool = False
    files_changed: tuple[str, ...] = ()


@dataclass(frozen=True)
class Changelog:
    """A complete changelog for a run.

    Attributes:
        run_id: Identifier of the orchestrator run.
        version: Semantic version string (may be empty if unknown).
        date: ISO-8601 date string for the changelog.
        entries: All changelog entries.
        breaking_changes: Subset of entries flagged as breaking.
    """

    run_id: str
    version: str
    date: str
    entries: tuple[ChangelogEntry, ...] = ()
    breaking_changes: tuple[ChangelogEntry, ...] = ()


# ---------------------------------------------------------------------------
# Component inference
# ---------------------------------------------------------------------------


def _infer_component(file_path: str) -> str:
    """Infer a logical component name from a file path.

    Checks the path against known prefixes in :data:`_COMPONENT_MAP`.
    Falls back to the first path segment, or ``"other"`` for bare filenames.

    Args:
        file_path: Relative file path (e.g. ``"src/bernstein/core/server.py"``).

    Returns:
        Logical component name.
    """
    normalised = file_path.replace("\\", "/").lstrip("/")
    for prefix, component in _COMPONENT_MAP.items():
        if normalised.startswith(prefix):
            return component
    # Fall back to the first directory segment
    parts = Path(normalised).parts
    if len(parts) > 1:
        return parts[0]
    return "other"


def _dominant_component(files: list[str]) -> str:
    """Return the most common component across a list of file paths.

    Args:
        files: List of file paths.

    Returns:
        The component that appears most frequently, or ``"other"`` when
        *files* is empty.
    """
    if not files:
        return "other"
    counts: dict[str, int] = defaultdict(int)
    for f in files:
        counts[_infer_component(f)] += 1
    return max(counts, key=lambda c: counts[c])


# ---------------------------------------------------------------------------
# Breaking-change detection
# ---------------------------------------------------------------------------


def detect_breaking_changes(diff: str) -> list[str]:
    """Detect breaking changes in a unified diff.

    Looks for:
    - Removed public function/method definitions
    - Removed public class definitions
    - Changed function signatures (same name, different parameters)

    Args:
        diff: Unified diff text.

    Returns:
        List of human-readable descriptions of detected breaking changes.
    """
    breaking: list[str] = []
    removed_defs: dict[str, str] = {}  # name -> params

    for line in diff.splitlines():
        _process_diff_line(line, removed_defs, breaking)

    # Any remaining removed defs without a corresponding add are removals
    for name in removed_defs:
        breaking.append(f"Removed public function `{name}`")

    return breaking


def _process_diff_line(
    line: str,
    removed_defs: dict[str, str],
    breaking: list[str],
) -> None:
    """Process a single diff line for breaking-change detection."""
    m_rem = _RE_REMOVED_DEF_PARAMS.match(line)
    if m_rem:
        name, params = m_rem.group(1), m_rem.group(2)
        if not name.startswith("_"):
            removed_defs[name] = params
        return

    m_cls = _RE_REMOVED_CLASS.match(line)
    if m_cls:
        name = m_cls.group(1)
        if not name.startswith("_"):
            breaking.append(f"Removed public class `{name}`")
        return

    m_add = _RE_ADDED_DEF.match(line)
    if m_add:
        name, new_params = m_add.group(1), m_add.group(2)
        if name in removed_defs:
            old_params = removed_defs.pop(name)
            if old_params.strip() != new_params.strip():
                breaking.append(f"Changed signature of `{name}`")


# ---------------------------------------------------------------------------
# Changelog generation
# ---------------------------------------------------------------------------


def generate_changelog(
    run_id: str,
    archive_path: Path,
    *,
    version: str = "",
) -> Changelog:
    """Generate a changelog from the task archive.

    Reads completed task records from the archive JSONL file, builds
    :class:`ChangelogEntry` instances, and assembles them into a
    :class:`Changelog`.

    Args:
        run_id: Identifier for the orchestrator run.
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.
        version: Optional semantic version string for the changelog header.

    Returns:
        Populated :class:`Changelog` instance.
    """
    records = _read_archive(archive_path)
    if not records:
        logger.info("No archive records found at %s", archive_path)

    entries: list[ChangelogEntry] = []
    breaking_entries: list[ChangelogEntry] = []

    for record in records:
        task_id = str(record.get("task_id", "unknown"))
        title = str(record.get("title", ""))
        summary = str(record.get("result_summary") or title)
        raw_files: Any = record.get("owned_files")
        owned_files: list[str] = (
            [str(item) for item in cast("list[Any]", raw_files)] if isinstance(raw_files, list) else []
        )
        status = str(record.get("status", ""))

        # Only include completed tasks in the changelog
        if status not in ("done", "completed"):
            continue

        component = _dominant_component(owned_files)
        files_tuple = tuple(owned_files)

        entry = ChangelogEntry(
            component=component,
            summary=summary,
            task_id=task_id,
            is_breaking=False,
            files_changed=files_tuple,
        )
        entries.append(entry)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return Changelog(
        run_id=run_id,
        version=version,
        date=today,
        entries=tuple(entries),
        breaking_changes=tuple(breaking_entries),
    )


def _read_archive(archive_path: Path) -> list[dict[str, Any]]:
    """Read all records from an archive JSONL file.

    Args:
        archive_path: Path to the JSONL archive file.

    Returns:
        List of parsed JSON dicts.  Malformed lines are silently skipped.
    """
    if not archive_path.exists():
        return []

    records: list[dict[str, Any]] = []
    try:
        with archive_path.open(encoding="utf-8") as f:
            for line_num, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data: dict[str, Any] = json.loads(line)
                    records.append(data)
                except json.JSONDecodeError:
                    logger.warning(
                        "Malformed JSON at %s:%d — skipping",
                        archive_path,
                        line_num,
                    )
    except OSError as exc:
        logger.warning("Cannot read archive at %s: %s", archive_path, exc)

    return records


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def group_by_component(
    entries: tuple[ChangelogEntry, ...] | list[ChangelogEntry],
) -> dict[str, list[ChangelogEntry]]:
    """Group changelog entries by their component.

    Args:
        entries: Sequence of :class:`ChangelogEntry` instances.

    Returns:
        Dict mapping component names to lists of entries, sorted by
        component name.
    """
    groups: dict[str, list[ChangelogEntry]] = defaultdict(list)
    for entry in entries:
        groups[entry.component].append(entry)
    return dict(sorted(groups.items()))


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def render_markdown(changelog: Changelog) -> str:
    """Render a :class:`Changelog` as a Markdown string.

    Produces sections for breaking changes (if any), then groups remaining
    entries by component.

    Args:
        changelog: The changelog to render.

    Returns:
        Formatted Markdown string.
    """
    lines: list[str] = []

    version_label = changelog.version if changelog.version else changelog.run_id
    lines.extend(
        [
            f"# Changelog — {version_label}",
            "",
            f"**Date:** {changelog.date}",
            f"**Run ID:** {changelog.run_id}",
            "",
        ]
    )

    _render_breaking_section(lines, changelog.breaking_changes)
    _render_changes_section(lines, changelog.entries)

    return "\n".join(lines)


def _render_breaking_section(
    lines: list[str],
    breaking_changes: tuple[ChangelogEntry, ...],
) -> None:
    """Render the breaking changes section if any exist."""
    if not breaking_changes:
        return
    lines.append("## Breaking Changes")
    lines.append("")
    for entry in breaking_changes:
        lines.append(f"- **{entry.component}:** {entry.summary} (`{entry.task_id}`)")
    lines.append("")


def _render_entry_files(lines: list[str], files_changed: tuple[str, ...]) -> None:
    """Render the files list for a changelog entry."""
    if not files_changed:
        return
    file_list = ", ".join(f"`{f}`" for f in files_changed[:5])
    if len(files_changed) > 5:
        file_list += f" (+{len(files_changed) - 5} more)"
    lines.append(f"  Files: {file_list}")


def _render_changes_section(
    lines: list[str],
    entries: tuple[ChangelogEntry, ...],
) -> None:
    """Render the grouped changes section."""
    if not entries:
        lines.append("*No changes recorded.*")
        lines.append("")
        return

    lines.append("## Changes")
    lines.append("")
    grouped = group_by_component(entries)
    for component, component_entries in grouped.items():
        lines.append(f"### {component}")
        lines.append("")
        for entry in component_entries:
            prefix = "**BREAKING** " if entry.is_breaking else ""
            lines.append(f"- {prefix}{entry.summary} (`{entry.task_id}`)")
            _render_entry_files(lines, entry.files_changed)
        lines.append("")
