"""Bernstein cost: spend visibility across all recorded metrics."""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, cast

import click
from rich.console import Console
from rich.panel import Panel

from bernstein.cli.helpers import is_json, print_json
from bernstein.core.observability.metric_collector import iter_metric_files

console = Console()


# ---------------------------------------------------------------------------
# Time-range parsing
# ---------------------------------------------------------------------------


def _parse_time_range(spec: str) -> float:
    """Parse a human time-range spec like ``7d``, ``24h``, ``1h`` into a cutoff timestamp.

    Returns a Unix timestamp; records older than this should be excluded.

    Args:
        spec: Time range string (e.g. ``"7d"``, ``"24h"``, ``"1h"``).

    Returns:
        Unix timestamp representing the start of the window.

    Raises:
        click.BadParameter: If *spec* cannot be parsed.
    """
    m = re.fullmatch(r"(\d+)\s*([hHdDwWmM])", spec.strip())
    if not m:
        msg = f"Invalid time range: {spec!r}. Use e.g. 1h, 24h, 7d, 30d."
        raise click.BadParameter(msg)
    value = int(m.group(1))
    unit = m.group(2).lower()
    multipliers = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000}
    return time.time() - value * multipliers[unit]


def _filter_by_time(records: list[dict[str, Any]], cutoff: float) -> list[dict[str, Any]]:
    """Filter records to those with ``timestamp >= cutoff``.

    Args:
        records: List of JSONL record dicts.
        cutoff: Unix timestamp lower bound.

    Returns:
        Filtered list (preserves order).
    """
    return [r for r in records if float(r.get("timestamp", 0) or 0) >= cutoff]


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
    if done == failed == 0 and seen:
        done = sum(1 for r in seen.values() if float(r.get("cost_usd", 0) or 0) > 0)
    return done, failed


def _profile_comparison_line(comp: Any) -> str:
    """One human-readable line for a cross-profile comparison."""
    return (
        f"{comp.profile_a} vs {comp.profile_b} ({comp.role}/{comp.model}): "
        f"${comp.mean_cost_usd_per_task_a:.4f} vs ${comp.mean_cost_usd_per_task_b:.4f} per task, "
        f"{comp.mean_output_tokens_per_task_a:.0f} vs {comp.mean_output_tokens_per_task_b:.0f} out-tokens/task "
        f"({comp.tasks_a}+{comp.tasks_b} tasks)"
    )


def _render_profile_savings_section(
    cons: Console,
    profile_comparisons: list[Any] | None,
    profiles_seen: int,
) -> None:
    """Print the per-profile savings section, honouring the honesty rule.

    Nothing is printed when fewer than two profiles appear in the
    ledger window. With two or more profiles but no cohort where both
    sides reach the comparable-task bar, the section prints
    "insufficient comparable runs" instead of a savings claim.
    """
    from bernstein.core.cost.profile_attribution import MIN_COMPARABLE_TASKS

    if profiles_seen < 2:
        return
    cons.print()
    cons.print("[bold]Per-profile comparison[/bold]  (same role+model cohorts only)")
    if not profile_comparisons:
        cons.print(
            f"  [dim]insufficient comparable runs (need >= {MIN_COMPARABLE_TASKS} "
            f"tasks per profile with matching role and model)[/dim]"
        )
        return
    for comp in profile_comparisons:
        cons.print(f"  {_profile_comparison_line(comp)}")


def _render_savings_comparison(
    cons: Console,
    actual_cost: float,
    savings_vs_opus: float,
    profile_comparisons: list[Any] | None = None,
    profiles_seen: int = 0,
) -> None:
    """Print an ASCII bar chart comparing Bernstein vs all-Opus baseline.

    When the ledger window covers two or more response-style profiles a
    per-profile section follows the chart (issue #2245); the section
    obeys the honesty rule via :func:`_render_profile_savings_section`.
    """
    single_agent_cost = actual_cost + savings_vs_opus
    if single_agent_cost > 0:
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
    _render_profile_savings_section(cons, profile_comparisons, profiles_seen)


