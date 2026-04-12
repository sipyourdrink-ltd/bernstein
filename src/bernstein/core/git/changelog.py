"""Generate changelog from completed tasks."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


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
    metrics_dir = workdir / ".sdd" / "metrics"
    tasks_file = metrics_dir / "tasks.jsonl"

    if not tasks_file.exists():
        return "# Changelog\n\nNo task data available.\n"

    cutoff_time = time.time() - (period_days * 24 * 60 * 60)

    # Categorize tasks
    features: list[str] = []
    fixes: list[str] = []
    improvements: list[str] = []
    documentation: list[str] = []
    other: list[str] = []

    try:
        for line in tasks_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                task_created = data.get("created_at", 0)
                if task_created < cutoff_time:
                    continue

                status = data.get("status", "")
                if status != "done":
                    continue

                title = data.get("title", "Untitled")
                task_type = data.get("type", "standard")
                role = data.get("role", "")

                # Categorize by type/role/title keywords
                title_lower = title.lower()
                if "fix" in title_lower or "bug" in title_lower or "patch" in title_lower:
                    fixes.append(f"- {title}")
                elif "doc" in title_lower or "readme" in title_lower:
                    documentation.append(f"- {title}")
                elif "improv" in title_lower or "optim" in title_lower or "refactor" in title_lower:
                    improvements.append(f"- {title}")
                elif task_type == "feature" or "feat" in title_lower:
                    features.append(f"- {title}")
                elif role in ("backend", "frontend", "qa", "devops"):
                    improvements.append(f"- {title} ({role})")
                else:
                    other.append(f"- {title}")
            except json.JSONDecodeError:
                continue
    except OSError:
        return "# Changelog\n\nError reading task data.\n"

    # Build changelog
    lines = ["# Changelog", ""]
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Period: Last {period_days} days")
    lines.append("")

    if features:
        lines.append("## ✨ Features")
        lines.extend(sorted(features))
        lines.append("")

    if improvements:
        lines.append("## 🚀 Improvements")
        lines.extend(sorted(improvements))
        lines.append("")

    if fixes:
        lines.append("## 🐛 Bug Fixes")
        lines.extend(sorted(fixes))
        lines.append("")

    if documentation:
        lines.append("## 📚 Documentation")
        lines.extend(sorted(documentation))
        lines.append("")

    if other:
        lines.append("## 📦 Other Changes")
        lines.extend(sorted(other))
        lines.append("")

    if not any([features, improvements, fixes, documentation, other]):
        lines.append("No changes recorded in this period.")
        lines.append("")

    changelog = "\n".join(lines)

    if output_path:
        output_path.write_text(changelog, encoding="utf-8")

    return changelog
