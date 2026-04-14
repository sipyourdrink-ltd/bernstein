"""Evaluation and benchmarking commands for Bernstein CLI.

This module contains evaluation and benchmarking groups and commands:
  benchmark_group (swe-bench, run, compare)
  eval_group (run, report, failures)

All commands and groups are registered with the main CLI group in main.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import click

from bernstein.cli.helpers import (
    console,
)

if TYPE_CHECKING:
    from bernstein.eval.golden import Tier

_NO_EVAL_RUNS_MSG = "[yellow]No eval runs found.[/yellow]"

_STYLE_BOLD_CYAN = "bold cyan"


@click.group("benchmark")
def benchmark_group() -> None:
    """Run the tiered golden benchmark suite."""


def _run_swe_bench_command(
    *,
    subset: str,
    sample: int | None,
    instance_id: str | None,
    dataset_path: str | None,
    save: bool,
) -> None:
    """Run the SWE-Bench harness and print a report.

    Args:
        subset: Dataset subset name (for example ``"lite"``).
        sample: Optional number of instances to sample.
        instance_id: Optional single instance to evaluate.
        dataset_path: Optional local JSONL path.
        save: Whether to persist the results under ``.sdd/``.
    """
    from rich.table import Table

    from bernstein.benchmark.swe_bench import InstanceResult, SWEBenchRunner, compute_report, save_results

    workdir = Path(".")
    subset_literal = cast("Literal['lite', 'full']", subset)
    runner = SWEBenchRunner(workdir=workdir, sample=sample, instance_id=instance_id, subset=subset_literal)

    dpath = Path(dataset_path) if dataset_path else None
    instances = runner.load_dataset(dpath)

    if not instances:
        console.print(
            "[yellow]No instances found. Pass --dataset <path.jsonl> or install the 'datasets' package.[/yellow]"
        )
        raise SystemExit(1)

    console.print(f"[bold]SWE-Bench evaluation[/bold] — subset={subset} • {len(instances)} instance(s)")

    table = Table(title="SWE-Bench Results", header_style=_STYLE_BOLD_CYAN, show_lines=False)
    table.add_column("Instance", style="dim", min_width=30)
    table.add_column("Model", min_width=14)
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
            result.model_name,
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
        f"[dim]cost/task ${report.cost_per_task:.4f}  "
        f"time/task {report.time_per_task:.0f}s[/dim]"
    )

    if report.per_model_breakdown:
        model_table = Table(title="Per-Model Breakdown", header_style="bold magenta", show_lines=False)
        model_table.add_column("Model", min_width=16)
        model_table.add_column("Resolved", min_width=12)
        model_table.add_column("Resolve Rate", justify="right", min_width=12)
        model_table.add_column("Cost/Task", justify="right", min_width=12)
        model_table.add_column("Time/Task", justify="right", min_width=12)
        for breakdown in report.per_model_breakdown:
            model_table.add_row(
                breakdown.model_name,
                f"{breakdown.resolved}/{breakdown.total}",
                f"{breakdown.resolve_rate:.1%}",
                f"${breakdown.cost_per_task:.4f}",
                f"{breakdown.time_per_task:.1f}s",
            )
        console.print(model_table)

    if save:
        sdd_dir = Path(".sdd")
        save_results(report, sdd_dir)
        console.print(f"[dim]Results saved → {sdd_dir / 'metrics' / 'swe_bench_results.jsonl'}[/dim]")


@benchmark_group.command("swe-bench")
@click.option(
    "--subset",
    type=click.Choice(["lite", "full"]),
    default="lite",
    show_default=True,
    help="Which SWE-Bench subset to evaluate.",
)
@click.option("--lite", "force_lite", is_flag=True, default=False, help="Deprecated alias for --subset lite.")
@click.option("--sample", "sample", type=int, default=None, help="Evaluate a random sample of N instances.")
@click.option("--instance", "instance_id", default=None, help="Evaluate a single instance by ID.")
@click.option("--dataset", "dataset_path", default=None, help="Path to local JSONL dataset file.")
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Persist results to .sdd/metrics/swe_bench_results.jsonl.",
)
def benchmark_swe_bench(
    subset: str,
    force_lite: bool,
    sample: int | None,
    instance_id: str | None,
    dataset_path: str | None,
    save: bool,
) -> None:
    """Run Bernstein against SWE-Bench instances and report resolve rate.

    \b
      bernstein benchmark swe-bench --subset lite       # all Lite instances
      bernstein benchmark swe-bench --sample 20         # random 20-instance eval
      bernstein benchmark swe-bench --instance django__django-11905
    """
    if force_lite:
        subset = "lite"
    _run_swe_bench_command(
        subset=subset,
        sample=sample,
        instance_id=instance_id,
        dataset_path=dataset_path,
        save=save,
    )


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
    table = Table(title=f"Benchmarks — tier={tier}", header_style=_STYLE_BOLD_CYAN, show_lines=False)
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
        if result.passed:
            continue
        for sig in result.signal_results:
            if not sig.passed:
                table.add_row("", "", f"  [dim]↳ {sig.signal_type}: {sig.message}[/dim]", "", "")

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


@benchmark_group.command("compare")
@click.option(
    "--tasks-dir",
    default="templates/benchmarks",
    show_default=True,
    help="Directory containing benchmark task YAML files.",
)
@click.option(
    "--mode",
    "modes",
    multiple=True,
    type=click.Choice(["single", "orchestrated"]),
    default=("single", "orchestrated"),
    show_default=True,
    help="Execution modes to include in comparison.",
)
def benchmark_compare(tasks_dir: str, modes: tuple[str, ...]) -> None:
    """Run comparative benchmark: single-agent vs orchestrated.

    \b
      bernstein benchmark compare                                   # default tasks
      bernstein benchmark compare --tasks-dir path/to/tasks         # custom tasks
      bernstein benchmark compare --mode single --mode orchestrated # explicit modes
    """
    from bernstein.benchmark.comparative import ComparativeBenchmark, load_benchmark_tasks

    tdir = Path(tasks_dir)
    if not tdir.is_dir():
        console.print(f"[red]Tasks directory not found:[/red] {tdir}")
        raise SystemExit(1)

    tasks = load_benchmark_tasks(tdir)
    if not tasks:
        console.print("[yellow]No benchmark tasks found in directory.[/yellow]")
        raise SystemExit(1)

    console.print(f"[bold]Comparative benchmark[/bold] — {len(tasks)} task(s), modes: {', '.join(modes)}")

    suite = ComparativeBenchmark(tasks=tasks, workdir=Path("."))
    report = suite.run_suite(modes=list(modes))  # type: ignore[arg-type]

    md = suite.generate_markdown_report(report)
    from rich.markdown import Markdown

    console.print(Markdown(md))


@benchmark_group.command("simulate")
@click.option(
    "--tasks-dir",
    default="templates/benchmarks",
    show_default=True,
    help="Directory containing benchmark task YAML files.",
)
@click.option(
    "--seed",
    type=int,
    default=42,
    show_default=True,
    help="Random seed for reproducible results.",
)
@click.option(
    "--task-id",
    "task_ids",
    multiple=True,
    help="Run only these task IDs (repeatable). Default: all tasks.",
)
@click.option(
    "--baseline",
    "baseline_path",
    default=None,
    type=click.Path(),
    help="Path to a prior benchmark_runs.jsonl for regression detection.",
)
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Persist results to .sdd/benchmarks/benchmark_runs.jsonl.",
)
def benchmark_simulate(
    tasks_dir: str,
    seed: int,
    task_ids: tuple[str, ...],
    baseline_path: str | None,
    save: bool,
) -> None:
    """Run reproducible benchmark: throughput, cost, quality across standard tasks.

    Uses deterministic simulation (no live LLM calls) so results are
    comparable across runs with the same seed.

    \b
      bernstein benchmark simulate                             # all tasks, seed=42
      bernstein benchmark simulate --seed 1                   # different seed
      bernstein benchmark simulate --task-id bugfix-1         # single task
      bernstein benchmark simulate --baseline prior.jsonl     # detect regressions
    """
    from pathlib import Path as _Path

    from rich.table import Table

    from bernstein.benchmark.comparative import load_benchmark_tasks
    from bernstein.benchmark.reproducible import BenchmarkConfig, ReproducibleBenchmark

    tdir = _Path(tasks_dir)
    if not tdir.is_dir():
        console.print(f"[red]Tasks directory not found:[/red] {tdir}")
        raise SystemExit(1)

    tasks = load_benchmark_tasks(tdir)
    if not tasks:
        console.print("[yellow]No benchmark tasks found in directory.[/yellow]")
        raise SystemExit(1)

    sdd_dir = _Path(".sdd") / "benchmarks"
    bline = _Path(baseline_path) if baseline_path else None
    output_dir = sdd_dir if save else None

    config = BenchmarkConfig(
        seed=seed,
        task_ids=list(task_ids),
        baseline_path=bline,
        output_dir=output_dir,
    )
    bench = ReproducibleBenchmark(tasks=tasks, config=config)
    run, report = bench.run_and_compare()

    # --- Summary table ---
    table = Table(title=f"Benchmark simulation — seed={seed}", header_style=_STYLE_BOLD_CYAN, show_lines=False)
    table.add_column("Metric", min_width=22)
    table.add_column("Value", justify="right", min_width=18)

    t = run.throughput
    c = run.cost
    q = run.quality
    table.add_row("Tasks run", str(run.task_count))
    table.add_row("Tasks/hour", f"{t.tasks_per_hour:.1f}")
    table.add_row("p50 latency", f"{t.p50_latency_s:.1f}s")
    table.add_row("p95 latency", f"{t.p95_latency_s:.1f}s")
    table.add_row("Pass rate", f"{q.pass_rate:.1%}")
    table.add_row("Verification rate", f"{q.verification_rate:.1%}")
    table.add_row("Cost/task", f"${c.per_task_usd:.5f}")
    table.add_row("Total cost", f"${c.total_usd:.4f}")
    table.add_row("Total tokens", f"{c.total_tokens:,}")

    console.print(table)
    console.print(f"[dim]Run ID: {run.run_id}[/dim]")

    if report is not None:
        if report.is_regression:
            console.print("\n[bold red]Regression detected:[/bold red]")
            for msg in report.regressions:
                console.print(f"  [red]✗[/red] {msg}")
            raise SystemExit(1)
        else:
            delta_tph = f"{report.throughput_delta_pct:+.1f}%"
            delta_cost = f"{report.cost_delta_pct:+.1f}%"
            delta_q = f"{report.quality_delta_pp:+.1f}pp"
            console.print(
                f"\n[green]No regression[/green] vs baseline {report.baseline_run_id}  "
                f"[dim]throughput {delta_tph}  cost {delta_cost}  quality {delta_q}[/dim]"
            )

    if save:
        out = sdd_dir / "benchmark_runs.jsonl"
        console.print(f"[dim]Results saved → {out}[/dim]")


# ---------------------------------------------------------------------------
# eval — multiplicative scoring harness
# ---------------------------------------------------------------------------


@click.group("eval")
def eval_group() -> None:
    """Evaluation harness with multiplicative scoring."""


@eval_group.command("golden")
@click.option("--workdir", default=".", help="Project root.", type=click.Path(exists=True))
def eval_golden(workdir: str) -> None:
    """Run the curated golden test suite to detect orchestrator regressions."""
    import asyncio

    from rich.table import Table

    from bernstein.benchmark.golden import GoldenEvalRunner
    from bernstein.cli.helpers import SERVER_URL

    runner = GoldenEvalRunner(Path(workdir), SERVER_URL)

    console.print("[bold]Running Golden Test Suite…[/bold]\n")

    # We use asyncio.run because the CLI is synchronous but the runner might be async
    summary = asyncio.run(runner.run_suite())

    table = Table(title=f"Golden Results ({summary['timestamp']})", header_style=_STYLE_BOLD_CYAN)
    table.add_column("Task ID", style="dim")
    table.add_column("Title")
    table.add_column("Status", justify="center")
    table.add_column("Cost", justify="right")
    table.add_column("Duration", justify="right")

    for res in summary["tasks"]:
        status = "[green]PASS[/green]" if res["passed"] else "[red]FAIL[/red]"
        table.add_row(res["task_id"], res["title"], status, f"${res['cost_usd']:.4f}", f"{res['duration_s']}s")

    console.print(table)

    passed = summary["passed"]
    total = summary["total_tasks"]
    console.print(f"\n[bold]Score:[/bold] {passed}/{total} ({passed / total:.1%})")
    cost_str = f"${summary['total_cost_usd']:.4f}"
    dur_str = f"{summary['duration_s']:.1f}s"
    console.print(f"[dim]Total cost: {cost_str}  Total duration: {dur_str}[/dim]")

    if summary["failed"] > 0:
        raise SystemExit(1)


@eval_group.command("swe-bench")
@click.option(
    "--subset",
    type=click.Choice(["lite", "full"]),
    default="lite",
    show_default=True,
    help="Which SWE-Bench subset to evaluate.",
)
@click.option("--sample", "sample", type=int, default=None, help="Evaluate a random sample of N instances.")
@click.option("--instance", "instance_id", default=None, help="Evaluate a single instance by ID.")
@click.option("--dataset", "dataset_path", default=None, help="Path to local JSONL dataset file.")
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Persist results to .sdd/metrics/swe_bench_results.jsonl.",
)
def eval_swe_bench(
    subset: str,
    sample: int | None,
    instance_id: str | None,
    dataset_path: str | None,
    save: bool,
) -> None:
    """Run Bernstein against SWE-Bench from the eval command group."""
    _run_swe_bench_command(
        subset=subset,
        sample=sample,
        instance_id=instance_id,
        dataset_path=dataset_path,
        save=save,
    )


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
    table = Table(title="Eval Results", header_style=_STYLE_BOLD_CYAN, show_lines=False)
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
        tier_table = Table(title="Per-Tier Scores", header_style=_STYLE_BOLD_CYAN)
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
        console.print(_NO_EVAL_RUNS_MSG)
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
        console.print(_NO_EVAL_RUNS_MSG)
        raise SystemExit(1)

    run_files = sorted(runs_dir.glob("eval_run_*.json"), reverse=True)
    if not run_files:
        console.print(_NO_EVAL_RUNS_MSG)
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
