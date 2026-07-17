"""Compliance CLI -- EU AI Act, SOC 2 / ISO 27001 policy snapshots.

EU AI Act mapping: Bernstein implements the record-keeping obligations of
Article 12 of Regulation (EU) 2024/1689 ("automatic recording of events
('logs') over the lifetime of the high-risk AI system"). Per-task risk
class assessments (minimal / limited / high / unacceptable) are stored
under ``.sdd/eu_ai_act/`` and surfaced via ``bernstein compliance eu-ai-act``.

Bundle export lives under ``bernstein audit export --article-12``.
See docs/compliance/ for the regulator-shape walkthrough.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import click

from bernstein.compliance.eu_ai_act import ComplianceEngine, bernstein_descriptor
from bernstein.core.compliance.pack import (
    build_incident_pack,
    build_oversight_pack,
    build_pack,
    build_retention_pack,
)
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
    """Inspect compliance artifacts and policy enforcement.

    Shipped framework policy bundles: SOC 2, ISO 27001, PCI DSS, NIST 800-53.
    EU AI Act Article 12 (Regulation (EU) 2024/1689) record-keeping is
    mapped via ``bernstein audit export --article-12``. Cite: docs/compliance/.
    """


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
    click.echo("  EU AI Act Compliance Assessment: Bernstein")
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
    CompliancePolicyLibrary().disable(fw, config_dir=config_dir)
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
# Runtime snapshot overrides - pass current configuration state:
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
        click.echo(f"Compliance check: {len(results)} policies evaluated")
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
    paths = CompliancePolicyLibrary().export_rego(fw, dest_dir=dest)
    click.echo(f"Exported {len(paths)} Rego policies to: {dest}")


# ---------------------------------------------------------------------------
# `bernstein compliance pack` - regulator-mapped evidence packs
#
# ``pack`` is a group. With no subcommand (or a leading option) it defaults to
# ``article-12`` so the legacy ``bernstein compliance pack --since ...`` call
# keeps working; ``retention`` / ``incident`` / ``oversight`` add the packs
# mapped to Article 12(3), Article 73, and Article 14 respectively. Each pack
# is a deterministic projection of the chain, sealed with the operator key and
# offline-verifiable with ``python -m bernstein_verify pack``.
# ---------------------------------------------------------------------------


def _parse_iso_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise click.BadParameter(f"expected YYYY-MM-DD, got {value!r}") from exc


def _window_dates(since: str, until: str) -> tuple[date, date]:
    since_date = _parse_iso_date(since).date()
    until_date = _parse_iso_date(until).date()
    if since_date > until_date:
        raise click.BadParameter("--since must be <= --until")
    return since_date, until_date


def _require_operator_key(operator_key: Path | None, workdir: Path) -> Path:
    resolved_key = operator_key or (workdir / ".sdd" / "keys" / "operator.key")
    if not resolved_key.exists():
        raise click.ClickException(
            f"Operator signing key not found at {resolved_key}.\n"
            "Generate one with `openssl genpkey -algorithm Ed25519 -out "
            f"{resolved_key}` or pass --operator-key <path>.",
        )
    return resolved_key


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


class _DefaultSubcommandGroup(click.Group):
    """A group that routes to a default subcommand when the first token is not
    a known subcommand, so ``pack --since ...`` still reaches ``article-12``."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._default_cmd = kwargs.pop("default_cmd", None)
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        default = self._default_cmd
        if isinstance(default, str):
            if not args:
                args = [default]
            elif args[0] not in self.commands and args[0] not in ("--help", "-h"):
                args = [default, *args]
        return super().parse_args(ctx, args)


@compliance_group.group("pack", cls=_DefaultSubcommandGroup, default_cmd="article-12")
def pack_group() -> None:
    """Assemble regulator-mapped evidence packs from the audit chain.

    Subcommands (each offline-verifiable with `python -m bernstein_verify pack`):

    \b
      article-12  record-keeping bundle (default; Article 12).
      retention   chain-continuity evidence for a window (Article 12(3)).
      incident    serious-incident report from a run (Article 73).
      oversight   human-oversight evidence from receipts (Article 14).
    """