def _render_shareable_summary(
    cons: Console,
    actual_cost: float,
    savings_vs_opus: float,
    tasks_done: int,
    tasks_failed: int,
    total_duration_s: float,
    profile_comparisons: list[Any] | None = None,
    profiles_seen: int = 0,
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
        lines.extend(
            (
                f"   Cost:  ${actual_cost:.2f} (vs ~${single_agent_cost:.2f} single agent)",
                f"   Saved: ${savings_vs_opus:.2f} ({savings_pct:.0f}%)",
            )
        )
    else:
        lines.append(f"   Cost:  ${actual_cost:.2f}")
    if profiles_seen >= 2:
        if profile_comparisons:
            lines.extend(f"   Profiles: {_profile_comparison_line(comp)}" for comp in profile_comparisons)
        else:
            lines.append("   Profiles: insufficient comparable runs")

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
# Cache hit rate
# ---------------------------------------------------------------------------


def _compute_cache_hit_rate(sdd_dir: Path) -> float | None:
    """Compute cache hit rate from ``.sdd/runtime/*.tokens`` files.

    Each line is JSONL: ``{"ts": float, "in": int, "out": int, "cache_read": int, "cache_write": int}``.

    Returns cache_read / (cache_read + cache_write) * 100, or ``None`` if
    no cache data is available.

    Args:
        sdd_dir: Path to the ``.sdd`` directory.

    Returns:
        Cache hit rate as a percentage, or ``None``.
    """
    runtime_dir = sdd_dir / "runtime"
    if not runtime_dir.exists():
        return None
    total_read = 0
    total_write = 0
    for tokens_file in runtime_dir.glob("*.tokens"):
        for line in tokens_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                rec = json.loads(line)
                total_read += int(rec.get("cache_read", 0) or 0)
                total_write += int(rec.get("cache_write", 0) or 0)

    total = total_read + total_write
    if total == 0:
        return None
    return (total_read / total) * 100.0


# ---------------------------------------------------------------------------
# "By" aggregation helpers
# ---------------------------------------------------------------------------


def _aggregate_by_agent(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate task records grouped by agent_id."""
    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"tasks": 0, "cost_usd": 0.0})
    for rec in records:
        agent = str(rec.get("agent_id", "") or rec.get("role", "unknown"))
        rows[agent]["tasks"] += 1
        rows[agent]["cost_usd"] += float(rec.get("cost_usd", 0.0) or 0.0)
    return rows.copy()


def _aggregate_by_task(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate task records grouped by task_id."""
    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"tasks": 0, "cost_usd": 0.0, "model": ""})
    for rec in records:
        tid = str(rec.get("task_id", "unknown"))
        rows[tid]["tasks"] += 1
        rows[tid]["cost_usd"] += float(rec.get("cost_usd", 0.0) or 0.0)
        rows[tid]["model"] = str(rec.get("model", "") or "")
    return rows.copy()


