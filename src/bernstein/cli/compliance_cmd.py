"""Compliance-related CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.core.eu_ai_act import summarize_assessments


@click.group("compliance")
def compliance_group() -> None:
    """Inspect compliance artifacts and summaries."""


@compliance_group.command("eu-ai-act")
@click.option("--workdir", default=".", show_default=True, type=click.Path(path_type=Path))
@click.option("--json-output", "as_json", is_flag=True, help="Emit raw JSON instead of a table.")
def eu_ai_act_status(workdir: Path, as_json: bool) -> None:
    """Show the current EU AI Act task-risk summary."""

    summary = summarize_assessments(workdir / ".sdd")
    payload = {
        "total": summary.total,
        "counts": summary.counts,
        "latest_high_risk": [
            {
                "task_id": record.task_id,
                "title": record.title,
                "role": record.role,
                "risk_level": record.risk_level,
                "approval_required": record.approval_required,
                "assessed_at": record.assessed_at,
            }
            for record in summary.latest_high_risk
        ],
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo("EU AI Act Task Risk Summary")
    click.echo(f"  Total assessments: {summary.total}")
    for level in ("minimal", "limited", "high", "unacceptable"):
        click.echo(f"  {level:>12}: {summary.counts.get(level, 0)}")
    if not summary.latest_high_risk:
        click.echo("  No high-risk or unacceptable tasks recorded.")
        return
    click.echo("")
    click.echo("Latest high-risk tasks:")
    for record in summary.latest_high_risk:
        suffix = " (approval required)" if record.approval_required else ""
        click.echo(f"  - {record.task_id} [{record.risk_level}] {record.title}{suffix}")
