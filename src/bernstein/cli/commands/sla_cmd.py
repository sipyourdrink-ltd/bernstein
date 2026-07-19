"""CLI commands for per-goal SLA contracts (#2549).

Commands:

- ``bernstein sla add --subject-type <t> --subject <id> [axes...]``
- ``bernstein sla list [--json]``
- ``bernstein sla show <id> [--json]``
- ``bernstein sla verify <receipt.json>``
- ``bernstein sla report <id> [--json]``

The CLI is a thin shell around
:class:`bernstein.core.planning.sla_store.SLAStore`, the pure evaluators in
:mod:`bernstein.core.observability.sla_eval`, and the signed receipt in
:mod:`bernstein.core.orchestration.sla_receipt`. ``verify`` reads nothing but
the receipt file itself: it recomputes the contract hash, re-derives every axis
verdict and the remediation from the embedded evidence, checks the chain-slice
linkage, and checks the Ed25519 signature - so a receipt handed to a compliance
reviewer proves the breach offline, byte for byte.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any

import click


def _sdd_dir() -> Path:
    sdd = Path.cwd() / ".sdd"
    if not sdd.exists():
        click.echo("error: no .sdd/ directory found. Run 'bernstein init' first.", err=True)
        raise SystemExit(1)
    return sdd


@click.group("sla")
def sla_group() -> None:
    """Manage per-goal SLA contracts and their signed violation receipts (#2549)."""


@sla_group.command("add")
@click.option(
    "--subject-type",
    type=click.Choice(["schedule", "task_family", "envelope"], case_sensitive=False),
    required=True,
    help="What the contract binds to.",
)
@click.option("--subject", "subject_id", required=True, help="Schedule id, task family, or envelope name.")
@click.option("--max-run-duration", "max_run_duration_s", type=int, default=0, help="Max run duration in seconds.")
@click.option("--start-lateness", "start_lateness_s", type=int, default=0, help="Max start lateness in seconds.")
@click.option("--fire-frequency", "fire_frequency_s", type=int, default=0, help="Max gap between fires in seconds.")
@click.option("--artifact-freshness", "artifact_freshness_s", type=int, default=0, help="Max artifact age in seconds.")
@click.option("--artifact-path", default="", help="Repo-relative path of the maintained artifact (freshness axis).")
@click.option("--spend-rate", "spend_rate_usd_per_hour", type=float, default=0.0, help="Max spend rate in USD/hour.")
@click.option(
    "--budget-events",
    type=int,
    default=3,
    show_default=True,
    help="Allowed failures before budget depletion.",
)
@click.option(
    "--remediation-cost",
    "remediation_cost_usd",
    type=float,
    default=0.0,
    help="Projected extra spend a model-upgrade remediation weighs against the budget gate.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
def sla_add(
    subject_type: str,
    subject_id: str,
    max_run_duration_s: int,
    start_lateness_s: int,
    fire_frequency_s: int,
    artifact_freshness_s: int,
    artifact_path: str,
    spend_rate_usd_per_hour: float,
    budget_events: int,
    remediation_cost_usd: float,
    as_json: bool,
) -> None:
    """Register a per-goal SLA contract (content-addressed, idempotent)."""
    from bernstein.core.planning.sla_store import SLAContractError, SLAStore, build_contract

    sdd = _sdd_dir()
    try:
        contract = build_contract(
            subject_type=subject_type.lower(),
            subject_id=subject_id,
            max_run_duration_s=max_run_duration_s,
            start_lateness_s=start_lateness_s,
            fire_frequency_s=fire_frequency_s,
            artifact_freshness_s=artifact_freshness_s,
            artifact_path=artifact_path,
            spend_rate_usd_per_hour=spend_rate_usd_per_hour,
            budget_events=budget_events,
            remediation_cost_usd=remediation_cost_usd,
        )
    except SLAContractError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(2) from exc

    stored = SLAStore(sdd).add(contract)
    if as_json:
        click.echo(_json.dumps(stored.to_dict(), sort_keys=True, indent=2))
        return
    click.echo(f"Registered SLA contract {stored.id}")
    click.echo(f"  contract_hash: {stored.contract_hash}")
    click.echo(f"  subject:       {stored.subject_type}:{stored.subject_id}")
    click.echo(f"  axes:          {', '.join(stored.declared_axes())}")


@sla_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
def sla_list(as_json: bool) -> None:
    """List all registered SLA contracts."""
    from bernstein.core.planning.sla_store import SLAStore

    contracts = SLAStore(_sdd_dir()).list()
    if as_json:
        click.echo(_json.dumps({"contracts": [c.to_dict() for c in contracts]}, sort_keys=True, indent=2))
        return
    if not contracts:
        click.echo("(no SLA contracts registered)")
        return
    click.echo(f"{'ID':<20} {'SUBJECT':<28} AXES")
    for c in contracts:
        subject = f"{c.subject_type}:{c.subject_id}"
        click.echo(f"{c.id:<20} {subject[:28]:<28} {', '.join(c.declared_axes())}")


@sla_group.command("show")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
def sla_show(contract_id: str, as_json: bool) -> None:
    """Show one SLA contract's full record."""
    from bernstein.core.planning.sla_store import SLAContractError, SLAStore

    try:
        contract = SLAStore(_sdd_dir()).get(contract_id)
    except SLAContractError:
        contract = None
    if contract is None:
        click.echo(f"error: SLA contract {contract_id!r} not found", err=True)
        raise SystemExit(1)
    click.echo(_json.dumps(contract.to_dict(), sort_keys=True, indent=2) if as_json else _render_contract(contract))


def _render_contract(contract: Any) -> str:
    lines = [
        f"id:             {contract.id}",
        f"contract_hash:  {contract.contract_hash}",
        f"subject:        {contract.subject_type}:{contract.subject_id}",
        f"budget_events:  {contract.budget_events}",
    ]
    if contract.max_run_duration_s:
        lines.append(f"max_run_duration: {contract.max_run_duration_s}s")
    if contract.start_lateness_s:
        lines.append(f"start_lateness:   {contract.start_lateness_s}s")
    if contract.fire_frequency_s:
        lines.append(f"fire_frequency:   {contract.fire_frequency_s}s")
    if contract.artifact_freshness_s:
        lines.append(f"artifact_freshness: {contract.artifact_freshness_s}s ({contract.artifact_path})")
    if contract.spend_rate_usd_per_hour:
        lines.append(f"spend_rate:       ${contract.spend_rate_usd_per_hour}/h")
    return "\n".join(lines)


@sla_group.command("verify")
@click.argument("receipt_file", type=click.Path(dir_okay=False))
def sla_verify(receipt_file: str) -> None:
    """Verify a signed violation receipt offline (nothing but the file is read).

    Recomputes the contract hash, re-derives every axis verdict and the
    remediation from the embedded evidence, checks the chain-slice linkage, and
    checks the Ed25519 signature. Exit codes: 0 = verified, 1 = no receipt,
    2 = mismatch (tamper).
    """
    from bernstein.core.orchestration.sla_receipt import read_receipt_file, verify_receipt

    receipt = read_receipt_file(Path(receipt_file))
    if receipt is None:
        click.echo(f"NO RECEIPT -- could not read {receipt_file}", err=True)
        raise SystemExit(1)
    result = verify_receipt(receipt)
    if result.ok:
        click.echo(f"OK -- receipt {receipt.receipt_id} verifies offline (contract {receipt.contract_hash[:16]}...)")
        raise SystemExit(0)
    click.echo(f"MISMATCH -- receipt {receipt.receipt_id} failed verification:", err=True)
    for err in result.errors:
        click.echo(f"  - {err}", err=True)
    raise SystemExit(2)


@sla_group.command("report")
@click.argument("contract_id")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON output.")
def sla_report(contract_id: str, as_json: bool) -> None:
    """Project the deterministic error-budget report over the work-ledger segment.

    Two independent checkouts holding the same ledger segment produce
    byte-identical output: remaining budget, burn rate, and escalation tier are a
    pure projection, so operator and stakeholder derive the same numbers.
    """
    from bernstein.core.orchestration.sla_monitor import build_report
    from bernstein.core.planning.sla_store import SLAContractError, SLAStore

    sdd = _sdd_dir()
    try:
        contract = SLAStore(sdd).get(contract_id)
    except SLAContractError:
        contract = None
    if contract is None:
        click.echo(f"error: SLA contract {contract_id!r} not found", err=True)
        raise SystemExit(1)
    report = build_report(sdd, contract)
    if as_json:
        click.echo(_json.dumps(report, sort_keys=True, indent=2))
        return
    eb = report["error_budget"]
    click.echo(f"contract:        {contract.id}")
    click.echo(f"subject:         {contract.subject_type}:{contract.subject_id}")
    click.echo(f"budget events:   {eb['budget_remaining']} / {eb['budget_total']} remaining")
    click.echo(f"failed / total:  {eb['failed_events']} / {eb['total_events']}")
    click.echo(f"burn rate:       {eb['burn_rate']}x")
    click.echo(f"escalation tier: {eb['escalation_tier']}")
    click.echo(f"segment head:    {eb['segment_head']}")


__all__ = ["sla_group"]
