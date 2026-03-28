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
                        round(v["duration_total"] / v["duration_count"], 1)
                        if v["duration_count"] > 0
                        else None
                    ),
                }
                for model, v in sorted_models
            ],
            "totals": totals,
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
        avg_dur = (
            f"{v['duration_total'] / v['duration_count']:.1f}s"
            if v["duration_count"] > 0
            else "—"
        )
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
    avg_total = (
        f"{total_dur / total_dur_count:.1f}s" if total_dur_count > 0 else "—"
    )
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
