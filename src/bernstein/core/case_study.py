"""Case study generator for completed orchestration runs.

Reads metrics and traces from a run directory and produces a formatted
Markdown document suitable for sharing as a case study or post-mortem.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CaseStudyConfig:
    """Configuration for case study generation.

    Attributes:
        title: Custom title for the case study.
        author: Author name to include in the document.
        include_costs: Whether to include cost breakdown.
        include_timeline: Whether to include the timeline section.
    """

    title: str = ""
    author: str = ""
    include_costs: bool = True
    include_timeline: bool = True


def _load_json_safe(path: Path) -> dict[str, Any]:
    """Load a JSON file, returning empty dict on failure."""
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    return f"{hours:.1f}h"


def generate_case_study(run_dir: Path, config: CaseStudyConfig) -> str:
    """Generate a Markdown case study from a completed run.

    Reads .sdd/ metrics, traces, and summary data to produce a structured
    case study document.

    Args:
        run_dir: Path to the run directory containing .sdd/ state.
        config: Configuration controlling which sections to include.

    Returns:
        A formatted Markdown string.
    """
    sdd = run_dir / ".sdd"
    summary = _load_json_safe(sdd / "summary.json")
    metrics_dir = sdd / "metrics"

    # Collect task metrics.
    task_files: list[dict[str, Any]] = []
    if metrics_dir.is_dir():
        for f in sorted(metrics_dir.iterdir()):
            if f.suffix == ".json":
                data = _load_json_safe(f)
                if data:
                    task_files.append(data)

    goal = summary.get("goal", config.title or "Orchestration Run")
    title = config.title or goal
    total_tasks = summary.get("total_tasks", len(task_files))
    completed = summary.get("completed_tasks", total_tasks)
    failed = summary.get("failed_tasks", 0)
    total_cost = summary.get("total_cost_usd", 0.0)
    duration = summary.get("duration_s", 0.0)

    # Collect unique agents/models used.
    agents_used: set[str] = set()
    models_used: set[str] = set()
    for t in task_files:
        if role := t.get("role"):
            agents_used.add(str(role))
        if model := t.get("model"):
            models_used.add(str(model))

    sections: list[str] = []

    # Header.
    sections.append(f"# {title}")
    if config.author:
        sections.append(f"\n*Author: {config.author}*")
    sections.append("")

    # Executive Summary.
    sections.append("## Executive Summary")
    sections.append("")
    sections.append(
        f"This case study covers an orchestration run that executed "
        f"**{total_tasks}** tasks with **{completed}** completed "
        f"and **{failed}** failed."
    )
    sections.append("")

    # Problem Statement.
    sections.append("## Problem Statement")
    sections.append("")
    sections.append(f"{goal}")
    sections.append("")

    # Approach.
    sections.append("## Approach")
    sections.append("")
    if agents_used:
        sections.append(f"- **Agents**: {', '.join(sorted(agents_used))}")
    if models_used:
        sections.append(f"- **Models**: {', '.join(sorted(models_used))}")
    sections.append(f"- **Total tasks**: {total_tasks}")
    sections.append("")

    # Results.
    sections.append("## Results")
    sections.append("")
    sections.append(f"- **Tasks completed**: {completed}/{total_tasks}")
    if failed:
        sections.append(f"- **Tasks failed**: {failed}")
    if config.include_timeline and duration > 0:
        sections.append(f"- **Total duration**: {_format_duration(duration)}")
    if config.include_costs and total_cost > 0:
        sections.append(f"- **Total cost**: ${total_cost:.2f}")
    sections.append("")

    # Lessons Learned.
    sections.append("## Lessons Learned")
    sections.append("")
    if failed > 0:
        sections.append(f"- {failed} task(s) failed and may warrant investigation.")
    if total_cost > 0 and total_tasks > 0:
        avg_cost = total_cost / total_tasks
        sections.append(f"- Average cost per task: ${avg_cost:.4f}")
    if not agents_used:
        sections.append("- No agent role data available for analysis.")
    sections.append("")

    return "\n".join(sections)


def export_case_study(content: str, output_path: Path, format: str = "md") -> Path:
    """Write case study content to a file.

    Args:
        content: The Markdown content to write.
        output_path: Destination file path.
        format: Output format (currently only 'md' is supported).

    Returns:
        The path the file was written to.
    """
    if format != "md":
        logger.warning("Unsupported format '%s', falling back to md.", format)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path
