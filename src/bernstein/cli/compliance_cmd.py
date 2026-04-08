"""Compliance-related CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.compliance.eu_ai_act import ComplianceEngine, bernstein_descriptor
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


@compliance_group.command("assess")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Project root directory (parent of .sdd/).",
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(path_type=Path),
    help="Directory to write evidence package (default: <workdir>/.sdd/compliance/).",
)
@click.option("--version", default="1.0.0", show_default=True, help="System version string for the evidence package.")
@click.option("--json-output", "as_json", is_flag=True, help="Print compliance summary as JSON instead of a table.")
@click.option(
    "--no-export",
    is_flag=True,
    default=False,
    help="Skip writing evidence files to disk; only print the summary.",
)
def assess(workdir: Path, output_dir: Path | None, version: str, as_json: bool, no_export: bool) -> None:
    """Run EU AI Act compliance assessment for this Bernstein deployment.

    Classifies the Bernstein system under Annex III, generates Annex IV
    technical documentation, runs the conformity assessment, and writes the
    evidence package to disk (unless --no-export is set).
    """
    descriptor = bernstein_descriptor(version=version)
    engine = ComplianceEngine()

    if no_export:
        report = engine.run(descriptor, doc_version=version, include_tech_doc=True)
        _print_report(report, as_json)
        return

    out_dir = output_dir if output_dir is not None else workdir / ".sdd" / "compliance"
    package_path = engine.export_evidence_package(descriptor, out_dir, doc_version=version)
    report = json.loads(package_path.read_text(encoding="utf-8"))["report"]
    _print_report(report, as_json)
    if not as_json:
        click.echo(f"\nEvidence package written to: {package_path}")


@compliance_group.command("report")
@click.option(
    "--evidence-package",
    default=None,
    type=click.Path(path_type=Path, exists=True),
    help="Path to an existing evidence_package.json (default: <workdir>/.sdd/compliance/evidence_package.json).",
)
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Project root (used to locate default evidence package).",
)
@click.option("--json-output", "as_json", is_flag=True, help="Emit raw JSON.")
def report(evidence_package: Path | None, workdir: Path, as_json: bool) -> None:
    """Print the EU AI Act compliance report from an existing evidence package."""
    pkg_path = evidence_package or (workdir / ".sdd" / "compliance" / "evidence_package.json")
    if not pkg_path.exists():
        raise click.ClickException(
            f"Evidence package not found: {pkg_path}\n"
            "Run `bernstein compliance assess` first."
        )
    package = json.loads(pkg_path.read_text(encoding="utf-8"))
    rep = package.get("report", package)
    _print_report(rep, as_json)


def _print_report(report: dict[str, object], as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(report, indent=2))
        return

    classification = report.get("classification", {})
    conformity = report.get("conformity", {})
    summary = report.get("compliance_summary", {})

    risk = str(classification.get("risk_category", "unknown")).upper()
    domain = str(classification.get("annex_iii_domain", "N/A"))
    overall = str(conformity.get("overall_status", "unknown"))
    passed = conformity.get("passed", 0)
    failed = conformity.get("failed", 0)
    partial = conformity.get("partial", 0)

    click.echo("─" * 60)
    click.echo("  EU AI Act Compliance Assessment — Bernstein")
    click.echo("─" * 60)
    click.echo(f"  Risk Category    : {risk}")
    click.echo(f"  Annex III Domain : {domain}")
    click.echo(f"  Conformity Status: {overall}  (pass={passed}, fail={failed}, partial={partial})")

    justification = str(classification.get("justification", ""))
    if justification:
        click.echo(f"\n  Justification:\n    {justification}")

    gaps: list[object] = list(conformity.get("mandatory_gaps", []))  # type: ignore[arg-type]
    if gaps:
        click.echo("\n  Mandatory Gaps:")
        for gap in gaps:
            click.echo(f"    - {gap}")

    next_steps: list[object] = list(summary.get("next_steps", []))  # type: ignore[arg-type]
    if next_steps:
        click.echo("\n  Next Steps:")
        for step in next_steps:
            click.echo(f"    • {step}")

    deadline = str(summary.get("deadline", "N/A"))
    click.echo(f"\n  Deadline: {deadline}")
    click.echo("─" * 60)
