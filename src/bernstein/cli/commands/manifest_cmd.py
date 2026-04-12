"""CLI commands for inspecting and comparing run manifests."""

from __future__ import annotations

from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console


def _sdd_dir() -> Path:
    return Path.cwd() / ".sdd"


@click.group("manifest")
def manifest_group() -> None:
    """Inspect and compare run manifests.

    \b
      bernstein manifest list           # list all manifests
      bernstein manifest show <run-id>  # display a run's configuration
      bernstein manifest diff <a> <b>   # compare two run configurations
    """


@manifest_group.command("list")
def manifest_list() -> None:
    """List all available run manifests.

    \b
      bernstein manifest list
    """
    from bernstein.core.manifest import list_manifests

    sdd = _sdd_dir()
    runs = list_manifests(sdd)
    if not runs:
        console.print("[yellow]No manifests found.[/yellow] Run [bold]bernstein[/bold] to generate one.")
        return

    table = Table(title="Run Manifests", show_header=True, header_style="bold cyan")
    table.add_column("Run ID", style="bold")
    table.add_column("Hash (first 12)")

    from bernstein.core.manifest import load_manifest

    for run_id in runs:
        m = load_manifest(sdd, run_id)
        h = m.manifest_hash[:12] if m else "?"
        table.add_row(run_id, h)

    console.print(table)


@manifest_group.command("show")
@click.argument("run_id")
def manifest_show(run_id: str) -> None:
    """Display the manifest for a past run.

    \b
      bernstein manifest show 20240315-143022
    """
    from bernstein.core.manifest import load_manifest

    sdd = _sdd_dir()
    m = load_manifest(sdd, run_id)
    if m is None:
        console.print(f"[red]Manifest not found for run [bold]{run_id}[/bold].[/red]")
        raise SystemExit(1)

    # Header
    console.print(
        Panel(
            f"[bold]Run ID:[/bold] {m.run_id}\n[bold]Manifest Hash:[/bold] {m.manifest_hash}",
            title="Run Manifest",
            border_style="cyan",
        )
    )

    # Provenance
    prov_table = Table(title="Provenance", show_header=False, border_style="dim")
    prov_table.add_column("Field", style="bold")
    prov_table.add_column("Value")
    prov_table.add_row("Triggered by", m.provenance.triggered_by)
    prov_table.add_row("Triggered at", m.provenance.triggered_at_iso)
    prov_table.add_row("Commit SHA", m.provenance.commit_sha or "(unknown)")
    console.print(prov_table)

    # Workflow
    if m.workflow_name:
        wf_table = Table(title="Workflow", show_header=False, border_style="dim")
        wf_table.add_column("Field", style="bold")
        wf_table.add_column("Value")
        wf_table.add_row("Name", m.workflow_name)
        wf_table.add_row("Definition Hash", m.workflow_definition_hash[:16] + "...")
        console.print(wf_table)

    # Agent Adapter
    aa_table = Table(title="Agent Adapter", show_header=False, border_style="dim")
    aa_table.add_column("Field", style="bold")
    aa_table.add_column("Value")
    aa_table.add_row("CLI", m.agent_adapter.cli)
    aa_table.add_row("Model", m.agent_adapter.model or "(default)")
    aa_table.add_row("Max Agents", str(m.agent_adapter.max_agents))
    aa_table.add_row("Tasks/Agent", str(m.agent_adapter.max_tasks_per_agent))
    console.print(aa_table)

    # Budget & Approval
    ba_table = Table(title="Budget & Approval", show_header=False, border_style="dim")
    ba_table.add_column("Field", style="bold")
    ba_table.add_column("Value")
    budget_label = f"${m.budget_ceiling_usd:.2f}" if m.budget_ceiling_usd > 0 else "unlimited"
    ba_table.add_row("Budget Ceiling", budget_label)
    ba_table.add_row("Approval Mode", m.approval_gates.mode)
    ba_table.add_row("Plan Mode", str(m.approval_gates.plan_mode))
    console.print(ba_table)

    # Model Routing
    mr_table = Table(title="Model Routing", show_header=False, border_style="dim")
    mr_table.add_column("Field", style="bold")
    mr_table.add_column("Value")
    mr_table.add_row("Default Model", m.model_routing.default_model or "(auto)")
    if m.model_routing.allowed_providers:
        mr_table.add_row("Allowed Providers", ", ".join(m.model_routing.allowed_providers))
    if m.model_routing.denied_providers:
        mr_table.add_row("Denied Providers", ", ".join(m.model_routing.denied_providers))
    console.print(mr_table)

    # Orchestrator Config
    if m.orchestrator_config:
        oc_table = Table(title="Orchestrator Config", show_header=True, header_style="bold")
        oc_table.add_column("Key", style="bold")
        oc_table.add_column("Value")
        for k, v in sorted(m.orchestrator_config.items()):
            oc_table.add_row(k, str(v))
        console.print(oc_table)


@manifest_group.command("diff")
@click.argument("run_a")
@click.argument("run_b")
def manifest_diff(run_a: str, run_b: str) -> None:
    """Compare two run configurations and highlight differences.

    \b
      bernstein manifest diff 20240315-143022 20240316-091500
    """
    from bernstein.core.manifest import diff_manifests, load_manifest

    sdd = _sdd_dir()
    ma = load_manifest(sdd, run_a)
    mb = load_manifest(sdd, run_b)

    if ma is None:
        console.print(f"[red]Manifest not found for run [bold]{run_a}[/bold].[/red]")
        raise SystemExit(1)
    if mb is None:
        console.print(f"[red]Manifest not found for run [bold]{run_b}[/bold].[/red]")
        raise SystemExit(1)

    diffs = diff_manifests(ma, mb)

    if not diffs:
        console.print(
            f"[green]Runs [bold]{run_a}[/bold] and [bold]{run_b}[/bold] have identical configurations.[/green]"
        )
        return

    console.print(
        Panel(
            f"[bold]{run_a}[/bold] (hash: {ma.manifest_hash[:12]})\n"
            f"[bold]{run_b}[/bold] (hash: {mb.manifest_hash[:12]})",
            title="Manifest Diff",
            border_style="yellow",
        )
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Field", style="bold")
    table.add_column(f"{run_a}", style="red")
    table.add_column(f"{run_b}", style="green")

    for field_name, (va, vb) in sorted(diffs.items()):
        table.add_row(field_name, _format_value(va), _format_value(vb))

    console.print(table)
    console.print(f"\n[yellow]{len(diffs)} field(s) changed.[/yellow]")


def _format_value(v: object) -> str:
    """Format a value for diff display."""
    import json

    if isinstance(v, dict):
        return json.dumps(v, indent=2, sort_keys=True)
    return str(v)
