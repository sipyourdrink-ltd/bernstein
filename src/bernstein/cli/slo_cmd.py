"""CLI command: ``bernstein slo`` — SLO burn-down rate dashboard.

Displays the current SLO compliance, error budget consumption, burn rate,
and a linear projection of when the SLO will be breached — the error-budget
concept from SRE applied to agent orchestration.

Data source:
- Queries the task server at ``GET /slo/burndown`` when the server is running.
- Falls back to reading ``.sdd/metrics/slos.json`` when offline.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

import click

from bernstein.cli.helpers import console, server_get


def _render_burndown(data: dict[str, Any], *, compact: bool = False) -> None:
    """Render a burn-down dashboard dict to the console using Rich."""
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    slo_target = float(data.get("slo_target", 0.9))
    slo_current = float(data.get("slo_current", 0.0))
    burn_rate = float(data.get("burn_rate", 0.0))
    budget_fraction = float(data.get("budget_fraction", 1.0))
    budget_consumed_pct = float(data.get("budget_consumed_pct", 0.0))
    days_to_breach = data.get("days_to_breach")
    breach_projection = str(data.get("breach_projection", ""))
    status = str(data.get("status", "green"))
    total_tasks = int(data.get("total_tasks", 0))
    failed_tasks = int(data.get("failed_tasks", 0))
    sparkline: list[dict[str, Any]] = list(data.get("sparkline") or [])

    # --- Status colour ---
    status_colors = {"green": "green", "yellow": "yellow", "red": "red bold"}
    status_style = status_colors.get(status, "white")
    status_icon = {"green": "●", "yellow": "◑", "red": "●"}.get(status, "○")

    # --- Main stats table ---
    table = Table(show_header=False, box=None, padding=(0, 2), expand=False)
    table.add_column("Metric", style="bold", no_wrap=True, width=28)
    table.add_column("Value", no_wrap=True)

    slo_style = "green" if slo_current >= slo_target else "red bold"
    table.add_row("SLO Target", f"{slo_target * 100:.1f}%")
    table.add_row(
        "SLO Current",
        f"[{slo_style}]{slo_current * 100:.1f}%[/{slo_style}]",
    )
    table.add_row("Burn Rate", f"{burn_rate:.2f}x  [dim](1.0 = on-target)[/dim]")
    table.add_row(
        "Error Budget Remaining",
        f"{budget_fraction * 100:.1f}%  [dim]({budget_consumed_pct:.1f}% consumed)[/dim]",
    )
    table.add_row(
        "Days to Breach",
        f"[bold]{days_to_breach:.1f} days[/bold]" if days_to_breach is not None else "[green]Not at risk[/green]",
    )
    table.add_row("Total / Failed Tasks", f"{total_tasks} / {failed_tasks}")

    # --- Burn-down sparkline ---
    spark_text = ""
    if sparkline and not compact:
        # ASCII sparkline: map budget_fraction 0.0-1.0 to ▁▂▃▄▅▆▇█
        bars = "▁▂▃▄▅▆▇█"
        points = [float(p.get("budget_fraction", 1.0)) for p in sparkline[-20:]]
        spark_chars = [bars[min(int(v * 8), 7)] for v in points]
        spark_text = "  " + "".join(spark_chars) + "  [dim](error budget over time)[/dim]"

    # --- Projection line ---
    proj_style = "red bold" if status == "red" else "yellow" if status == "yellow" else "green"
    projection_line = f"\n[{proj_style}]{status_icon} {breach_projection}[/{proj_style}]"
    if spark_text:
        projection_line += f"\n{spark_text}"

    panel = Panel(
        table,
        title=f"[bold]SLO Burn-Down Dashboard[/bold]  [{status_style}]{status_icon} {status.upper()}[/{status_style}]",
        subtitle=Text.from_markup(f"[dim]{breach_projection}[/dim]"),
        border_style=status_style,
        padding=(0, 1),
    )
    console.print(panel)
    if spark_text:
        console.print(f"  Budget sparkline:{spark_text}")


def _load_offline(workdir: str) -> dict[str, Any] | None:
    """Try to read SLO data from .sdd/metrics/slos.json (offline fallback)."""
    path = Path(workdir) / ".sdd" / "metrics" / "slos.json"
    if not path.exists():
        return None
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            # slos.json has the full dashboard; extract error_budget for burn-down
            typed_raw = cast("dict[str, Any]", raw)
            eb: dict[str, Any] = typed_raw.get("error_budget", {})
            total: int = int(eb.get("total_tasks", 0))
            failed: int = int(eb.get("failed_tasks", 0))
            success_rate = (total - failed) / total if total > 0 else 1.0
            slo_target = 0.90
            from bernstein.core.slo import ErrorBudget

            budget_obj = ErrorBudget(total_tasks=total, failed_tasks=failed, slo_target=slo_target)
            burn_rate = budget_obj.burn_rate
            budget_fraction = budget_obj.budget_fraction
            is_depleted = budget_obj.is_depleted

            if is_depleted:
                projection = "Error budget exhausted — SLO breached now"
                status = "red"
            elif burn_rate > 1.5 or budget_fraction < 0.3:
                projection = "Error budget at risk — monitor closely"
                status = "yellow"
            else:
                projection = "On track — error budget not at risk"
                status = "green"

            return {
                "slo_name": "task_success",
                "slo_target": slo_target,
                "slo_current": round(success_rate, 4),
                "slo_met": success_rate >= slo_target,
                "burn_rate": round(burn_rate, 4),
                "burn_rate_per_day": None,
                "budget_fraction": round(budget_fraction, 4),
                "budget_consumed_pct": round((1.0 - budget_fraction) * 100, 1),
                "days_to_breach": None,
                "breach_projection": projection,
                "total_tasks": total,
                "failed_tasks": failed,
                "status": status,
                "sparkline": [],
                "history_size": 0,
            }
    except (OSError, json.JSONDecodeError, ZeroDivisionError):
        pass
    return None


@click.command("slo")
@click.option(
    "--workdir",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output raw JSON instead of formatted table.",
)
@click.option(
    "--watch",
    is_flag=True,
    default=False,
    help="Refresh every N seconds until interrupted.",
)
@click.option(
    "--interval",
    default=30,
    show_default=True,
    help="Refresh interval in seconds (--watch mode).",
)
@click.option(
    "--compact",
    is_flag=True,
    default=False,
    help="Compact output without sparkline.",
)
def slo_cmd(
    workdir: str,
    output_json: bool,
    watch: bool,
    interval: int,
    compact: bool,
) -> None:
    """Show SLO burn-down rate and error budget status.

    \b
    Displays:
      • Current SLO compliance % vs. target (default 90%)
      • Error budget consumption and remaining fraction
      • Burn rate (1.0 = consuming at exactly the allowed rate)
      • Linear projection of days until the SLO is breached
      • ASCII sparkline of error budget over recent history

    \b
    Examples:
      bernstein slo                  # formatted dashboard
      bernstein slo --json           # raw JSON
      bernstein slo --watch          # refresh every 30s
      bernstein slo --watch --interval 10
    """
    import time

    def _fetch() -> dict[str, Any] | None:
        # Try live server first (server_get returns dict | None directly).
        try:
            result = server_get("/slo/burndown")
            if isinstance(result, dict):
                return result
        except Exception:
            pass
        # Fallback to offline file.
        return _load_offline(workdir)

    def _display(data: dict[str, Any]) -> None:
        if output_json:
            click.echo(json.dumps(data, indent=2))
        else:
            _render_burndown(data, compact=compact)

    if not watch:
        data = _fetch()
        if data is None:
            console.print(
                "[yellow]No SLO data available.[/yellow] Start the task server or run ``bernstein run`` first."
            )
            raise SystemExit(1)
        _display(data)
        return

    # --watch mode: refresh until Ctrl-C
    try:
        while True:
            if not output_json:
                click.clear()
            data = _fetch()
            if data is None:
                console.print("[yellow]Waiting for SLO data…[/yellow]")
            else:
                _display(data)
            if not output_json:
                console.print(f"\n[dim]Refreshing every {interval}s — Ctrl-C to stop[/dim]")
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/dim]")
        sys.exit(0)