@pack_group.command("article-12")
@click.option("--since", required=True, help="Window start date (YYYY-MM-DD, inclusive).")
@click.option("--until", required=True, help="Window end date (YYYY-MM-DD, inclusive).")
@click.option("--org", required=True, help="Organisation name (printed on the cover page).")
@click.option("--output", required=True, type=click.Path(path_type=Path), help="Destination .zip path.")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Project root (used to locate .sdd/lineage and .sdd/agents).",
)
@click.option(
    "--lineage-dir",
    default=None,
    type=click.Path(path_type=Path),
    help="Override path to lineage directory (default: <workdir>/.sdd/lineage).",
)
@click.option(
    "--agent-cards-dir",
    default=None,
    type=click.Path(path_type=Path),
    help="Override path to Agent Cards directory (default: <workdir>/.sdd/agents).",
)
@click.option(
    "--operator-key",
    default=None,
    type=click.Path(path_type=Path),
    help=("Path to PEM PKCS#8 Ed25519 operator key for manifest signing (default: <workdir>/.sdd/keys/operator.key)."),
)
def pack_article12(
    since: str,
    until: str,
    org: str,
    output: Path,
    workdir: Path,
    lineage_dir: Path | None,
    agent_cards_dir: Path | None,
    operator_key: Path | None,
) -> None:
    """Build a one-command EU AI Act Article 12 evidence bundle.

    Filters the lineage log to ``[since, until]``, packages the human-
    readable PDF + machine-readable CSV + raw JSONL + per-entry signatures
    + Agent Cards, and emits an operator-signed SLSA-style manifest.
    """
    since_date, until_date = _window_dates(since, until)
    resolved_lineage = lineage_dir or (workdir / ".sdd" / "lineage")
    resolved_cards = agent_cards_dir or (workdir / ".sdd" / "agents")
    resolved_key = _require_operator_key(operator_key, workdir)

    out_path = build_pack(
        since=since_date,
        until=until_date,
        org=org,
        lineage_dir=resolved_lineage,
        agent_cards_dir=resolved_cards,
        output_path=output,
        operator_key_path=resolved_key,
    )
    click.echo(f"Compliance pack written to: {out_path}")


@pack_group.command("retention")
@click.option("--since", required=True, help="Window start date (YYYY-MM-DD, inclusive).")
@click.option("--until", required=True, help="Window end date (YYYY-MM-DD, inclusive).")
@click.option("--org", required=True, help="Organisation name (printed on the cover page).")
@click.option("--output", required=True, type=click.Path(path_type=Path), help="Destination .zip path.")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Project root (used to locate .sdd/lineage and .sdd/agents).",
)
@click.option("--lineage-dir", default=None, type=click.Path(path_type=Path), help="Override lineage directory.")
@click.option(
    "--agent-cards-dir", default=None, type=click.Path(path_type=Path), help="Override Agent Cards directory."
)
@click.option("--operator-key", default=None, type=click.Path(path_type=Path), help="Override operator signing key.")
def pack_retention(
    since: str,
    until: str,
    org: str,
    output: Path,
    workdir: Path,
    lineage_dir: Path | None,
    agent_cards_dir: Path | None,
    operator_key: Path | None,
) -> None:
    """Build a chain-continuity (retention) evidence pack for the window.

    Records the boundary head hashes, entry count, detected coverage gaps,
    and retention parameters, embedding the signed lineage log so an auditor
    recomputes the boundary head hashes from the actual signed entries.
    """
    since_date, until_date = _window_dates(since, until)
    resolved_lineage = lineage_dir or (workdir / ".sdd" / "lineage")
    resolved_cards = agent_cards_dir or (workdir / ".sdd" / "agents")
    resolved_key = _require_operator_key(operator_key, workdir)

    out_path = build_retention_pack(
        since=since_date,
        until=until_date,
        org=org,
        lineage_dir=resolved_lineage,
        agent_cards_dir=resolved_cards,
        output_path=output,
        operator_key_path=resolved_key,
    )
    click.echo(f"Retention pack written to: {out_path}")


