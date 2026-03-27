"""Evaluation and benchmarking commands for Bernstein CLI.

This module contains evaluation and benchmarking groups and commands:
  benchmark_group (swe-bench, run)
  eval_group (run, report, failures)

All commands and groups are registered with the main CLI group in main.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click

from bernstein.cli.helpers import (
    console,
)

if TYPE_CHECKING:
    from bernstein.eval.golden import Tier


@click.group("benchmark")
def benchmark_group() -> None:
    """Run the tiered golden benchmark suite."""


@benchmark_group.command("swe-bench")
@click.option("--lite", "mode", flag_value="lite", default=True, help="Run SWE-Bench Lite (300 instances).")
@click.option("--sample", "sample", type=int, default=None, help="Evaluate a random sample of N instances.")
@click.option("--instance", "instance_id", default=None, help="Evaluate a single instance by ID.")
@click.option("--dataset", "dataset_path", default=None, help="Path to local JSONL dataset file.")
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Persist results to .sdd/benchmark/swe_bench_results.json.",
)
def benchmark_swe_bench(
    mode: str,
    sample: int | None,
    instance_id: str | None,
    dataset_path: str | None,
    save: bool,
) -> None:
    """Run Bernstein against SWE-Bench instances and report resolve rate.

    \b
      bernstein benchmark swe-bench --lite              # all 300 Lite instances
      bernstein benchmark swe-bench --sample 20         # random 20-instance eval
      bernstein benchmark swe-bench --instance django__django-11905
    """
    from rich.table import Table

    from bernstein.benchmark.swe_bench import InstanceResult, SWEBenchRunner, compute_report, save_results

    workdir = Path(".")
    runner = SWEBenchRunner(workdir=workdir, sample=sample, instance_id=instance_id)

    dpath = Path(dataset_path) if dataset_path else None
    instances = runner.load_dataset(dpath)

    if not instances:
        console.print(
            "[yellow]No instances found. Pass --dataset <path.jsonl> or install the 'datasets' package.[/yellow]"
        )
        raise SystemExit(1)

    console.print(f"[bold]SWE-Bench evaluation[/bold] — {len(instances)} instance(s)")

    table = Table(title="SWE-Bench Results", header_style="bold cyan", show_lines=False)
    table.add_column("Instance", style="dim", min_width=30)
    table.add_column("Resolved", min_width=10)
    table.add_column("Cost (USD)", justify="right", min_width=12)
    table.add_column("Time (s)", justify="right", min_width=10)
    table.add_column("Agents", justify="right", min_width=8)

    results: list[InstanceResult] = []
    for inst in instances:
        console.print(f"  Running [cyan]{inst.instance_id}[/cyan]…", end="")
        result = runner.run_instance(inst)
        results.append(result)
        status_icon = "[green]✓[/green]" if result.resolved else "[red]✗[/red]"
        console.print(f" {status_icon}")
        table.add_row(
            inst.instance_id,
            "[green]YES[/green]" if result.resolved else "[red]NO[/red]",
            f"${result.cost_usd:.4f}",
            f"{result.duration_seconds:.1f}",
            str(result.agent_count),
        )

    report = compute_report(results)
    console.print(table)
    console.print(
        f"\n[bold]Resolve rate:[/bold] {report.resolve_rate:.1%} "
        f"({report.resolved}/{report.total})  "
        f"[dim]median cost ${report.median_cost_usd:.4f}  "
        f"median time {report.median_duration_seconds:.0f}s[/dim]"
    )

    if save:
        sdd_dir = Path(".sdd")
        out = save_results(report, sdd_dir)
        console.print(f"[dim]Results saved → {out}[/dim]")


@benchmark_group.command("run")
@click.option(
    "--tier",
    type=click.Choice(["smoke", "capability", "stretch", "all"]),
    default="all",
    show_default=True,
    help="Which benchmark tier to run.",
)
@click.option(
    "--benchmarks-dir",
    default="tests/benchmarks",
    show_default=True,
    help="Root directory containing smoke/capability/stretch sub-dirs.",
)
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Persist results to .sdd/benchmarks/YYYY-MM-DD.jsonl.",
)
def benchmark_run(tier: str, benchmarks_dir: str, save: bool) -> None:
    """Run benchmark suite and report pass/fail per benchmark.

    \b
      bernstein benchmark run                  # run all tiers
      bernstein benchmark run --tier smoke     # smoke only
      bernstein benchmark run --tier stretch   # stretch only
    """
    from rich.table import Table

    from bernstein.evolution.benchmark import (
        run_all,
        run_selected,
        save_results,
    )

    bdir = Path(benchmarks_dir)
    if not bdir.exists():
        console.print(f"[red]Benchmarks directory not found:[/red] {bdir}")
        raise SystemExit(1)

    summary = run_all(bdir) if tier == "all" else run_selected(bdir, tier)  # type: ignore[arg-type]

    # ---- Results table ----
    table = Table(title=f"Benchmarks — tier={tier}", header_style="bold cyan", show_lines=False)
    table.add_column("ID", style="dim", min_width=14)
    table.add_column("Tier", min_width=12)
    table.add_column("Goal", min_width=40)
    table.add_column("Result", min_width=8)
    table.add_column("Duration", justify="right", min_width=10)

    for result in summary.results:
        status_str = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
        table.add_row(
            result.benchmark_id,
            result.tier,
            result.goal,
            status_str,
            f"{result.duration_seconds:.2f}s",
        )
        if not result.passed:
            for sig in result.signal_results:
                if not sig.passed:
                    table.add_row(
                        "",
                        "",
                        f"  [dim]↳ {sig.signal_type}: {sig.message}[/dim]",
                        "",
                        "",
                    )

    console.print(table)
    console.print(
        f"\n[bold]Total:[/bold] {summary.total}  "
        f"[green]{summary.passed} passed[/green]  "
        f"[red]{summary.failed} failed[/red]"
    )

    if save and summary.total > 0:
        sdd_dir = Path(".sdd")
        out = save_results(summary, sdd_dir)
        console.print(f"[dim]Results saved → {out}[/dim]")

    if summary.failed > 0:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# eval — multiplicative scoring harness
# ---------------------------------------------------------------------------


@click.group("eval")
def eval_group() -> None:
    """Evaluation harness with multiplicative scoring."""


@eval_group.command("run")
@click.option(
    "--tier",
    type=click.Choice(["smoke", "standard", "stretch", "adversarial"]),
    default=None,
    help="Run only tasks from this tier.",
)
@click.option("--compare", "compare_prev", is_flag=True, default=False, help="Compare vs previous run.")
@click.option("--save/--no-save", default=True, show_default=True, help="Persist results to disk.")
def eval_run(tier: str | None, compare_prev: bool, save: bool) -> None:
    """Run the golden benchmark suite with multiplicative scoring.

    \b
      bernstein eval run                    # run full golden suite
      bernstein eval run --tier smoke       # smoke tier only
      bernstein eval run --compare          # compare vs previous run
    """
    from rich.table import Table

    from bernstein.eval.harness import EvalHarness, TaskEvalResult

    workdir = Path(".")
    state_dir = workdir / ".sdd"
    harness = EvalHarness(state_dir=state_dir, repo_root=workdir)

    tier_filter: Tier | None = tier  # type: ignore[assignment]
    tasks = harness.load_golden_tasks(tier_filter=tier_filter)

    if not tasks:
        console.print("[yellow]No golden tasks found.[/yellow]")
        console.print(f"[dim]Expected at: {state_dir / 'eval' / 'golden'}/<tier>/*.md[/dim]")
        raise SystemExit(1)

    console.print(f"[bold]Eval harness[/bold] — {len(tasks)} golden task(s)")

    # Evaluate each task (with empty telemetry for now — real runs
    # would collect telemetry from actual agent execution)
    task_results: list[TaskEvalResult] = []
    for task in tasks:
        result = harness.evaluate_task(task)
        task_results.append(result)

    run_result = harness.compute_multiplicative_score(task_results)

    # Display results
    table = Table(title="Eval Results", header_style="bold cyan", show_lines=False)
    table.add_column("Component", min_width=15)
    table.add_column("Score", justify="right", min_width=10)

    mc = run_result.multiplicative_components
    if mc:
        table.add_row("Task Success", f"{mc.task_success:.2%}")
        table.add_row("Code Quality", f"{mc.code_quality:.2%}")
        table.add_row("Efficiency", f"{mc.efficiency:.2%}")
        table.add_row("Reliability", f"{mc.reliability:.2%}")
        table.add_row("Safety", f"{mc.safety:.2%}")
        table.add_row("", "")
        table.add_row("[bold]Final Score[/bold]", f"[bold]{mc.final_score:.4f}[/bold]")

    console.print(table)

    # Per-tier breakdown
    pt = run_result.per_tier
    if pt:
        tier_table = Table(title="Per-Tier Scores", header_style="bold cyan")
        tier_table.add_column("Tier", min_width=15)
        tier_table.add_column("Score", justify="right", min_width=10)
        tier_table.add_row("Smoke", f"{pt.smoke:.2%}")
        tier_table.add_row("Standard", f"{pt.standard:.2%}")
        tier_table.add_row("Stretch", f"{pt.stretch:.2%}")
        tier_table.add_row("Adversarial", f"{pt.adversarial:.2%}")
        console.print(tier_table)

    # Compare with previous run
    if compare_prev:
        prev = harness.load_previous_run()
        if prev:
            delta = run_result.score - prev.score
            color = "green" if delta >= 0 else "red"
            console.print(f"\n[bold]vs previous:[/bold] [{color}]{delta:+.4f}[/{color}]")
            console.print(f"[dim]Previous score: {prev.score:.4f}[/dim]")
        else:
            console.print("[dim]No previous run found for comparison.[/dim]")

    # Save results
    if save:
        path = harness.save_run(run_result)
        console.print(f"[dim]Results saved → {path}[/dim]")


@eval_group.command("report")
def eval_report() -> None:
    """Generate a markdown report from the most recent eval run."""
    from bernstein.eval.harness import EvalHarness

    workdir = Path(".")
    state_dir = workdir / ".sdd"
    harness = EvalHarness(state_dir=state_dir, repo_root=workdir)

    prev = harness.load_previous_run()
    if not prev:
        console.print("[yellow]No eval runs found.[/yellow]")
        raise SystemExit(1)

    console.print(f"[bold]Eval Report[/bold] — score: {prev.score:.4f}")

    mc = prev.multiplicative_components
    if mc:
        console.print(f"  Task Success:  {mc.task_success:.2%}")
        console.print(f"  Code Quality:  {mc.code_quality:.2%}")
        console.print(f"  Efficiency:    {mc.efficiency:.2%}")
        console.print(f"  Reliability:   {mc.reliability:.2%}")
        console.print(f"  Safety:        {mc.safety:.2%}")

    pt = prev.per_tier
    if pt:
        console.print(f"\n  Smoke:       {pt.smoke:.2%}")
        console.print(f"  Standard:    {pt.standard:.2%}")
        console.print(f"  Stretch:     {pt.stretch:.2%}")
        console.print(f"  Adversarial: {pt.adversarial:.2%}")

    if prev.cost_total > 0:
        console.print(f"\n  Total cost: ${prev.cost_total:.2f}")

    console.print(f"  Tasks evaluated: {prev.tasks_evaluated}")


@eval_group.command("failures")
def eval_failures() -> None:
    """Show failure taxonomy breakdown from the most recent eval run."""
    import json as json_mod

    from rich.table import Table

    workdir = Path(".")
    runs_dir = workdir / ".sdd" / "eval" / "runs"

    if not runs_dir.is_dir():
        console.print("[yellow]No eval runs found.[/yellow]")
        raise SystemExit(1)

    run_files = sorted(runs_dir.glob("eval_run_*.json"), reverse=True)
    if not run_files:
        console.print("[yellow]No eval runs found.[/yellow]")
        raise SystemExit(1)

    data = json_mod.loads(run_files[0].read_text(encoding="utf-8"))
    failures = data.get("failures", [])

    if not failures:
        console.print("[green]No failures in the most recent run.[/green]")
        return

    table = Table(title="Failure Taxonomy", header_style="bold red", show_lines=True)
    table.add_column("Task", min_width=20)
    table.add_column("Category", min_width=18)
    table.add_column("Details", min_width=40)

    for f in failures:
        table.add_row(
            str(f.get("task", "")),
            str(f.get("taxonomy", "")),
            str(f.get("details", "")),
        )

    console.print(table)

    # Category counts
    counts: dict[str, int] = {}
    for f in failures:
        cat = str(f.get("taxonomy", "unknown"))
        counts[cat] = counts.get(cat, 0) + 1

    console.print(f"\n[bold]Total failures:[/bold] {len(failures)}")
    for cat, count in sorted(counts.items(), key=lambda x: -x[1]):
        console.print(f"  {cat}: {count}")


# ---------------------------------------------------------------------------
# workspace — multi-repo workspace management
