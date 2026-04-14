"""CLI commands for prompt versioning and A/B testing.

Usage::

    bernstein prompts list                     # list all versioned prompts
    bernstein prompts show plan                # show versions for 'plan' prompt
    bernstein prompts compare plan 1 2         # compare v1 vs v2 metrics
    bernstein prompts promote plan 2           # manually promote v2 to active
    bernstein prompts ab-start plan 1 2        # start A/B test between v1 and v2
    bernstein prompts ab-stop plan             # stop A/B test
    bernstein prompts seed                     # seed .sdd/prompts/ from templates/
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console


def _sdd_dir() -> Path:
    return Path.cwd() / ".sdd"


def _templates_dir() -> Path:
    from bernstein import get_templates_dir

    return get_templates_dir(Path.cwd())


@click.group("prompts")
def prompts_group() -> None:
    """Manage versioned prompts and A/B tests.

    \b
      bernstein prompts list                  # list all prompts
      bernstein prompts show <name>           # show versions
      bernstein prompts compare <name> v1 v2  # compare metrics
      bernstein prompts promote <name> <ver>  # promote version
      bernstein prompts ab-start <name> a b   # start A/B test
      bernstein prompts ab-stop <name>        # stop A/B test
    """


@prompts_group.command("list")
def prompts_list() -> None:
    """List all versioned prompts and their active versions."""
    from bernstein.core.prompt_versioning import PromptRegistry

    registry = PromptRegistry(_sdd_dir())
    names = registry.list_prompts()

    if not names:
        console.print("[yellow]No versioned prompts found.[/yellow]")
        console.print("Run [bold]bernstein prompts seed[/bold] to initialize from templates.")
        return

    table = Table(title="Versioned Prompts", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Active", justify="center")
    table.add_column("Versions", justify="center")
    table.add_column("A/B Test", justify="center")
    table.add_column("Total Obs.", justify="right")

    for name in names:
        meta = registry.get_meta(name)
        if meta is None:
            continue
        total_obs = sum(v.get("metrics", {}).get("observations", 0) for v in meta.versions.values())
        ab_status = (
            f"v{meta.ab_versions[0]} vs v{meta.ab_versions[1]}"
            if meta.ab_enabled and len(meta.ab_versions) == 2
            else "[dim]off[/dim]"
        )
        table.add_row(
            name,
            f"v{meta.active_version}",
            str(len(meta.versions)),
            ab_status,
            str(total_obs),
        )

    console.print(table)


def _format_version_status(ver_num: int, meta: Any) -> str:
    """Format the status label for a prompt version."""
    parts: list[str] = []
    if ver_num == meta.active_version:
        parts.append("[green]active[/green]")
    if meta.ab_enabled and ver_num in meta.ab_versions:
        parts.append("[yellow]A/B[/yellow]")
    return " ".join(parts) if parts else "[dim]-[/dim]"


@prompts_group.command("show")
@click.argument("name")
def prompts_show(name: str) -> None:
    """Show all versions of a prompt with their metrics.

    \b
      bernstein prompts show plan
    """
    from bernstein.core.prompt_versioning import PromptRegistry, VersionMetrics

    registry = PromptRegistry(_sdd_dir())
    meta = registry.get_meta(name)

    if meta is None:
        console.print(f"[red]Prompt [bold]{name}[/bold] not found.[/red]")
        raise SystemExit(1)

    ab_info = ""
    if meta.ab_enabled and len(meta.ab_versions) == 2:
        va, vb = meta.ab_versions
        ab_info = f"  A/B: v{va} vs v{vb} ({meta.ab_traffic_split:.0%} to v{vb})"

    console.print(
        Panel(
            f"[bold]Prompt:[/bold] {name}\n"
            f"[bold]Active:[/bold] v{meta.active_version}\n"
            f"[bold]Versions:[/bold] {len(meta.versions)}" + ab_info,
            title=f"Prompt: {name}",
            border_style="cyan",
        )
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Ver", style="bold", justify="center")
    table.add_column("Status", justify="center")
    table.add_column("Obs.", justify="right")
    table.add_column("Success%", justify="right")
    table.add_column("Avg Quality", justify="right")
    table.add_column("Avg Cost", justify="right")
    table.add_column("Avg Latency", justify="right")
    table.add_column("Description")

    for ver_num in sorted(meta.versions.keys()):
        ver_dict = meta.versions[ver_num]
        metrics = VersionMetrics.from_dict(ver_dict.get("metrics", {}))

        status = _format_version_status(ver_num, meta)
        has_obs = bool(metrics.observations)
        table.add_row(
            f"v{ver_num}",
            status,
            str(metrics.observations),
            f"{metrics.success_rate:.1%}" if has_obs else "-",
            f"{metrics.avg_quality:.3f}" if has_obs else "-",
            f"${metrics.avg_cost:.4f}" if has_obs else "-",
            f"{metrics.avg_latency:.0f}s" if has_obs else "-",
            ver_dict.get("description", ""),
        )

    console.print(table)


@prompts_group.command("compare")
@click.argument("name")
@click.argument("v1", type=int)
@click.argument("v2", type=int)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def prompts_compare(name: str, v1: int, v2: int, as_json: bool) -> None:
    """Compare metrics between two prompt versions.

    \b
      bernstein prompts compare plan 1 2
    """
    import json

    from bernstein.core.prompt_versioning import PromptRegistry

    registry = PromptRegistry(_sdd_dir())
    result = registry.compare_versions(name, v1, v2)

    if result is None:
        console.print(f"[red]Cannot compare: prompt [bold]{name}[/bold] v{v1}/v{v2} not found.[/red]")
        raise SystemExit(1)

    if as_json:
        console.print(json.dumps(result, indent=2))
        return

    console.print(
        Panel(
            f"[bold]{name}[/bold]: v{v1} vs v{v2}\n"
            f"[bold]Winner:[/bold] {result['winner']}\n"
            f"[bold]A/B active:[/bold] {'yes' if result['ab_active'] else 'no'}\n"
            f"[bold]Active version:[/bold] v{result['active_version']}",
            title="Prompt Comparison",
            border_style="cyan",
        )
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric", style="bold")
    table.add_column(f"v{v1}", justify="right")
    table.add_column(f"v{v2}", justify="right")
    table.add_column("Delta", justify="right")

    d1, d2 = result["v1"], result["v2"]

    def _delta(a: float, b: float, fmt: str = ".4f", pct: bool = False) -> str:
        diff = b - a
        sign = "+" if diff >= 0 else ""
        if diff > 0:
            color = "green"
        elif diff < 0:
            color = "red"
        else:
            color = "dim"
        if pct:
            return f"[{color}]{sign}{diff:.1%}[/{color}]"
        return f"[{color}]{sign}{diff:{fmt}}[/{color}]"

    table.add_row(
        "Observations",
        str(d1["observations"]),
        str(d2["observations"]),
        str(d2["observations"] - d1["observations"]),
    )
    table.add_row(
        "Success Rate",
        f"{d1['success_rate']:.1%}",
        f"{d2['success_rate']:.1%}",
        _delta(d1["success_rate"], d2["success_rate"], pct=True),
    )
    table.add_row(
        "Avg Quality",
        f"{d1['avg_quality']:.3f}",
        f"{d2['avg_quality']:.3f}",
        _delta(d1["avg_quality"], d2["avg_quality"], fmt=".3f"),
    )
    table.add_row(
        "Avg Cost",
        f"${d1['avg_cost']:.4f}",
        f"${d2['avg_cost']:.4f}",
        _delta(d1["avg_cost"], d2["avg_cost"], fmt=".4f"),
    )
    table.add_row(
        "Avg Latency",
        f"{d1['avg_latency']:.0f}s",
        f"{d2['avg_latency']:.0f}s",
        _delta(d1["avg_latency"], d2["avg_latency"], fmt=".0f"),
    )

    console.print(table)


@prompts_group.command("promote")
@click.argument("name")
@click.argument("version", type=int)
def prompts_promote(name: str, version: int) -> None:
    """Promote a specific version to active.

    \b
      bernstein prompts promote plan 2
    """
    from bernstein.core.prompt_versioning import PromptRegistry

    registry = PromptRegistry(_sdd_dir())
    if registry.promote_version(name, version):
        console.print(f"[green]Promoted [bold]{name}[/bold] v{version} to active.[/green]")
    else:
        console.print(f"[red]Failed: prompt [bold]{name}[/bold] v{version} not found.[/red]")
        raise SystemExit(1)


@prompts_group.command("ab-start")
@click.argument("name")
@click.argument("version_a", type=int)
@click.argument("version_b", type=int)
@click.option("--split", default=0.5, type=float, help="Traffic fraction for version B (default 0.5).")
def prompts_ab_start(name: str, version_a: int, version_b: int, split: float) -> None:
    """Start an A/B test between two prompt versions.

    \b
      bernstein prompts ab-start plan 1 2
      bernstein prompts ab-start plan 1 2 --split 0.3
    """
    from bernstein.core.prompt_versioning import PromptRegistry

    registry = PromptRegistry(_sdd_dir())
    if registry.start_ab_test(name, version_a, version_b, traffic_split=split):
        console.print(
            f"[green]A/B test started:[/green] [bold]{name}[/bold] "
            f"v{version_a} vs v{version_b} ({split:.0%} traffic to v{version_b})"
        )
    else:
        console.print(f"[red]Failed: check that prompt [bold]{name}[/bold] v{version_a}/v{version_b} exist.[/red]")
        raise SystemExit(1)


@prompts_group.command("ab-stop")
@click.argument("name")
def prompts_ab_stop(name: str) -> None:
    """Stop an active A/B test without promoting either version.

    \b
      bernstein prompts ab-stop plan
    """
    from bernstein.core.prompt_versioning import PromptRegistry

    registry = PromptRegistry(_sdd_dir())
    if registry.stop_ab_test(name):
        console.print(f"[green]A/B test stopped for [bold]{name}[/bold].[/green]")
    else:
        console.print(f"[yellow]No active A/B test for [bold]{name}[/bold].[/yellow]")


@prompts_group.command("seed")
def prompts_seed() -> None:
    """Seed .sdd/prompts/ from templates/prompts/ as v1.

    \b
      bernstein prompts seed
    """
    from bernstein.core.prompt_versioning import seed_prompts_from_templates

    count = seed_prompts_from_templates(_sdd_dir(), _templates_dir())
    if count:
        console.print(f"[green]Seeded {count} prompt(s) as v1.[/green]")
    else:
        console.print("[dim]All prompts already seeded.[/dim]")
