"""Bernstein cost — spend visibility across all recorded metrics."""

from __future__ import annotations

import contextlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel

from bernstein.cli.helpers import is_json, print_json

console = Console()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ascii_bar(value: float, max_value: float, width: int = 30) -> str:
    """Return a block-character bar proportional to value/max_value."""
    if max_value <= 0 or value <= 0:
        return "░" * width
    filled = max(1, round((value / max_value) * width))
    filled = min(filled, width)
    return "█" * filled + "░" * (width - filled)


def _count_task_status(task_records: list[dict[str, Any]]) -> tuple[int, int]:
    """Return (done_count, failed_count) deduped by task_id."""
    seen: dict[str, dict[str, Any]] = {}
    for rec in task_records:
        tid = rec.get("task_id", "")
        if tid:
            seen[tid] = rec
    done = sum(1 for r in seen.values() if r.get("status") == "done")
    failed = sum(1 for r in seen.values() if r.get("status") == "failed")
    # If status not recorded, fall back to presence of cost as "done"
    if done == 0 and failed == 0 and seen:
        done = sum(1 for r in seen.values() if float(r.get("cost_usd", 0) or 0) > 0)
    return done, failed


def _render_savings_comparison(
    cons: Console,
    actual_cost: float,
    savings_vs_opus: float,
) -> None:
    """Print an ASCII bar chart comparing Bernstein vs all-Opus baseline."""
    single_agent_cost = actual_cost + savings_vs_opus
    if single_agent_cost <= 0:
        return

    savings_pct = (savings_vs_opus / single_agent_cost) * 100

    bar_width = 34
    single_bar = _ascii_bar(single_agent_cost, single_agent_cost, bar_width)
    bernstein_bar = _ascii_bar(actual_cost, single_agent_cost, bar_width)

    cons.print()
    cons.print("[bold]Cost Comparison[/bold]  (Bernstein vs all-Opus baseline)")
    cons.print(f"  Single agent  [red]{single_bar}[/red]  [dim]${single_agent_cost:.4f}[/dim]")
    cons.print(f"  Bernstein     [green]{bernstein_bar}[/green]  [bold green]${actual_cost:.4f}[/bold green]")
    if savings_pct > 0:
        cons.print(
            f"\n  [bold green]You saved ${savings_vs_opus:.4f} "
            f"({savings_pct:.0f}%) by using Bernstein's model cascade[/bold green]"
        )


