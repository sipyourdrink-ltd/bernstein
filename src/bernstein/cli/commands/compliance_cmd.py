"""Compliance-related CLI commands."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from bernstein.compliance.eu_ai_act import ComplianceEngine, bernstein_descriptor
from bernstein.core.compliance_policies import (
    ALL_POLICIES,
    ComplianceFramework,
    CompliancePolicyLibrary,
    PolicyInput,
    PolicySeverity,
    evaluate_all,
    evaluate_framework,
)
from bernstein.core.eu_ai_act import summarize_assessments

_FRAMEWORK_CHOICES = [f.value for f in ComplianceFramework]


@click.group("compliance")
def compliance_group() -> None:
    """Inspect compliance artifacts and policy enforcement."""


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
        raise click.ClickException(f"Evidence package not found: {pkg_path}\nRun `bernstein compliance assess` first.")
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


# ---------------------------------------------------------------------------
# Compliance-as-code: policy library commands
# ---------------------------------------------------------------------------


@compliance_group.command("enable")
@click.argument("framework", type=click.Choice(_FRAMEWORK_CHOICES, case_sensitive=False))
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Project root (parent of .sdd/).",
)
def enable_framework(framework: str, workdir: Path) -> None:
    """Activate a compliance framework policy set.

    Writes a marker file under <workdir>/.sdd/compliance/enabled/ so the
    policies persist across restarts.  Use ``bernstein compliance check`` to
    evaluate the enabled policies against the current configuration.

    FRAMEWORK is one of: soc2, iso27001, pci_dss, nist_800_53.
    """
    fw = ComplianceFramework(framework.lower())
    config_dir = workdir / ".sdd" / "compliance"
    lib = CompliancePolicyLibrary()
    lib.enable(fw, config_dir=config_dir)
    count = lib.policy_count(fw)
    click.echo(f"Enabled {fw.value} compliance framework ({count} policies).")
    click.echo(f"Marker written to: {config_dir / 'enabled' / fw.value}.yaml")
    click.echo("Run 'bernstein compliance check' to evaluate current configuration.")


@compliance_group.command("disable")
@click.argument("framework", type=click.Choice(_FRAMEWORK_CHOICES, case_sensitive=False))
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(path_type=Path),
)
def disable_framework(framework: str, workdir: Path) -> None:
    """Deactivate a compliance framework.

    FRAMEWORK is one of: soc2, iso27001, pci_dss, nist_800_53.
    """
    fw = ComplianceFramework(framework.lower())
    config_dir = workdir / ".sdd" / "compliance"
    lib = CompliancePolicyLibrary()
    lib.disable(fw, config_dir=config_dir)
    click.echo(f"Disabled {fw.value} compliance framework.")


@compliance_group.command("list")
@click.option(
    "--framework",
    default=None,
    type=click.Choice(_FRAMEWORK_CHOICES, case_sensitive=False),
    help="Filter by framework.",
)
@click.option("--json-output", "as_json", is_flag=True)
def list_policies(framework: str | None, as_json: bool) -> None:
    """List available compliance policies.

    Use --framework to filter by a specific standard (soc2, iso27001, etc.).
    """
    if framework:
        fw = ComplianceFramework(framework.lower())
        from bernstein.core.compliance_policies import _BY_FRAMEWORK

        policies = _BY_FRAMEWORK[fw]
    else:
        policies = ALL_POLICIES

    if as_json:
        data = [
            {
                "policy_id": p.policy_id,
                "name": p.name,
                "framework": p.framework.value,
                "control_id": p.control_id,
                "severity": p.severity.value,
                "description": p.description,
            }
            for p in policies
        ]
        click.echo(json.dumps(data, indent=2))
        return

    click.echo(f"{'ID':<22} {'Framework':<12} {'Control':<14} {'Sev':<12} Name")
    click.echo("─" * 90)
    for p in policies:
        click.echo(f"{p.policy_id:<22} {p.framework.value:<12} {p.control_id:<14} {p.severity.value:<12} {p.name}")
    click.echo(f"\nTotal: {len(policies)} policies")


@compliance_group.command("check")
@click.option(
    "--framework",
    default=None,
    type=click.Choice(_FRAMEWORK_CHOICES, case_sensitive=False),
    help="Evaluate only this framework.",
)
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Project root to load enabled frameworks from.",
)
@click.option("--json-output", "as_json", is_flag=True)
@click.option(
    "--fail-on",
    default="critical",
    type=click.Choice(["critical", "high", "medium", "low", "none"], case_sensitive=False),
    show_default=True,
    help="Exit non-zero if any failing policy has at least this severity.",
)
# Runtime snapshot overrides — pass current configuration state:
@click.option("--audit-logging/--no-audit-logging", default=False)
@click.option("--audit-hmac-chain/--no-audit-hmac-chain", default=False)
@click.option("--sandbox-enabled/--no-sandbox-enabled", default=False)
@click.option("--seccomp-enabled/--no-seccomp-enabled", default=False)
@click.option("--tls-enforced/--no-tls-enforced", default=False)
@click.option("--mfa-enabled/--no-mfa-enabled", default=False)
@click.option("--rbac-enabled/--no-rbac-enabled", default=False)
@click.option("--encrypt-at-rest/--no-encrypt-at-rest", default=False)
@click.option("--vulnerability-scanning/--no-vulnerability-scanning", default=False)
@click.option("--secrets-rotation-days", default=999, type=int, show_default=True)
def check_policies(
    framework: str | None,
    workdir: Path,
    as_json: bool,
    fail_on: str,
    audit_logging: bool,
    audit_hmac_chain: bool,
    sandbox_enabled: bool,
    seccomp_enabled: bool,
    tls_enforced: bool,
    mfa_enabled: bool,
    rbac_enabled: bool,
    encrypt_at_rest: bool,
    vulnerability_scanning: bool,
    secrets_rotation_days: int,
) -> None:
    """Evaluate compliance policies against the current runtime configuration.

    Pass --<flag> / --no-<flag> options to describe the current state of your
    deployment.  The command exits non-zero if any failing policy meets the
    severity threshold set by --fail-on (default: critical).
    """
    inp = PolicyInput(
        audit_logging=audit_logging,
        audit_hmac_chain=audit_hmac_chain,
        sandbox_enabled=sandbox_enabled,
        seccomp_enabled=seccomp_enabled,
        tls_enforced=tls_enforced,
        mfa_enabled=mfa_enabled,
        rbac_enabled=rbac_enabled,
        encrypt_at_rest=encrypt_at_rest,
        vulnerability_scanning=vulnerability_scanning,
        secrets_rotation_days=secrets_rotation_days,
    )

    if framework:
        fw = ComplianceFramework(framework.lower())
        results = evaluate_framework(fw, inp)
    else:
        # Load enabled frameworks from disk if no explicit framework given.
        config_dir = workdir / ".sdd" / "compliance"
        lib = CompliancePolicyLibrary()
        lib.load_enabled(config_dir)
        results = lib.evaluate(inp) if lib.enabled_frameworks else evaluate_all(inp)

    passing = [r for r in results if r.passed]
    failing = [r for r in results if not r.passed]

    _severity_order = {
        PolicySeverity.CRITICAL: 4,
        PolicySeverity.HIGH: 3,
        PolicySeverity.MEDIUM: 2,
        PolicySeverity.LOW: 1,
        PolicySeverity.INFORMATIONAL: 0,
    }
    _fail_threshold = {
        "critical": 4,
        "high": 3,
        "medium": 2,
        "low": 1,
        "none": 99,
    }

    if as_json:
        click.echo(
            json.dumps(
                {
                    "summary": {
                        "total": len(results),
                        "passing": len(passing),
                        "failing": len(failing),
                    },
                    "results": [r.to_dict() for r in results],
                },
                indent=2,
            )
        )
    else:
        click.echo(f"Compliance check — {len(results)} policies evaluated")
        click.echo(f"  Passing: {len(passing)}   Failing: {len(failing)}")
        click.echo("")
        if failing:
            click.echo("FAILURES:")
            for r in sorted(failing, key=lambda x: -_severity_order[x.severity]):
                click.echo(f"  [{r.severity.value.upper():<12}] {r.policy_id:<22} {r.name}")
                click.echo(f"           {r.remediation}")
        else:
            click.echo("All policies passed.")

    # Exit non-zero if any failure meets the severity threshold.
    threshold = _fail_threshold.get(fail_on.lower(), 99)
    if any(_severity_order[r.severity] >= threshold for r in failing):
        sys.exit(1)


@compliance_group.command("rego")
@click.argument("framework", type=click.Choice(_FRAMEWORK_CHOICES, case_sensitive=False))
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(path_type=Path),
    help="Directory to write .rego files (default: .sdd/compliance/rego/<framework>/).",
)
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(path_type=Path),
)
def export_rego(framework: str, output_dir: Path | None, workdir: Path) -> None:
    """Export OPA/Rego rule files for a compliance framework.

    Writes one .rego file per policy under OUTPUT_DIR so the rules can be
    loaded into an OPA server for live evaluation.

    FRAMEWORK is one of: soc2, iso27001, pci_dss, nist_800_53.
    """
    fw = ComplianceFramework(framework.lower())
    dest = output_dir or (workdir / ".sdd" / "compliance" / "rego" / fw.value)
    lib = CompliancePolicyLibrary()
    paths = lib.export_rego(fw, dest_dir=dest)
    click.echo(f"Exported {len(paths)} Rego policies to: {dest}")
