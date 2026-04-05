"""Bernstein token-report — prompt token usage analysis."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from bernstein.cli.helpers import is_json, print_json

console = Console()


@click.command("token-report")
@click.option(
    "--metrics-dir",
    default=".sdd/metrics",
    show_default=True,
    help="Directory containing metrics JSONL files.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.option("--markdown", "as_markdown", is_flag=True, default=False, help="Output markdown report.")
def token_report_cmd(metrics_dir: str, as_json: bool, as_markdown: bool) -> None:
    """Analyze prompt token distribution and suggest reductions."""
    from bernstein.core.token_analyzer import TokenUsageAnalyzer, to_markdown

    mdir = Path(metrics_dir)
    if not mdir.exists():
        if as_json or is_json():
            print_json({"error": f"Metrics directory not found: {mdir}"})
        else:
            console.print(f"[red]Metrics directory not found:[/red] {mdir}")
        raise SystemExit(1)

    workdir = mdir.parent.parent  # .sdd/metrics -> .sdd -> workdir
    analyzer = TokenUsageAnalyzer(workdir)
    analysis = analyzer.analyze()

    if not analysis.task_stats:
        if as_json or is_json():
            print_json({"rows": [], "totals": {}})
        else:
            console.print("[dim]No task metrics found.[/dim]")
        return

    # JSON output.
    if as_json or is_json():
        print_json(
            {
                "total_tokens_prompt": analysis.total_tokens_prompt,
                "total_tokens_completion": analysis.total_tokens_completion,
                "total_cost_usd": round(analysis.total_cost_usd, 6),
                "overall_io_ratio": round(analysis.overall_io_ratio, 2),
                "task_count": len(analysis.task_stats),
                "waste_patterns": [
                    {
                        "task_id": wp.task_id,
                        "title": wp.title,
                        "pattern": wp.pattern,
                        "detail": wp.detail,
                    }
                    for wp in analysis.waste_patterns
                ],
                "model_spend": [
                    {
                        "model": ms.model,
                        "task_count": ms.task_count,
                        "total_tokens_prompt": ms.total_tokens_prompt,
                        "total_tokens_completion": ms.total_tokens_completion,
                        "total_cost_usd": round(ms.total_cost_usd, 6),
                    }
                    for ms in analysis.model_spend
                ],
                "top_5_hungry": [
                    {
                        "task_id": ts.task_id,
                        "title": ts.title,
                        "model": ts.model,
                        "tokens_prompt": ts.tokens_prompt,
                        "tokens_completion": ts.tokens_completion,
                        "io_ratio": round(ts.io_ratio, 2),
                        "cost_usd": round(ts.cost_usd, 6),
                    }
                    for ts in analysis.top_5_hungry
                ],
            }
        )
        return

    # Markdown output.
    if as_markdown:
        console.print(to_markdown(analysis))
        return

    # Rich table output (default).
    from rich.panel import Panel
    from rich.table import Table

    # Summary panel.
    eff_color = "green" if analysis.overall_io_ratio <= 3.0 else "yellow"
    console.print(
        Panel(
            f"[bold]Total input tokens:[/bold]  {analysis.total_tokens_prompt:,}\n"
            f"[bold]Total output tokens:[/bold] {analysis.total_tokens_completion:,}\n"
            f"[bold]I/O ratio:[/bold]           [{eff_color}]{analysis.overall_io_ratio:.1f}:1[/{eff_color}]"
            f" (target < 3:1)\n"
            f"[bold]Total cost:[/bold]           [green]${analysis.total_cost_usd:.4f}[/green]",
            title="[bold cyan]Token Usage Summary[/bold cyan]",
            border_style="cyan",
            expand=False,
        )
    )

    # Top 5 hungry tasks.
    if analysis.top_5_hungry:
        table = Table(
            title="Top 5 Token-Hungry Tasks",
            header_style="bold cyan",
            show_lines=False,
        )
        table.add_column("Task", min_width=30)
        table.add_column("Model", min_width=10)
        table.add_column("Input", justify="right", min_width=10)
        table.add_column("Output", justify="right", min_width=10)
        table.add_column("Ratio", justify="right", min_width=8)
        table.add_column("Cost", justify="right", min_width=8)

        for ts in analysis.top_5_hungry:
            short = ts.title[:40] + ("..." if len(ts.title) > 40 else "")
            ratio_style = "red" if ts.io_ratio >= 10.0 else ("yellow" if ts.io_ratio >= 3.0 else "green")
            table.add_row(
                short,
                ts.model,
                f"{ts.tokens_prompt:,}",
                f"{ts.tokens_completion:,}",
                f"[{ratio_style}]{ts.io_ratio:.1f}:1[/{ratio_style}]",
                f"${ts.cost_usd:.4f}",
            )
        console.print(table)

    # Model spend.
    if analysis.model_spend:
        mtable = Table(
            title="Spend by Model",
            header_style="bold cyan",
            show_lines=False,
        )
        mtable.add_column("Model", min_width=15)
        mtable.add_column("Tasks", justify="right", min_width=6)
        mtable.add_column("Input", justify="right", min_width=10)
        mtable.add_column("Output", justify="right", min_width=10)
        mtable.add_column("Cost", justify="right", min_width=8)

        for ms in analysis.model_spend:
            mtable.add_row(
                ms.model,
                str(ms.task_count),
                f"{ms.total_tokens_prompt:,}",
                f"{ms.total_tokens_completion:,}",
                f"${ms.total_cost_usd:.4f}",
            )
        console.print(mtable)

    # Suggestions.
    if analysis.waste_patterns:
        console.print("\n[bold yellow]Suggestions[/bold yellow]")
        for wp in analysis.waste_patterns:
            short = wp.title[:50] + ("..." if len(wp.title) > 50 else "")
            console.print(f"  [yellow]*[/yellow] [bold]{short}[/bold]: {wp.detail}")
        console.print()