def _render_shareable_summary(
    cons: Console,
    actual_cost: float,
    savings_vs_opus: float,
    tasks_done: int,
    tasks_failed: int,
    total_duration_s: float,
) -> None:
    """Print a copy-pasteable markdown run summary."""
    single_agent_cost = actual_cost + savings_vs_opus
    savings_pct = (savings_vs_opus / single_agent_cost) * 100 if single_agent_cost > 0 else 0

    mins = int(total_duration_s // 60)
    secs = int(total_duration_s % 60)
    time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

    lines: list[str] = [
        "🎼 Bernstein run summary",
        f"   Tasks: {tasks_done} completed" + (f", {tasks_failed} failed" if tasks_failed else ""),
    ]
    if total_duration_s > 0:
        lines.append(f"   Time:  {time_str}")
    if single_agent_cost > actual_cost:
        lines.append(f"   Cost:  ${actual_cost:.2f} (vs ~${single_agent_cost:.2f} single agent)")
        lines.append(f"   Saved: ${savings_vs_opus:.2f} ({savings_pct:.0f}%)")
    else:
        lines.append(f"   Cost:  ${actual_cost:.2f}")

    cons.print()
    cons.print(
        Panel(
            "\n".join(lines),
            title="[bold]Shareable summary[/bold]",
            border_style="dim",
            expand=False,
        )
    )


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


@click.command("estimate")
@click.argument("goal")
@click.option("--role", default="backend", help="Agent role for the task.")
@click.option("--scope", type=click.Choice(["small", "medium", "large"]), default="medium", help="Task scope.")
@click.option("--complexity", type=click.Choice(["low", "medium", "high"]), default="medium", help="Task complexity.")
@click.option(
    "--metrics-dir",
    default=".sdd/metrics",
    show_default=True,
    help="Directory containing historical metrics.",
)
def estimate_cmd(goal: str, role: str, scope: str, complexity: str, metrics_dir: str) -> None:
    """Predict the cost of a task before running it.

    \b
      bernstein estimate "Fix all typos in src/" --scope small
      bernstein estimate "Implement RAG system" --scope large --complexity high
    """
    from bernstein.core.cost import predict_task_cost
    from bernstein.core.models import Complexity, Scope, Task

    # Create a dummy task for prediction
    task = Task(
        id="estimate",
        title=goal[:100],
        description=goal,
        role=role,
        scope=Scope(scope),
        complexity=Complexity(complexity),
    )

    est_cost = predict_task_cost(task, metrics_dir=Path(metrics_dir))

    if is_json():
        print_json({
            "goal": goal,
            "role": role,
            "scope": scope,
            "complexity": complexity,
            "estimated_cost_usd": round(est_cost, 4)
        })
        return

    console.print(
        Panel(
            f"[bold]Cost Prediction[/bold]\n\n"
            f"Goal:       [cyan]{goal}[/cyan]\n"
            f"Role:       {role}\n"
            f"Scope:      {scope}\n"
            f"Complexity: {complexity}\n\n"
            f"Estimated total: [bold green]${est_cost:.4f}[/bold green] (±20%)\n\n"
            f"[dim]Note: Predictions use historical data when available and assume\n"
            f"average token consumption for the given scope/complexity.[/dim]",
            border_style="green",
            expand=False,
        )
    )


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
@click.option("--share", is_flag=True, default=False, help="Print only the shareable summary snippet.")
def cost_cmd(metrics_dir: str, as_json: bool, share: bool) -> None:
    """Show agent spend: cost, tokens, and duration per model."""
    mdir = Path(metrics_dir)
    if not mdir.exists():
        if as_json or is_json():
            print_json({"error": f"Metrics directory not found: {mdir}"})
        else:
            console.print(f"[red]Metrics directory not found:[/red] {mdir}")
        raise SystemExit(1)

    task_records = _load_tasks_jsonl(mdir)
    api_records = _load_api_usage_jsonl(mdir)

    rows = _aggregate(task_records, api_records)

    if not rows:
        if as_json or is_json():
            print_json({"rows": [], "totals": {}})
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
        compute_savings_vs_manual,
        compute_savings_vs_opus,
        project_monthly_cost,
    )

    savings_vs_opus = compute_savings_vs_opus(task_records)
    savings_vs_manual = compute_savings_vs_manual(task_records)
    daily_costs = compute_daily_cost(task_records, days=7)
    projected_monthly = project_monthly_cost(task_records, window_days=7)

    tasks_done, tasks_failed = _count_task_status(task_records)

    if as_json or is_json():
        output = {
            "rows": [
                {
                    "model": model,
                    "tasks": v["tasks"],
                    "tokens_in": v["tokens_in"],
                    "tokens_out": v["tokens_out"],
                    "cost_usd": round(v["cost_usd"], 6),
                    "cost_per_task": round(v["cost_usd"] / v["tasks"], 6) if v["tasks"] > 0 else 0,
                    "avg_duration_s": (
                        round(v["duration_total"] / v["duration_count"], 1) if v["duration_count"] > 0 else None
                    ),
                }
                for model, v in sorted_models
            ],
            "totals": totals,
            "fast_path": fast_path_savings,
            "savings_vs_opus_usd": round(savings_vs_opus, 6),
            "savings_vs_manual": savings_vs_manual,
            "daily_costs": daily_costs,
            "projected_monthly_usd": round(projected_monthly, 4),
            "tasks_done": tasks_done,
            "tasks_failed": tasks_failed,
        }
        print_json(output)
        return

    # --share: print only the shareable snippet and exit
    if share:
        _render_shareable_summary(
            console,
            actual_cost=totals["cost_usd"],
            savings_vs_opus=savings_vs_opus,
            tasks_done=tasks_done,
            tasks_failed=tasks_failed,
            total_duration_s=total_dur,
        )
        return

    from rich.table import Table

    table = Table(title="Bernstein Cost Report", header_style="bold cyan", show_lines=False)
    table.add_column("Model", min_width=20)
    table.add_column("Tasks", justify="right", min_width=6)
    table.add_column("Tokens In", justify="right", min_width=10)
    table.add_column("Tokens Out", justify="right", min_width=10)
    table.add_column("Cost USD", justify="right", min_width=10)
    table.add_column("Cost/Task", justify="right", min_width=10)
    table.add_column("Avg Duration", justify="right", min_width=12)

    for model, v in sorted_models:
        avg_dur = f"{v['duration_total'] / v['duration_count']:.1f}s" if v["duration_count"] > 0 else "—"
        cost_str = f"${v['cost_usd']:.4f}" if v["cost_usd"] > 0 else "$0.0000"
        cost_per_task = f"${v['cost_usd'] / v['tasks']:.4f}" if v["tasks"] > 0 else "—"
        table.add_row(
            model,
            str(v["tasks"]),
            f"{v['tokens_in']:,}",
            f"{v['tokens_out']:,}",
            cost_str,
            cost_per_task,
            avg_dur,
        )

    # Totals row
    avg_total = f"{total_dur / total_dur_count:.1f}s" if total_dur_count > 0 else "—"
    total_cost_per_task = f"${totals['cost_usd'] / totals['tasks']:.4f}" if totals["tasks"] > 0 else "—"
    table.add_section()
    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{totals['tasks']}[/bold]",
        f"[bold]{totals['tokens_in']:,}[/bold]",
        f"[bold]{totals['tokens_out']:,}[/bold]",
        f"[bold green]${totals['cost_usd']:.4f}[/bold green]",
        f"[bold]{total_cost_per_task}[/bold]",
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

    # Manual coding savings
    if savings_vs_manual["manual_hours"] > 0:
        console.print(
            f"\n[bold green]Manual Coding Savings:[/bold green] "
            f"Saved ~${savings_vs_manual['savings_usd']:.2f} compared to manual coding "
            f"({savings_vs_manual['manual_hours']} hrs @ $100/hr)"
        )

    # ASCII bar chart: Bernstein vs single-agent baseline
    _render_savings_comparison(console, totals["cost_usd"], savings_vs_opus)

    # Projected monthly cost
    if projected_monthly > 0:
        console.print(f"\n[dim]Projected monthly cost (30d):[/dim] ${projected_monthly:.2f}")

    # Shareable run summary
    _render_shareable_summary(
        console,
        actual_cost=totals["cost_usd"],
        savings_vs_opus=savings_vs_opus,
        tasks_done=tasks_done,
        tasks_failed=tasks_failed,
        total_duration_s=total_dur,
    )
