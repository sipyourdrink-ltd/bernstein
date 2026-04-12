"""Embeddable status widget for GitHub PR descriptions.

Generates Markdown tables, SVG badges, and injects them into PR bodies
via the ``gh`` CLI so reviewers see run quality at a glance.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Any, cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunSummary:
    """Structured summary of a single orchestration run."""

    run_id: str
    agents_used: list[str]
    total_cost_usd: float
    quality_gate_passed: bool
    quality_score: int
    duration_seconds: float
    tasks_completed: int
    tasks_failed: int


@dataclass(frozen=True)
class StatusWidget:
    """Rendered widget ready for injection into a PR body."""

    markdown: str
    badge_url: str
    details_url: str


# ---------------------------------------------------------------------------
# build_run_summary
# ---------------------------------------------------------------------------

_METRIC_DEFAULTS: dict[str, Any] = {
    "agents_used": [],
    "total_cost_usd": 0.0,
    "quality_gate_passed": False,
    "quality_score": 0,
    "duration_seconds": 0.0,
    "tasks_completed": 0,
    "tasks_failed": 0,
}


def build_run_summary(run_id: str, metrics: dict[str, Any]) -> RunSummary:
    """Collect run metrics into a structured summary.

    Args:
        run_id: Unique identifier for the orchestration run.
        metrics: Dict of metric values; missing keys fall back to safe
            defaults so callers do not need to provide every field.

    Returns:
        A populated ``RunSummary``.
    """
    agents_raw: object = metrics.get("agents_used", _METRIC_DEFAULTS["agents_used"])
    if isinstance(agents_raw, list):
        agents: list[str] = [str(item) for item in cast("list[object]", agents_raw)]
    elif isinstance(agents_raw, tuple):
        agents = [str(item) for item in cast("tuple[object, ...]", agents_raw)]
    else:
        agents = []

    def _float(key: str) -> float:
        val = metrics.get(key, _METRIC_DEFAULTS[key])
        try:
            return float(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return float(_METRIC_DEFAULTS[key])

    def _int(key: str) -> int:
        val = metrics.get(key, _METRIC_DEFAULTS[key])
        try:
            return int(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return int(_METRIC_DEFAULTS[key])

    def _bool(key: str) -> bool:
        val = metrics.get(key, _METRIC_DEFAULTS[key])
        if isinstance(val, bool):
            return val
        return bool(val)

    return RunSummary(
        run_id=run_id,
        agents_used=agents,
        total_cost_usd=_float("total_cost_usd"),
        quality_gate_passed=_bool("quality_gate_passed"),
        quality_score=_int("quality_score"),
        duration_seconds=_float("duration_seconds"),
        tasks_completed=_int("tasks_completed"),
        tasks_failed=_int("tasks_failed"),
    )


# ---------------------------------------------------------------------------
# render_widget_markdown
# ---------------------------------------------------------------------------

WIDGET_SENTINEL = "<!-- bernstein-status-widget -->"


def render_widget_markdown(summary: RunSummary) -> str:
    """Render a Markdown status table from a run summary.

    The table is wrapped in an HTML comment sentinel so
    ``inject_widget_into_pr`` can locate and replace it on
    subsequent runs.

    Args:
        summary: The run summary to render.

    Returns:
        A Markdown string containing the status table.
    """
    status_label = "passed" if summary.quality_gate_passed else "failed"
    status_icon = "white_check_mark" if summary.quality_gate_passed else "x"
    duration_min = summary.duration_seconds / 60.0

    lines = [
        WIDGET_SENTINEL,
        "",
        "### Bernstein Run Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Run | `{summary.run_id}` |",
        f"| Quality gate | :{status_icon}: {status_label} |",
        f"| Quality score | {summary.quality_score}/100 |",
        f"| Tasks completed | {summary.tasks_completed} |",
        f"| Tasks failed | {summary.tasks_failed} |",
        f"| Agents | {', '.join(summary.agents_used) or 'none'} |",
        f"| Cost | ${summary.total_cost_usd:.2f} |",
        f"| Duration | {duration_min:.1f} min |",
        "",
        WIDGET_SENTINEL,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# generate_badge_svg
# ---------------------------------------------------------------------------

_BADGE_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="20">'
    '<rect width="{width}" height="20" rx="3" fill="#555"/>'
    '<rect x="{label_width}" width="{value_width}" height="20" rx="3" fill="{color}"/>'
    '<rect x="{label_width}" width="4" height="20" fill="{color}"/>'
    '<text x="{label_x}" y="14" font-family="Verdana,sans-serif" '
    'font-size="11" fill="#fff">{label}</text>'
    '<text x="{value_x}" y="14" font-family="Verdana,sans-serif" '
    'font-size="11" fill="#fff">{value}</text>'
    "</svg>"
)


def generate_badge_svg(summary: RunSummary) -> str:
    """Generate an SVG badge reflecting the run quality gate result.

    Args:
        summary: The run summary to render into a badge.

    Returns:
        An SVG string suitable for embedding or serving as an image.
    """
    label = "bernstein"
    if summary.quality_gate_passed:
        value = f"score {summary.quality_score}"
        color = "#4c1" if summary.quality_score >= 80 else "#dfb317"
    else:
        value = "failed"
        color = "#e05d44"

    label_width = len(label) * 7 + 10
    value_width = len(value) * 7 + 10
    width = label_width + value_width

    return _BADGE_TEMPLATE.format(
        width=width,
        label_width=label_width,
        value_width=value_width,
        color=color,
        label_x=label_width // 2,
        value_x=label_width + value_width // 2,
        label=label,
        value=value,
    )


# ---------------------------------------------------------------------------
# inject_widget_into_pr
# ---------------------------------------------------------------------------


def inject_widget_into_pr(pr_number: int, widget: StatusWidget) -> bool:
    """Append or replace the status widget in a GitHub PR description.

    Uses the ``gh`` CLI to read the current PR body, strips any
    existing widget block (delimited by ``WIDGET_SENTINEL``), and
    appends the new widget markdown.

    Args:
        pr_number: The GitHub PR number to update.
        widget: The rendered widget to inject.

    Returns:
        ``True`` if the PR was updated successfully, ``False`` otherwise.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--json", "body", "--jq", ".body"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        current_body = result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("Failed to read PR #%d body: %s", pr_number, exc)
        return False

    new_body = replace_widget_block(current_body, widget.markdown)

    try:
        subprocess.run(
            ["gh", "pr", "edit", str(pr_number), "--body", new_body],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("Failed to update PR #%d body: %s", pr_number, exc)
        return False

    return True


def replace_widget_block(body: str, widget_md: str) -> str:
    """Strip an existing widget block from *body* and append *widget_md*.

    Args:
        body: The current PR body text.
        widget_md: The new widget markdown to append.

    Returns:
        The updated body with the old widget removed and the new one appended.
    """
    parts = body.split(WIDGET_SENTINEL)
    if len(parts) >= 3:
        # parts[0] = before first sentinel, parts[1] = old widget, parts[2:] = after closing sentinel.
        cleaned = parts[0].rstrip()
        # Everything after the closing sentinel is preserved.
        trailing = WIDGET_SENTINEL.join(parts[2:]).strip()
        if trailing:
            cleaned = cleaned + "\n\n" + trailing
    else:
        cleaned = body.rstrip()

    if cleaned:
        return cleaned + "\n\n" + widget_md
    return widget_md
