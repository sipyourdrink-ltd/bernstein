"""Bernstein cost — spend visibility across all recorded metrics."""

from __future__ import annotations

import contextlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import click
from rich.console import Console

console = Console()

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_tasks_jsonl(metrics_dir: Path) -> list[dict[str, Any]]:
    p = metrics_dir / "tasks.jsonl"
    if not p.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                records.append(json.loads(line))
    return records


def _load_api_usage_jsonl(metrics_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for p in sorted(metrics_dir.glob("api_usage_*.jsonl")):
        for line in p.read_text().splitlines():
            line = line.strip()
            if line:
                with contextlib.suppress(json.JSONDecodeError):
                    records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate_fast_path_savings(task_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate fast-path savings from task records.

    Returns dict with tasks_bypassed, estimated_savings_usd, and action breakdown.
    """
    bypassed = 0
    savings = 0.0
    actions: dict[str, int] = defaultdict(int)
    for rec in task_records:
        if rec.get("model") == "fast-path":
            bypassed += 1
            savings += float(rec.get("estimated_savings_usd", 0.0) or 0.0)
            action = rec.get("fast_path_action", "unknown")
            actions[action] += 1
    return {
        "tasks_bypassed": bypassed,
        "estimated_savings_usd": savings,
        "actions": dict(actions),
    }


def _aggregate(
    task_records: list[dict[str, Any]],
    api_records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return per-model aggregated stats.

    Keys: model name (or "unknown")
    Values: dict with tasks, tokens_in, tokens_out, cost_usd, duration_total, duration_count
    """
    rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "tasks": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "cost_usd": 0.0,
            "duration_total": 0.0,
            "duration_count": 0,
        }
    )

    # Deduplicate task records by task_id, keeping the last entry
    seen: dict[str, dict[str, Any]] = {}
    for rec in task_records:
        tid = rec.get("task_id", "")
        if tid:
            seen[tid] = rec
        else:
            # No task_id — treat as anonymous
            model = rec.get("model") or "unknown"
            row = rows[model]
            row["tasks"] += 1
            row["tokens_in"] += int(rec.get("tokens_prompt", 0) or 0)
            row["tokens_out"] += int(rec.get("tokens_completion", 0) or 0)
            row["cost_usd"] += float(rec.get("cost_usd", 0.0) or 0.0)
            dur = float(rec.get("duration_seconds", 0.0) or 0.0)
            if dur > 0:
                row["duration_total"] += dur
                row["duration_count"] += 1

    for rec in seen.values():
        model = rec.get("model") or "unknown"
        row = rows[model]
        row["tasks"] += 1
        row["tokens_in"] += int(rec.get("tokens_prompt", 0) or 0)
        row["tokens_out"] += int(rec.get("tokens_completion", 0) or 0)
        row["cost_usd"] += float(rec.get("cost_usd", 0.0) or 0.0)
        dur = float(rec.get("duration_seconds", 0.0) or 0.0)
        if dur > 0:
            row["duration_total"] += dur
            row["duration_count"] += 1

    # api_usage records have labels with provider/model but no token breakdown
    for rec in api_records:
        labels = rec.get("labels", {})
        model = labels.get("model") or "unknown"
        if model not in rows:
            rows[model]  # ensure key exists (defaultdict)

    return dict(rows)


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@click.command("cost")
@click.option(
    "--metrics-dir",
    default=".sdd/metrics",
    show_default=True,
    help="Directory containing metrics JSONL files.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def cost_cmd(metrics_dir: str, as_json: bool) -> None:
    """Show agent spend: cost, tokens, and duration per model."""
    mdir = Path(metrics_dir)
    if not mdir.exists():
        if as_json:
            click.echo(json.dumps({"error": f"Metrics directory not found: {mdir}"}))
        else:
            console.print(f"[red]Metrics directory not found:[/red] {mdir}")
        raise SystemExit(1)

    task_records = _load_tasks_jsonl(mdir)
    api_records = _load_api_usage_jsonl(mdir)

    rows = _aggregate(task_records, api_records)

    if not rows:
        if as_json:
            click.echo(json.dumps({"rows": [], "totals": {}}))
        else:
            console.print("[dim]No metrics data found.[/dim]")
        return

    # Sort by cost descending, then by task count
    sorted_models = sorted(rows.items(), key=lambda kv: (-kv[1]["cost_usd"], -kv[1]["tasks"]))

    totals: dict[str, Any] = {
        "tasks": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0.0,
        "avg_duration_s": None,
    }
    total_dur = 0.0
    total_dur_count = 0
    for _, v in sorted_models:
        totals["tasks"] += v["tasks"]
        totals["tokens_in"] += v["tokens_in"]
        totals["tokens_out"] += v["tokens_out"]
        totals["cost_usd"] += v["cost_usd"]
        total_dur += v["duration_total"]
        total_dur_count += v["duration_count"]
    if total_dur_count > 0:
        totals["avg_duration_s"] = round(total_dur / total_dur_count, 1)

    fast_path_savings = _aggregate_fast_path_savings(task_records)

    from bernstein.core.cost import (
        compute_daily_cost,
        compute_savings_vs_opus,
        project_monthly_cost,
    )

    savings_vs_opus = compute_savings_vs_opus(task_records)
    daily_costs = compute_daily_cost(task_records, days=7)
    projected_monthly = project_monthly_cost(task_records, window_days=7)

    if as_json:
        output = {
            "rows": [
                {
                    "model": model,
                    "tasks": v["tasks"],
                    "tokens_in": v["tokens_in"],
                    "tokens_out": v["tokens_out"],
                    "cost_usd": round(v["cost_usd"], 6),
                    "avg_duration_s": (
                        round(v["duration_total"] / v["duration_count"], 1) if v["duration_count"] > 0 else None
                    ),
                }
                for model, v in sorted_models
            ],
            "totals": totals,
            "fast_path": fast_path_savings,
            "savings_vs_opus_usd": round(savings_vs_opus, 6),
            "daily_costs": daily_costs,
            "projected_monthly_usd": round(projected_monthly, 4),
        }
        click.echo(json.dumps(output, indent=2))
        return

    from rich.table import Table

    table = Table(title="Bernstein Cost Report", header_style="bold cyan", show_lines=False)
    table.add_column("Model", min_width=20)
    table.add_column("Tasks", justify="right", min_width=6)
    table.add_column("Tokens In", justify="right", min_width=10)
    table.add_column("Tokens Out", justify="right", min_width=10)
    table.add_column("Cost USD", justify="right", min_width=10)
    table.add_column("Avg Duration", justify="right", min_width=12)

    for model, v in sorted_models:
        avg_dur = f"{v['duration_total'] / v['duration_count']:.1f}s" if v["duration_count"] > 0 else "—"
        cost_str = f"${v['cost_usd']:.4f}" if v["cost_usd"] > 0 else "$0.0000"
        table.add_row(
            model,
            str(v["tasks"]),
            f"{v['tokens_in']:,}",
            f"{v['tokens_out']:,}",
            cost_str,
            avg_dur,
        )

    # Totals row
    avg_total = f"{total_dur / total_dur_count:.1f}s" if total_dur_count > 0 else "—"
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{totals['tasks']}[/bold]",
        f"[bold]{totals['tokens_in']:,}[/bold]",
        f"[bold]{totals['tokens_out']:,}[/bold]",
        f"[bold green]${totals['cost_usd']:.4f}[/bold green]",
        f"[bold]{avg_total}[/bold]",
    )

    console.print(table)

    # Fast-path savings summary
    if fast_path_savings["tasks_bypassed"] > 0:
        bp = fast_path_savings["tasks_bypassed"]
        sv = fast_path_savings["estimated_savings_usd"]
        actions = fast_path_savings["actions"]
        action_parts = [f"{v} {k}" for k, v in sorted(actions.items(), key=lambda x: -x[1])]
        console.print(
            f"\n[bold green]Fast-path:[/bold green] Saved ~${sv:.2f} by "
            f"bypassing LLM for {bp} task(s) ({', '.join(action_parts)})"
        )

    # Savings vs all-Opus baseline
    console.print(f"[bold green]Savings vs Opus baseline:[/bold green] ~${savings_vs_opus:.4f}")

    # Projected monthly cost
    if projected_monthly > 0:
        console.print(f"[dim]Projected monthly cost (30d):[/dim] ${projected_monthly:.2f}")