@pack_group.command("oversight")
@click.option("--since", required=True, help="Window start date (YYYY-MM-DD, inclusive).")
@click.option("--until", required=True, help="Window end date (YYYY-MM-DD, inclusive).")
@click.option("--org", required=True, help="Organisation name (printed on the cover page).")
@click.option("--output", required=True, type=click.Path(path_type=Path), help="Destination .zip path.")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Project root (used to locate resolved approvals).",
)
@click.option(
    "--approvals",
    default=None,
    type=click.Path(path_type=Path),
    help="JSONL/JSON of resolved approvals (default: <workdir>/.sdd/approvals/resolved.jsonl).",
)
@click.option("--operator-key", default=None, type=click.Path(path_type=Path), help="Override operator signing key.")
def pack_oversight(
    since: str,
    until: str,
    org: str,
    output: Path,
    workdir: Path,
    approvals: Path | None,
    operator_key: Path | None,
) -> None:
    """Build a human-oversight (Article 14) evidence pack from approval receipts.

    Every in-window approval becomes a receipt carrying the attested
    displayed-versus-executed binding, so an auditor recomputes the binding
    offline decision by decision.
    """
    since_date, until_date = _window_dates(since, until)
    resolved_key = _require_operator_key(operator_key, workdir)
    approvals_path = approvals or (workdir / ".sdd" / "approvals" / "resolved.jsonl")

    records: list[dict[str, object]] = []
    if approvals_path.exists():
        if approvals_path.suffix == ".jsonl":
            records = _load_jsonl(approvals_path)
        else:
            data = _load_json(approvals_path)
            if isinstance(data, list):
                records = data
            elif isinstance(data, dict):
                records = list(data.get("approvals", []))

    out_path = build_oversight_pack(
        since=since_date,
        until=until_date,
        org=org,
        approvals=records,
        output_path=output,
        operator_key_path=resolved_key,
    )
    click.echo(f"Oversight pack written to: {out_path} ({len(records)} candidate approvals)")


@pack_group.command("incident")
@click.option("--run", required=True, help="Run identifier of the incident.")
@click.option("--org", required=True, help="Organisation name (printed on the cover page).")
@click.option("--output", required=True, type=click.Path(path_type=Path), help="Destination .zip path.")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Project root (used to locate the incident, evidence, and approval stores).",
)
@click.option(
    "--incident-dir",
    default=None,
    type=click.Path(path_type=Path),
    help="Override incident directory (default: <workdir>/.sdd/incidents/<run>).",
)
@click.option("--operator-key", default=None, type=click.Path(path_type=Path), help="Override operator signing key.")
def pack_incident(
    run: str,
    org: str,
    output: Path,
    workdir: Path,
    incident_dir: Path | None,
    operator_key: Path | None,
) -> None:
    """Build a serious-incident (Article 73) report pack for a run.

    Joins the incident timeline with the prev_hmac-chained audit slice and the
    referenced evidence bundles and approval receipts. A referenced artefact
    missing from the store is recorded as an explicit gap - the pack never
    fabricates completeness.
    """
    resolved_key = _require_operator_key(operator_key, workdir)
    idir = incident_dir or (workdir / ".sdd" / "incidents" / run)

    timeline_path = idir / "timeline.json"
    if timeline_path.exists():
        timeline = _load_json(timeline_path)
        if not isinstance(timeline, dict):
            raise click.ClickException(f"{timeline_path}: expected a JSON object")
    else:
        timeline = {"run_id": run, "events": [], "involved_agents": [], "artifacts": []}

    slice_path = idir / "audit-slice.jsonl"
    audit_events = _load_jsonl(slice_path) if slice_path.exists() else []

    evidence_bundles: dict[str, bytes] = {}
    receipts: dict[str, bytes] = {}
    gaps: list[dict[str, str]] = []
    for ref in timeline.get("evidence_bundle_refs", []):
        candidate = workdir / ".sdd" / "evidence" / f"{ref}.json"
        if candidate.exists():
            evidence_bundles[f"{ref}.json"] = candidate.read_bytes()
        else:
            gaps.append({"kind": "evidence_bundle", "ref": str(ref), "reason": "missing_from_store"})
    for ref in timeline.get("receipt_refs", []):
        candidate = workdir / ".sdd" / "approvals" / f"{ref}.json"
        if candidate.exists():
            receipts[f"{ref}.json"] = candidate.read_bytes()
        else:
            gaps.append({"kind": "receipt", "ref": str(ref), "reason": "missing_from_store"})

    out_path = build_incident_pack(
        run_id=run,
        org=org,
        timeline=timeline,
        audit_events=audit_events,
        evidence_bundles=evidence_bundles,
        receipts=receipts,
        gaps=gaps,
        output_path=output,
        operator_key_path=resolved_key,
    )
    click.echo(f"Incident pack written to: {out_path} ({len(gaps)} evidence gap(s))")
