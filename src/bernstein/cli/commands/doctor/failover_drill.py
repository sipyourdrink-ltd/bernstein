"""``bernstein doctor --failover-drill`` -- exercise declared fallback chains.

Reads the ``provider_availability`` section of ``bernstein.yaml`` in the
current directory, probes every declared chain element, and evaluates each
chain position as the dispatch target under a simulated outage of its
predecessors. Exits non-zero when any declared chain element is broken, so
operators find dead chains before an outage does.

Each drill row carries the deterministic decision hash for its simulated
outage prefix -- the exact hash a real outage's dispatch routing receipt
would record -- and the drill outcome per role is mirrored into the
HMAC-chained audit log when a ``.sdd`` workspace is present.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.routing.provider_availability import (
    AvailabilityPolicyError,
    DrillReport,
    ProviderAvailabilityConfig,
    binary_path_probe,
    decide_route,
    parse_provider_availability,
    run_failover_drill,
)

if TYPE_CHECKING:
    from pathlib import Path


def _load_availability_config(workdir: Path) -> ProviderAvailabilityConfig | None:
    """Load and validate the provider_availability section of bernstein.yaml.

    Returns None when the config file or the section is absent.

    Raises:
        AvailabilityPolicyError: When the section is present but invalid
            (including a chain element below its role's conformance floor).
    """
    import yaml

    config_path = workdir / "bernstein.yaml"
    if not config_path.exists():
        return None
    with config_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        return None
    section = data.get("provider_availability")
    if section is None:
        return None
    return parse_provider_availability(section)


def _record_drill_receipts(workdir: Path, config: ProviderAvailabilityConfig) -> None:
    """Best-effort: mirror the drill-time routing decision per role into the audit chain."""
    sdd_dir = workdir / ".sdd"
    if not sdd_dir.exists():
        return
    try:
        from bernstein.core.security.audit_chain import (
            AuditChainStore,
            record_routing_failover_receipt,
        )

        chain = AuditChainStore(sdd_dir / "audit")
        for role in sorted(config.policies):
            policy = config.policies[role]
            probes = tuple(binary_path_probe(element) for element in policy.chain)
            decision = decide_route(policy, probes)
            record_routing_failover_receipt(
                chain=chain,
                role=role,
                task_id="",
                decision_hash=decision.decision_hash,
                chosen_index=decision.chosen_index,
                reason=decision.reason,
                chain_considered=[element.to_dict() for element in decision.chain],
                probe_results=[probe.to_dict() for probe in decision.probes],
                kind="drill",
                actor="doctor.failover_drill",
            )
    except Exception:  # the receipt must never mask the drill verdict
        return


def _render_report(report: DrillReport, *, as_json: bool) -> None:
    from bernstein.cli.helpers import console

    if as_json:
        # click.echo keeps stdout parseable for CI pipelines.
        import click

        click.echo(json.dumps(report.to_dict(), indent=2))
        return

    from rich.table import Table

    table = Table(title="Provider failover drill", show_lines=False)
    table.add_column("Role", style="cyan", no_wrap=True)
    table.add_column("Pos", justify="right")
    table.add_column("Adapter")
    table.add_column("Model")
    table.add_column("Health")
    table.add_column("Decision", overflow="fold")
    for row in report.elements:
        health = "[green]healthy[/green]" if row.healthy else f"[red]BROKEN[/red] ({row.detail})"
        table.add_row(row.role, str(row.position), row.adapter, row.model, health, row.decision_hash)
    console.print(table)
    if report.ok:
        console.print("[green]All declared fallback chains are healthy.[/green]")
    else:
        console.print(f"[red]Broken chains for roles: {', '.join(report.broken_roles)}[/red]")


def run_failover_drill_cli(*, workdir: Path, as_json: bool) -> int:
    """Run the failover drill against the workspace config; return the exit code."""
    from bernstein.cli.helpers import console

    try:
        config = _load_availability_config(workdir)
    except AvailabilityPolicyError as exc:
        if as_json:
            import click

            click.echo(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            console.print(f"[red]provider_availability config invalid:[/red] {exc}")
        return 1

    if config is None or not config.policies:
        if as_json:
            import click

            click.echo(json.dumps({"ok": True, "elements": [], "broken_roles": []}, indent=2))
        else:
            console.print(
                "No fallback chains declared: add a provider_availability section to bernstein.yaml to drill."
            )
        return 0

    report = run_failover_drill(config, prober=binary_path_probe)
    _record_drill_receipts(workdir, config)
    _render_report(report, as_json=as_json)
    return 0 if report.ok else 1