def _aggregate_by_day(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate task records grouped by date (YYYY-MM-DD)."""
    import datetime as _dt

    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"tasks": 0, "cost_usd": 0.0})
    for rec in records:
        ts = float(rec.get("timestamp", 0) or 0)
        day = _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).strftime("%Y-%m-%d") if ts > 0 else "unknown"
        rows[day]["tasks"] += 1
        rows[day]["cost_usd"] += float(rec.get("cost_usd", 0.0) or 0.0)
    return rows.copy()


def _aggregate_from_ledger_or_tasks(
    ledger_path: Path,
    task_records: list[dict[str, Any]],
    dimension: str,
    cutoff: float,
) -> dict[str, dict[str, Any]]:
    """Aggregate spend by *dimension* using the JSONL ledger when present.

    Issue #1320: ``role`` and ``feature_label`` are first-class tags
    written by the LLM-adapter pre-call hook into ``.sdd/cost/ledger.jsonl``.
    When the ledger is unavailable we degrade to ``task_records`` so older
    runs still show a sensible breakdown.
    """
    from bernstein.core.cost.spend_ledger import SpendLedger, aggregate_entries

    if ledger_path.exists():
        entries = SpendLedger.load_entries(ledger_path)
        if cutoff > 0:
            entries = [e for e in entries if e.ts >= cutoff]
        grouped = aggregate_entries(entries, dimension)
        return {k: {"tasks": v["calls"], "cost_usd": v["cost_usd"]} for k, v in grouped.items()}

    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"tasks": 0, "cost_usd": 0.0})
    for rec in task_records:
        if dimension == "role":
            label = str(rec.get("role", "") or "unknown")
        elif dimension == "envelope":
            raw_tags_env: object = rec.get("cost_tags") or {}
            if isinstance(raw_tags_env, dict):
                tags_env = cast("dict[str, Any]", raw_tags_env)
                label = str(tags_env.get("quota_envelope", "") or "subscription")
            else:
                label = "subscription"
        else:
            tags = rec.get("cost_tags") or {}
            label = str(tags.get("feature_label", "") or "unknown") if isinstance(tags, dict) else "unknown"
        rows[label]["tasks"] += 1
        rows[label]["cost_usd"] += float(rec.get("cost_usd", 0.0) or 0.0)
    return rows.copy()


def _load_profile_ledger_view(ledger_path: Path, cutoff: float) -> tuple[list[Any], list[Any]]:
    """Load (entries, transitions) for per-profile views from the ledger.

    The transitions file lives next to the ledger
    (``profile_transitions.jsonl``). A missing ledger yields empty
    lists so callers degrade to "no profile data".
    """
    from bernstein.core.cost.profile_attribution import TRANSITIONS_FILENAME, load_transitions
    from bernstein.core.cost.spend_ledger import SpendLedger

    if not ledger_path.exists():
        return [], []
    entries = SpendLedger.load_entries(ledger_path)
    if cutoff > 0:
        entries = [e for e in entries if e.ts >= cutoff]
    transitions = load_transitions(ledger_path.parent / TRANSITIONS_FILENAME)
    return entries, transitions


def _profile_comparisons_from_ledger(ledger_path: Path, cutoff: float) -> tuple[list[Any], int]:
    """Return (comparisons, profiles_seen) for the savings sections.

    ``profiles_seen`` counts distinct profile tags among non-excluded
    entries; the honesty-rule renderers stay silent below two.
    """
    from bernstein.core.cost.profile_attribution import (
        compute_profile_comparisons,
        entry_profile,
        transitioned_task_ids,
    )

    entries, transitions = _load_profile_ledger_view(ledger_path, cutoff)
    if not entries:
        return [], 0
    excluded_ids = transitioned_task_ids(transitions)
    profiles = {
        entry_profile(e) for e in entries if entry_profile(e) and (not e.task_id or e.task_id not in excluded_ids)
    }
    comparisons = compute_profile_comparisons(entries, transitions)
    return list(comparisons), len(profiles)


def _aggregate_profile_grouping(
    ledger_path: Path,
    task_records: list[dict[str, Any]],
    cutoff: float,
) -> dict[str, dict[str, Any]]:
    """Aggregate spend by response-style profile (issue #2245).

    Ledger-backed when the ledger exists (per-entry attribution with
    transition exclusion); otherwise degrades to the
    ``cost_tags.response_profile`` stamped on task records so older
    runs still show a breakdown.
    """
    from bernstein.core.cost.profile_attribution import (
        UNATTRIBUTED_LABEL,
        aggregate_ledger_by_profile,
    )

    if ledger_path.exists():
        entries, transitions = _load_profile_ledger_view(ledger_path, cutoff)
        grouped = aggregate_ledger_by_profile(entries, transitions)
        return {k: {"tasks": v["tasks"], "cost_usd": v["cost_usd"]} for k, v in grouped.items()}

    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"tasks": 0, "cost_usd": 0.0})
    for rec in task_records:
        raw_tags: object = rec.get("cost_tags") or {}
        label = ""
        if isinstance(raw_tags, dict):
            tags = cast("dict[str, Any]", raw_tags)
            label = str(tags.get("response_profile", "") or "")
        rows[label or UNATTRIBUTED_LABEL]["tasks"] += 1
        rows[label or UNATTRIBUTED_LABEL]["cost_usd"] += float(rec.get("cost_usd", 0.0) or 0.0)
    return rows.copy()


def _compute_downgrade_tip(records: list[dict[str, Any]]) -> tuple[str, float] | None:
    """Estimate potential savings from downgrading simple opus tasks to sonnet.

    Returns a (tip_message, savings_usd) tuple, or ``None`` if no savings.
    """
    opus_simple_cost = 0.0
    opus_total = 0
    opus_simple = 0

    for rec in records:
        model = str(rec.get("model", "")).lower()
        if "opus" not in model:
            continue
        opus_total += 1
        scope = str(rec.get("scope", "")).lower()
        complexity = str(rec.get("complexity", "")).lower()
        if scope in ("small", "medium", "") and complexity in ("low", "medium", ""):
            opus_simple += 1
            opus_simple_cost += float(rec.get("cost_usd", 0.0) or 0.0)

    if 0 in (opus_total, opus_simple):
        return None

    # Sonnet is roughly 60% of opus cost
    savings = opus_simple_cost * 0.40
    pct = int((opus_simple / opus_total) * 100)

    tip = f"{pct}% of opus tasks could have used sonnet (simple scope, low complexity)"
    return tip, round(savings, 2)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Load JSONL records from a single file."""
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                records.append(json.loads(line))
    return records


def _load_tasks_jsonl(metrics_dir: Path) -> list[dict[str, Any]]:
    return _load_jsonl(metrics_dir / "tasks.jsonl")


def _load_archive_tasks(sdd_dir: Path) -> list[dict[str, Any]]:
    """Load task records from ``.sdd/archive/tasks.jsonl``."""
    return _load_jsonl(sdd_dir / "archive" / "tasks.jsonl")


def _load_api_usage_jsonl(metrics_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for p in iter_metric_files(metrics_dir, "api_usage"):
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
        "actions": actions.copy(),
    }


def _accumulate_record(row: dict[str, Any], rec: dict[str, Any]) -> None:
    """Accumulate a single task record into an aggregation row."""
    row["tasks"] += 1
    row["tokens_in"] += int(rec.get("tokens_prompt", 0) or 0)
    row["tokens_out"] += int(rec.get("tokens_completion", 0) or 0)
    row["cost_usd"] += float(rec.get("cost_usd", 0.0) or 0.0)
    dur = float(rec.get("duration_seconds", 0.0) or 0.0)
    if dur > 0:
        row["duration_total"] += dur
        row["duration_count"] += 1


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
            _accumulate_record(rows[rec.get("model") or "unknown"], rec)

    for rec in seen.values():
        _accumulate_record(rows[rec.get("model") or "unknown"], rec)

    # api_usage records have labels with provider/model but no token breakdown
    for rec in api_records:
        labels = rec.get("labels", {})
        model = labels.get("model") or "unknown"
        if model not in rows:
            rows[model]  # ensure key exists (defaultdict)

    return rows.copy()


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
        print_json(
            {
                "goal": goal,
                "role": role,
                "scope": scope,
                "complexity": complexity,
                "estimated_cost_usd": round(est_cost, 4),
            }
        )
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


def _cost_render_json(
    time_label: str,
    sorted_models: list[tuple[str, dict[str, Any]]],
    totals: dict[str, Any],
    fast_path_savings: dict[str, Any],
    savings_vs_opus: float,
    savings_vs_manual: dict[str, Any],
    daily_costs: Any,
    projected_monthly: float,
    tasks_done: int,
    tasks_failed: int,
    cache_hit_rate: float | None,
    grouped_data: dict[str, dict[str, Any]] | None,
    group_by: str | None,
    downgrade: tuple[str, float] | None,
    profile_comparisons: list[Any] | None = None,
    profiles_seen: int = 0,
) -> None:
    """Render cost report as JSON."""
    output: dict[str, Any] = {
        "time_range": time_label,
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
        "cache_hit_rate": round(cache_hit_rate, 1) if cache_hit_rate is not None else None,
    }
    if grouped_data is not None:
        output["grouped_by"] = group_by
        output["grouped"] = {
            k: {"tasks": v["tasks"], "cost_usd": round(v["cost_usd"], 6)}
            for k, v in sorted(grouped_data.items(), key=lambda kv: -kv[1]["cost_usd"])
        }
    if downgrade is not None:
        output["tip"] = downgrade[0]
        output["potential_savings_usd"] = downgrade[1]
    if profiles_seen >= 2:
        output["profile_comparisons"] = [c.to_dict() for c in (profile_comparisons or [])]
        output["insufficient_comparable_runs"] = not profile_comparisons
    print_json(output)


def _cost_render_grouped(
    title: str,
    grouped_data: dict[str, dict[str, Any]],
    group_by: str,
    cache_hit_rate: float | None,
    downgrade: tuple[str, float] | None,
) -> None:
    """Render a grouped cost breakdown table."""
    sorted_grouped = sorted(grouped_data.items(), key=lambda kv: -kv[1]["cost_usd"])
    total_cost = sum(v["cost_usd"] for v in grouped_data.values())
    total_tasks = sum(v["tasks"] for v in grouped_data.values())
    max_cost = max((v["cost_usd"] for v in grouped_data.values()), default=0.0)

    console.print(f"\n[bold]{title}[/bold]\n")
    console.print(f"  [bold]By {group_by.title()}:[/bold]")
    for label, v in sorted_grouped:
        pct = int((v["cost_usd"] / total_cost) * 100) if total_cost > 0 else 0
        bar = _ascii_bar(v["cost_usd"], max_cost, 16)
        console.print(f"    {label:<22s} ${v['cost_usd']:>7.2f}  ({pct:>2d}%)  {bar}  {v['tasks']:,} tasks")

    console.print(f"\n  Total: ${total_cost:.2f} across {total_tasks:,} tasks")
    if total_tasks > 0:
        console.print(f"  Avg cost/task: ${total_cost / total_tasks:.3f}")
    if cache_hit_rate is not None:
        console.print(f"  Cache hit rate: {cache_hit_rate:.0f}%")
    if downgrade is not None:
        console.print(f"\n  [dim]Tip: {downgrade[0]}[/dim]")
        console.print(f"  [dim]Potential savings: ${downgrade[1]:.2f}/week with smarter routing[/dim]")
    console.print()


@click.group("cost", invoke_without_command=True)
@click.option(
    "--metrics-dir",
    default=".sdd/metrics",
    show_default=True,
    help="Directory containing metrics JSONL files.",
)
@click.option("--last", "last", type=str, default=None, help="Time range: 1h, 24h, 7d, 30d.")
@click.option(
    "--since",
    "since",
    type=str,
    default=None,
    help="Anchor for --last (e.g. ``today``, ``yesterday``); shorthand for ``--last 24h`` etc.",
)
@click.option(
    "--by",
    "group_by",
    type=click.Choice(["agent", "model", "task", "day", "role", "feature_label", "envelope", "profile"]),
    default=None,
    help=(
        "Group breakdown by agent, model, task, day, role, feature_label, "
        "envelope (issue #1405), or profile (issue #2245)."
    ),
)
@click.option(
    "--ledger",
    "ledger_path",
    type=str,
    default=".sdd/cost/ledger.jsonl",
    show_default=True,
    help="Path to the rolling spend ledger (issue #1320). Used when --by is role|feature_label|profile.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.option("--share", is_flag=True, default=False, help="Print only the shareable summary snippet.")
@click.pass_context
def cost_cmd(
    ctx: click.Context,
    metrics_dir: str,
    last: str | None,
    since: str | None,
    group_by: str | None,
    ledger_path: str,
    as_json: bool,
    share: bool,
) -> None:
    """Show cost breakdown for recent runs."""
    if ctx.invoked_subcommand is not None:
        return
    # --since is a convenience: ``today`` ≈ ``--last 24h``,
    # ``yesterday`` ≈ ``--last 48h``. When both are provided ``--last`` wins.
    if last is None and since is not None:
        s = since.strip().lower()
        last = {"today": "24h", "yesterday": "48h", "week": "7d", "month": "30d"}.get(s, last)
        if last is None:
            # Treat unknown --since values as a direct time range (e.g. ``7d``).
            last = since

    mdir = Path(metrics_dir)
    if not mdir.exists():
        if as_json or is_json():
            print_json({"error": f"Metrics directory not found: {mdir}"})
        else:
            console.print(f"[red]Metrics directory not found:[/red] {mdir}")
        raise SystemExit(1)

    task_records = _load_tasks_jsonl(mdir)

    # Also load archive tasks from .sdd/archive/tasks.jsonl
    sdd_dir = mdir.parent  # .sdd/metrics -> .sdd
    archive_records = _load_archive_tasks(sdd_dir)
    task_records = archive_records + task_records

    api_records = _load_api_usage_jsonl(mdir)

    # Apply time-range filter
    cutoff: float = 0.0
    time_label = "all time"
    if last is not None:
        cutoff = _parse_time_range(last)
        time_label = f"last {last}"
        task_records = _filter_by_time(task_records, cutoff)
        api_records = _filter_by_time(api_records, cutoff)

    rows = _aggregate(task_records, api_records)

    if not rows and not task_records:
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

    # Cache hit rate from .sdd/runtime/*.tokens
    cache_hit_rate = _compute_cache_hit_rate(sdd_dir)

    # Downgrade tip
    downgrade = _compute_downgrade_tip(task_records)

    # --by grouping (alternative views)
    grouped_data: dict[str, dict[str, Any]] | None = None
    if group_by == "agent":
        grouped_data = _aggregate_by_agent(task_records)
    elif group_by == "task":
        grouped_data = _aggregate_by_task(task_records)
    elif group_by == "day":
        grouped_data = _aggregate_by_day(task_records)
    elif group_by in ("role", "feature_label", "envelope"):
        # Issue #1320 + #1405: role / feature_label / envelope are tagged
        # dimensions that live in the rolling spend ledger. Fall back to
        # task_records when the ledger is missing so old runs still show
        # a sensible view.
        grouped_data = _aggregate_from_ledger_or_tasks(
            Path(ledger_path),
            task_records,
            group_by,
            cutoff,
        )
    elif group_by == "profile":
        # Issue #2245: per-entry attribution from the ledger with
        # transition exclusion; tasks that changed profile mid-flight
        # appear as an explicit excluded bucket, never split.
        grouped_data = _aggregate_profile_grouping(Path(ledger_path), task_records, cutoff)
    # group_by == "model" or None => use the default rows (by model)

    # Per-profile savings inputs (issue #2245). Honesty rule: the
    # renderers only speak when >= 2 profiles are present, and only
    # claim savings for cohorts that clear MIN_COMPARABLE_TASKS.
    profile_comparisons, profiles_seen = _profile_comparisons_from_ledger(Path(ledger_path), cutoff)

    if as_json or is_json():
        _cost_render_json(
            time_label,
            sorted_models,
            totals,
            fast_path_savings,
            savings_vs_opus,
            savings_vs_manual,
            daily_costs,
            projected_monthly,
            tasks_done,
            tasks_failed,
            cache_hit_rate,
            grouped_data,
            group_by,
            downgrade,
            profile_comparisons,
            profiles_seen,
        )
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
            profile_comparisons=profile_comparisons,
            profiles_seen=profiles_seen,
        )
        return

    from rich.table import Table

    title = f"Bernstein Cost Report ({time_label})"

    if grouped_data is not None:
        assert group_by is not None
        _cost_render_grouped(title, grouped_data, group_by, cache_hit_rate, downgrade)
        return

    # Default: full model breakdown table
    table = Table(title=title, header_style="bold cyan", show_lines=False)
    table.add_column("Model", min_width=20, no_wrap=True)
    table.add_column("Tasks", justify="right", min_width=6)
    table.add_column("Tokens In", justify="right", min_width=10)
    table.add_column("Tokens Out", justify="right", min_width=10)
    table.add_column("Cost USD", justify="right", min_width=10)
    table.add_column("Cost/Task", justify="right", min_width=10)
    table.add_column("Avg Duration", justify="right", min_width=12)

    for model, v in sorted_models:
        avg_dur = f"{v['duration_total'] / v['duration_count']:.1f}s" if v["duration_count"] > 0 else "-"
        cost_str = f"${v['cost_usd']:.4f}" if v["cost_usd"] > 0 else "$0.0000"
        cost_per_task = f"${v['cost_usd'] / v['tasks']:.4f}" if v["tasks"] > 0 else "-"
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
    avg_total = f"{total_dur / total_dur_count:.1f}s" if total_dur_count > 0 else "-"
    total_cost_per_task = f"${totals['cost_usd'] / totals['tasks']:.4f}" if totals["tasks"] > 0 else "-"
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

    # Cache hit rate
    if cache_hit_rate is not None:
        console.print(f"\n[dim]Cache hit rate:[/dim] {cache_hit_rate:.0f}%")

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

    # ASCII bar chart: Bernstein vs single-agent baseline, followed by
    # the per-profile comparison section when profiles are present.
    _render_savings_comparison(
        console,
        totals["cost_usd"],
        savings_vs_opus,
        profile_comparisons=profile_comparisons,
        profiles_seen=profiles_seen,
    )

    # Projected monthly cost
    if projected_monthly > 0:
        console.print(f"\n[dim]Projected monthly cost (30d):[/dim] ${projected_monthly:.2f}")

    # Downgrade tip
    if downgrade is not None:
        console.print(f"\n  [dim]Tip: {downgrade[0]}[/dim]")
        console.print(f"  [dim]Potential savings: ${downgrade[1]:.2f}/week with smarter routing[/dim]")

    # Shareable run summary
    _render_shareable_summary(
        console,
        actual_cost=totals["cost_usd"],
        savings_vs_opus=savings_vs_opus,
        tasks_done=tasks_done,
        tasks_failed=tasks_failed,
        total_duration_s=total_dur,
        profile_comparisons=profile_comparisons,
        profiles_seen=profiles_seen,
    )


# ---------------------------------------------------------------------------
# `bernstein cost profile-report` (issue #2245)
# ---------------------------------------------------------------------------


def _profile_comparison_evidence(
    eval_ab_dir: Path,
    comparisons: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Link each cross-profile claim to the latest comparison artifact.

    For every (profile_a, profile_b) pair in the report's comparisons,
    look up the newest ``eval ab`` comparison artifact for that pair in
    the pair index. Pairs without recorded evidence are omitted; the
    link is presentation metadata and never enters the report's hashed
    payload.
    """
    from bernstein.eval.ab_comparison import latest_comparison_for_pair

    evidence: dict[str, dict[str, Any]] = {}
    for comp in comparisons:
        pair_key = f"{comp['profile_a']} vs {comp['profile_b']}"
        if pair_key in evidence:
            continue
        found = latest_comparison_for_pair(eval_ab_dir, str(comp["profile_a"]), str(comp["profile_b"]))
        if found is not None:
            evidence[pair_key] = {
                "artifact_name": str(found.get("artifact_name", "")),
                "artifact_sha256": str(found.get("artifact_sha256", "")),
            }
    return evidence


def _render_profile_report_human(
    cons: Console,
    content: dict[str, Any],
    sha256: str,
    artifact: Path,
    evidence: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Render the profile report as a table plus its verification anchors."""
    from rich.table import Table

    table = Table(title=f"Per-profile cost report ({content['window']})", header_style="bold cyan", show_lines=False)
    table.add_column("Profile", min_width=14, no_wrap=True)
    table.add_column("Tasks", justify="right", min_width=6)
    table.add_column("Out tokens", justify="right", min_width=10)
    table.add_column("Cost USD", justify="right", min_width=10)
    table.add_column("Tokens/task", justify="right", min_width=11)
    table.add_column("Pass rate", justify="right", min_width=9)

    profiles = cast("dict[str, Any]", content["profiles"])
    for label in sorted(profiles):
        row = cast("dict[str, Any]", profiles[label])
        quality = cast("dict[str, Any] | None", row.get("quality"))
        pass_rate = f"{float(quality['verdict_pass_rate']) * 100:.0f}%" if quality else "-"
        table.add_row(
            label,
            str(row["tasks"]),
            f"{row['output_tokens']:,}",
            f"${row['cost_usd']:.4f}",
            f"{row['mean_output_tokens_per_task']:.0f}",
            pass_rate,
        )
    cons.print(table)

    excluded = cast("dict[str, Any]", content["excluded"])
    if excluded["calls"]:
        cons.print(
            f"  [dim]Excluded (profile transition): {excluded['tasks']} task(s), "
            f"${excluded['cost_usd']:.4f} - attribution is per profile or not at all[/dim]"
        )

    comparisons = cast("list[dict[str, Any]]", content["comparisons"])
    if comparisons:
        cons.print("\n[bold]Comparable cohorts[/bold] (same role+model, both sides >= N tasks)")
        for comp in comparisons:
            cons.print(
                f"  {comp['profile_a']} vs {comp['profile_b']} ({comp['role']}/{comp['model']}): "
                f"${comp['mean_cost_usd_per_task_a']:.4f} vs ${comp['mean_cost_usd_per_task_b']:.4f} per task"
            )
            linked = (evidence or {}).get(f"{comp['profile_a']} vs {comp['profile_b']}")
            if linked:
                cons.print(f"    [dim]eval evidence: {linked['artifact_name']}[/dim]")
    else:
        cons.print(
            f"\n  [dim]insufficient comparable runs (need >= {content['min_comparable_tasks']} "
            f"tasks per profile with matching role and model)[/dim]"
        )

    ledger_block = cast("dict[str, Any]", content["ledger"])
    cons.print(f"\n  Report sha256:  {sha256}")
    cons.print(f"  Ledger lines:   {ledger_block['line_count']} (digest {ledger_block['lines_sha256'][:16]}...)")
    cons.print(f"  Artifact:       {artifact}")
    cons.print("  [dim]Appended to the audit chain as cost.profile_report[/dim]")


@cost_cmd.command("profile-report")
@click.option("--last", "last", type=str, default=None, help="Time range: 1h, 24h, 7d, 30d. Default: whole ledger.")
@click.option(
    "--metrics-dir",
    default=".sdd/metrics",
    show_default=True,
    help="Directory containing metrics JSONL files (quality-outcome join).",
)
@click.option(
    "--ledger",
    "ledger_path",
    type=str,
    default=".sdd/cost/ledger.jsonl",
    show_default=True,
    help="Path to the rolling spend ledger JSONL.",
)
@click.option(
    "--transitions",
    "transitions_path",
    type=str,
    default=".sdd/cost/profile_transitions.jsonl",
    show_default=True,
    help="Path to the profile_transition event records.",
)
@click.option(
    "--audit-dir",
    "audit_dir",
    type=str,
    default=".sdd/audit",
    show_default=True,
    help="Audit chain directory the report event is appended to.",
)
@click.option(
    "--reports-dir",
    "reports_dir",
    type=str,
    default=".sdd/reports/cost_profiles",
    show_default=True,
    help="Directory the content-addressed report artifact is written to.",
)
@click.option(
    "--eval-ab-dir",
    "eval_ab_dir",
    type=str,
    default=".sdd/reports/eval_ab",
    show_default=True,
    help="Directory holding eval ab comparison artifacts; cross-profile claims link the latest one per pair.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def cost_profile_report_cmd(
    last: str | None,
    metrics_dir: str,
    ledger_path: str,
    transitions_path: str,
    audit_dir: str,
    reports_dir: str,
    eval_ab_dir: str,
    as_json: bool,
) -> None:
    """Emit a content-addressed per-profile cost report (issue #2245).

    The report is computed from recorded ledger entries only, hashed
    over canonical JSON with no timestamps in the payload, written as
    ``<sha256>.json``, and appended to the audit chain - a third party
    holding the ledger can recompute it byte-identically.

    \b
      bernstein cost profile-report --last 7d
      bernstein cost profile-report --json
    """
    from bernstein.core.cost.profile_attribution import load_transitions
    from bernstein.core.cost.profile_report import build_profile_report, write_report_artifact
    from bernstein.core.security.audit_chain import AuditChainStore, record_cost_profile_report

    cutoff = _parse_time_range(last) if last else 0.0
    window_label = last or "all"

    mdir = Path(metrics_dir)
    task_records = _load_tasks_jsonl(mdir) if mdir.exists() else []
    task_records = _load_archive_tasks(mdir.parent) + task_records if mdir.exists() else task_records

    report = build_profile_report(
        ledger_path=Path(ledger_path),
        task_records=task_records,
        transitions=load_transitions(Path(transitions_path)),
        window_label=window_label,
        cutoff=cutoff,
    )
    artifact = write_report_artifact(report, Path(reports_dir))

    ledger_block = cast("dict[str, Any]", report.content["ledger"])
    try:
        chain = AuditChainStore(Path(audit_dir))
        record_cost_profile_report(
            chain=chain,
            report_sha256=report.sha256,
            ledger_lines_sha256=str(ledger_block["lines_sha256"]),
            ledger_first_line_sha256=str(ledger_block["first_line_sha256"]),
            ledger_last_line_sha256=str(ledger_block["last_line_sha256"]),
            ledger_line_count=int(ledger_block["line_count"]),
            window=window_label,
            artifact_name=report.artifact_name,
        )
    except Exception as exc:
        # The report is only trustworthy once anchored; refuse to
        # pretend otherwise.
        if as_json or is_json():
            print_json({"error": f"Audit chain append failed: {exc}", "artifact": str(artifact)})
        else:
            console.print(f"[red]Audit chain append failed:[/red] {exc}")
        raise SystemExit(1) from exc

    # Evidence links live outside ``content`` so the report's hashed
    # payload (and its byte-identical recomputability) is untouched.
    comparisons = cast("list[dict[str, Any]]", report.content["comparisons"])
    evidence = _profile_comparison_evidence(Path(eval_ab_dir), comparisons)

    if as_json or is_json():
        print_json(
            {
                "artifact": str(artifact),
                "sha256": report.sha256,
                "content": report.content,
                "comparison_evidence": evidence,
            }
        )
        return

    _render_profile_report_human(console, report.content, report.sha256, artifact, evidence)


# ---------------------------------------------------------------------------
# `bernstein cost-envelopes` subcommand group (issue #1405)
# ---------------------------------------------------------------------------


def _load_envelope_rollup_from_ledger(
    ledger_path: Path,
    envelopes_cfg: dict[str, dict[str, Any]],
    cutoff: float,
) -> dict[str, dict[str, Any]]:
    """Build a per-envelope rollup from the spend ledger.

    Falls back to an empty mapping when the ledger is missing. The
    operator-supplied ``envelopes_cfg`` is consulted for caps + model
    allowlists; envelopes seen in the ledger but absent from config show
    up as uncapped buckets so dashboards never lose attribution.
    """
    from bernstein.core.cost.cost_rollup_by_envelope import rollup
    from bernstein.core.cost.cost_tracker import EnvelopeConfig, TokenUsage
    from bernstein.core.cost.spend_ledger import SpendLedger

    if not ledger_path.exists():
        return {}
    entries = SpendLedger.load_entries(ledger_path)
    if cutoff > 0:
        entries = [e for e in entries if e.ts >= cutoff]
    records = [
        TokenUsage(
            input_tokens=e.input_tokens,
            output_tokens=e.output_tokens,
            model=e.model,
            cost_usd=e.cost_usd,
            agent_id=e.agent_id or "unknown",
            task_id=e.task_id or "unknown",
            timestamp=e.ts,
            cache_read_tokens=e.cache_read_tokens,
            cache_write_tokens=e.cache_write_tokens,
            quota_envelope=e.quota_envelope or "subscription",
        )
        for e in entries
    ]
    envelope_objs = {name: EnvelopeConfig.from_dict(name, cfg) for name, cfg in envelopes_cfg.items()}
    rows = rollup(records, envelope_objs)
    return {name: row.to_dict() for name, row in rows.items()}


def _read_envelopes_from_yaml(yaml_path: Path) -> dict[str, dict[str, Any]]:
    """Parse the ``cost.envelopes`` block from ``bernstein.yaml``.

    Returns an empty mapping when the file is missing, malformed, or
    lacks the block. PyYAML is imported lazily so the CLI module stays
    cheap to load.
    """
    if not yaml_path.exists():
        return {}
    try:
        import yaml  # local import: optional for non-CLI callers

        data_raw: object = yaml.safe_load(yaml_path.read_text()) or {}
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data_raw, dict):
        return {}
    data = cast("dict[str, Any]", data_raw)
    cost_block_raw: object = data.get("cost") or {}
    if not isinstance(cost_block_raw, dict):
        return {}
    cost_block = cast("dict[str, Any]", cost_block_raw)
    envelopes_block_raw: object = cost_block.get("envelopes") or {}
    if not isinstance(envelopes_block_raw, dict):
        return {}
    envelopes_block = cast("dict[str, Any]", envelopes_block_raw)
    out: dict[str, dict[str, Any]] = {}
    for name, payload in envelopes_block.items():
        if isinstance(payload, dict):
            payload_d = cast("dict[str, Any]", payload)
            out[name] = payload_d.copy()
    return out


@click.group("cost-envelopes")
def cost_envelopes_group() -> None:
    """Inspect per-quota-envelope cost attribution (issue #1405)."""


@cost_envelopes_group.command("show")
@click.option(
    "--ledger",
    "ledger_path",
    type=str,
    default=".sdd/cost/ledger.jsonl",
    show_default=True,
    help="Path to the rolling spend ledger JSONL.",
)
@click.option(
    "--config",
    "config_path",
    type=str,
    default="bernstein.yaml",
    show_default=True,
    help="Path to the bernstein.yaml file holding ``cost.envelopes``.",
)
@click.option("--last", "last", type=str, default=None, help="Time range: 1h, 24h, 7d, 30d.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def cost_envelopes_show_cmd(
    ledger_path: str,
    config_path: str,
    last: str | None,
    as_json: bool,
) -> None:
    """Render the per-envelope rollup table."""
    cutoff = _parse_time_range(last) if last else 0.0
    envelopes_cfg = _read_envelopes_from_yaml(Path(config_path))
    rollup_data = _load_envelope_rollup_from_ledger(Path(ledger_path), envelopes_cfg, cutoff)

    if as_json or is_json():
        print_json(
            {
                "ledger": ledger_path,
                "config": config_path,
                "envelopes": rollup_data,
            }
        )
        return

    if not rollup_data:
        console.print(
            "[dim]No envelope data found. Configure ``cost.envelopes`` in bernstein.yaml "
            "and run at least one task to populate the ledger.[/dim]"
        )
        return

    from rich.table import Table

    table = Table(title="Bernstein Cost Envelopes", header_style="bold cyan", show_lines=False)
    table.add_column("Envelope", min_width=18, no_wrap=True)
    table.add_column("Spent", justify="right", min_width=10)
    table.add_column("Cap", justify="right", min_width=10)
    table.add_column("Pct", justify="right", min_width=6)
    table.add_column("Hard cap", justify="right", min_width=10)
    table.add_column("Calls", justify="right", min_width=6)
    table.add_column("Status", min_width=10)

    for name in sorted(rollup_data):
        row = rollup_data[name]
        cap = float(row.get("cap", 0.0) or 0.0)
        hard_cap = float(row.get("hard_cap", 0.0) or 0.0)
        spent = float(row.get("total_spend", 0.0) or 0.0)
        pct = float(row.get("pct_used", 0.0) or 0.0)
        calls = int(row.get("calls", 0) or 0)
        if row.get("hard_breached"):
            status = "[red]HARD BREACH[/red]"
        elif row.get("threshold_reached"):
            status = "[yellow]threshold[/yellow]"
        else:
            status = "[green]ok[/green]"
        table.add_row(
            name,
            f"${spent:.4f}",
            f"${cap:.4f}" if cap > 0 else "-",
            f"{pct * 100:.0f}%" if cap > 0 else "-",
            f"${hard_cap:.4f}" if hard_cap > 0 else "-",
            str(calls),
            status,
        )
    console.print(table)


__all__ = ["cost_cmd", "cost_envelopes_group", "cost_profile_report_cmd", "estimate_cmd"]
