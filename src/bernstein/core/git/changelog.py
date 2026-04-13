"""Generate changelog from completed tasks."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _ChangelogCategories:
    """Mutable accumulator for categorized changelog entries."""

    features: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    documentation: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """Return True if no entries in any category."""
        return not any([self.features, self.improvements, self.fixes, self.documentation, self.other])


def _categorize_task(title: str, task_type: str, role: str, cats: _ChangelogCategories) -> None:
    """Classify a completed task into a changelog category.

    Args:
        title: Task title.
        task_type: Task type field (e.g. "feature", "standard").
        role: Agent role that completed the task.
        cats: Categories accumulator (mutated in place).
    """
    title_lower = title.lower()
    if "fix" in title_lower or "bug" in title_lower or "patch" in title_lower:
        cats.fixes.append(f"- {title}")
    elif "doc" in title_lower or "readme" in title_lower:
        cats.documentation.append(f"- {title}")
    elif "improv" in title_lower or "optim" in title_lower or "refactor" in title_lower:
        cats.improvements.append(f"- {title}")
    elif task_type == "feature" or "feat" in title_lower:
        cats.features.append(f"- {title}")
    elif role in ("backend", "frontend", "qa", "devops"):
        cats.improvements.append(f"- {title} ({role})")
    else:
        cats.other.append(f"- {title}")


def _collect_tasks(tasks_file: Path, cutoff_time: float) -> _ChangelogCategories:
    """Parse tasks.jsonl and categorize completed tasks after cutoff.

    Args:
        tasks_file: Path to the JSONL tasks file.
        cutoff_time: Unix timestamp; tasks created before this are skipped.

    Returns:
        Populated categories.
    """
    cats = _ChangelogCategories()
    for line in tasks_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if data.get("created_at", 0) < cutoff_time:
            continue
        if data.get("status", "") != "done":
            continue

        _categorize_task(
            data.get("title", "Untitled"),
            data.get("type", "standard"),
            data.get("role", ""),
            cats,
        )
    return cats


def _format_changelog(cats: _ChangelogCategories, period_days: int) -> str:
    """Render categories into a markdown changelog string.

    Args:
        cats: Populated changelog categories.
        period_days: Number of days the changelog covers.

    Returns:
        Markdown-formatted changelog.
    """
    lines = ["# Changelog", ""]
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Period: Last {period_days} days")
    lines.append("")

    _sections: list[tuple[str, list[str]]] = [
        ("## \u2728 Features", cats.features),
        ("## \U0001f680 Improvements", cats.improvements),
        ("## \U0001f41b Bug Fixes", cats.fixes),
        ("## \U0001f4da Documentation", cats.documentation),
        ("## \U0001f4e6 Other Changes", cats.other),
    ]
    for heading, items in _sections:
        if items:
            lines.append(heading)
            lines.extend(sorted(items))
            lines.append("")

    if cats.is_empty():
        lines.append("No changes recorded in this period.")
        lines.append("")

    return "\n".join(lines)


def generate_changelog(
    workdir: Path,
    period_days: int = 30,
    output_path: Path | None = None,
) -> str:
    """Generate a changelog from completed tasks.

    Groups task titles by type (Features, Fixes, etc.) and formats
    them as a markdown changelog.

    Args:
        workdir: Repository root directory.
        period_days: Number of days to include in changelog.
        output_path: Optional path to write changelog. If None, returns string.

    Returns:
        Markdown-formatted changelog.
    """
    tasks_file = workdir / ".sdd" / "metrics" / "tasks.jsonl"

    if not tasks_file.exists():
        return "# Changelog\n\nNo task data available.\n"

    cutoff_time = time.time() - (period_days * 24 * 60 * 60)

    try:
        cats = _collect_tasks(tasks_file, cutoff_time)
    except OSError:
        return "# Changelog\n\nError reading task data.\n"

    changelog = _format_changelog(cats, period_days)

    if output_path:
        output_path.write_text(changelog, encoding="utf-8")

    return changelog
