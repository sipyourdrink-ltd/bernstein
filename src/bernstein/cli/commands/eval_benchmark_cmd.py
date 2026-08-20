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

    workdir = Path()
    subset_literal = cast("Literal['lite', 'full']", subset)
    runner = SWEBenchRunner(workdir=workdir, sample=sample, instance_id=instance_id, subset=subset_literal)

    dpath = Path(dataset_path) if dataset_path else None
    instances = runner.load_dataset(dpath)

    if not instances:
        console.print(
            "[yellow]No instances found. Pass --dataset <path.jsonl> or install the 'datasets' package.[/yellow]"
        )
        raise SystemExit(1)

    console.print(f"[bold]SWE-Bench evaluation[/bold]: subset={subset} • {len(instances)} instance(s)")

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


def _run_programbench_command(
    *,
    adapter: str,
    subset: str,
    tasks_limit: int | None,
    task_id: str | None,
    dataset_path: str | None,
    out_json: str | None,
    save: bool,
) -> None:
    """Run the ProgramBench harness and print a report.

    Args:
        adapter: Adapter slug for invocation.
        subset: Dataset subset slug.
        tasks_limit: Optional sample size cap.
        task_id: Optional single task id to evaluate.
        dataset_path: Optional local JSONL path.
        out_json: Optional path to write JSON output.
        save: Whether to persist results under ``.sdd/``.
    """
    import json as _json

    from rich.table import Table

    from bernstein.benchmark.programbench import (
        ProgramBenchHarness,
        TaskResult,
        compute_report,
        report_to_dict,
        save_results,
    )

    workdir = Path()
    harness = ProgramBenchHarness(
        workdir=workdir,
        sample=tasks_limit,
        task_id=task_id,
        subset=subset,
    )

    dpath = Path(dataset_path) if dataset_path else None
    tasks = harness.load_dataset(dpath)

    if not tasks:
        console.print(
            "[yellow]No ProgramBench tasks found. Pass --dataset <path.jsonl>, "
            "set BERNSTEIN_PROGRAMBENCH_DATASET, or install the 'datasets' package.[/yellow]"
        )
        raise SystemExit(1)

    console.print(f"[bold]ProgramBench evaluation[/bold]: subset={subset} • adapter={adapter} • {len(tasks)} task(s)")

    table = Table(title="ProgramBench Results", header_style=_STYLE_BOLD_CYAN, show_lines=False)
    table.add_column("Task", style="dim", min_width=24)
    table.add_column("Adapter", min_width=12)
    table.add_column("Score", justify="right", min_width=10)
    table.add_column("Asserts", justify="right", min_width=10)
    table.add_column("Cost (USD)", justify="right", min_width=12)
    table.add_column("Time (s)", justify="right", min_width=10)

    results: list[TaskResult] = []
    for task in tasks:
        console.print(f"  Running [cyan]{task.task_id}[/cyan]…", end="")
        result = harness.run_task(adapter, task)
        results.append(result)
        if result.fully_solved:
            icon = "[green]100%[/green]"
        elif result.score >= 0.5:
            icon = f"[yellow]{result.score:.0%}[/yellow]"
        else:
            icon = f"[red]{result.score:.0%}[/red]"
        console.print(f" {icon}")
        table.add_row(
            task.task_id,
            result.adapter,
            f"{result.score:.2f}",
            f"{result.asserts_passed}/{result.asserts_total}",
            f"${result.cost_usd:.4f}",
            f"{result.duration_seconds:.1f}",
        )

    report = compute_report(results)
    console.print(table)
    console.print(
        f"\n[bold]Mean partial credit:[/bold] {report.mean_partial_credit:.2%}  "
        f"[green]{report.fully_solved} fully[/green]  "
        f"[yellow]{report.near_solved} near[/yellow]  "
        f"[red]{report.failed} failed[/red]  "
        f"[dim]total cost ${report.total_cost_usd:.4f}[/dim]"
    )

    if report.per_adapter_breakdown:
        adapter_table = Table(title="Per-Adapter Breakdown", header_style="bold magenta", show_lines=False)
        adapter_table.add_column("Adapter", min_width=12)
        adapter_table.add_column("Total", justify="right")
        adapter_table.add_column("Fully", justify="right")
        adapter_table.add_column("Near", justify="right")
        adapter_table.add_column("Failed", justify="right")
        adapter_table.add_column("Mean", justify="right")
        adapter_table.add_column("Cost/Task", justify="right")
        for b in report.per_adapter_breakdown:
            adapter_table.add_row(
                b.adapter,
                str(b.total),
                str(b.fully_solved),
                str(b.near_solved),
                str(b.failed),
                f"{b.mean_partial_credit:.2%}",
                f"${b.cost_per_task:.4f}",
            )
        console.print(adapter_table)

    if out_json:
        Path(out_json).write_text(_json.dumps(report_to_dict(report), indent=2), encoding="utf-8")
        console.print(f"[dim]JSON report saved → {out_json}[/dim]")

    if save:
        sdd_dir = Path(".sdd")
        save_results(report, sdd_dir)
        console.print(f"[dim]Results saved → {sdd_dir / 'metrics' / 'programbench_results.jsonl'}[/dim]")


@benchmark_group.command("programbench")
@click.option(
    "--adapter",
    required=True,
    help="Adapter slug (e.g. claude, codex, mock).",
)
@click.option(
    "--subset",
    default="lite",
    show_default=True,
    help="ProgramBench subset slug.",
)
@click.option(
    "--tasks",
    "tasks_limit",
    type=int,
    default=None,
    help="Evaluate a random sample of N tasks.",
)
@click.option("--task", "task_id", default=None, help="Evaluate a single task by ID.")
@click.option("--dataset", "dataset_path", default=None, help="Path to local JSONL dataset file.")
@click.option(
    "--out",
    "out_json",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write JSON report to this path.",
)
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Persist results to .sdd/metrics/programbench_results.jsonl.",
)
def benchmark_programbench(
    adapter: str,
    subset: str,
    tasks_limit: int | None,
    task_id: str | None,
    dataset_path: str | None,
    out_json: str | None,
    save: bool,
) -> None:
    """Run Bernstein against ProgramBench tasks with partial-credit scoring.

    \b
      bernstein eval programbench --adapter claude
      bernstein eval programbench --adapter mock --tasks 5
      bernstein eval programbench --adapter claude --task programbench-001
      bernstein eval programbench --adapter claude --out report.json
    """
    _run_programbench_command(
        adapter=adapter,
        subset=subset,
        tasks_limit=tasks_limit,
        task_id=task_id,
        dataset_path=dataset_path,
        out_json=out_json,
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
    table = Table(title=f"Benchmarks (tier={tier})", header_style=_STYLE_BOLD_CYAN, show_lines=False)
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
      bernstein eval compare                                   # default tasks
      bernstein eval compare --tasks-dir path/to/tasks         # custom tasks
      bernstein eval compare --mode single --mode orchestrated # explicit modes
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

    console.print(f"[bold]Comparative benchmark[/bold]: {len(tasks)} task(s), modes: {', '.join(modes)}")

    suite = ComparativeBenchmark(tasks=tasks, workdir=Path())
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
      bernstein eval simulate                             # all tasks, seed=42
      bernstein eval simulate --seed 1                   # different seed
      bernstein eval simulate --task-id bugfix-1         # single task
      bernstein eval simulate --baseline prior.jsonl     # detect regressions
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
    run, report = ReproducibleBenchmark(tasks=tasks, config=config).run_and_compare()

    # --- Summary table ---
    table = Table(title=f"Benchmark simulation (seed={seed})", header_style=_STYLE_BOLD_CYAN, show_lines=False)
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
# eval - multiplicative scoring harness
# ---------------------------------------------------------------------------


@benchmark_group.group("receipt")
def benchmark_receipt_group() -> None:
    """Emit and verify signed benchmark-score trajectory receipts (#2925).

    A trajectory receipt seals the exact replayable run that produced a
    published benchmark score into a content-addressed, spine-anchored
    envelope.  Third-party verifiers can re-derive the score from the embedded
    per-task components without trusting the printed number.
    """


@benchmark_receipt_group.command("emit")
@click.argument("run_id")
@click.option("--workdir", default=".", show_default=True, help="Project root.", type=click.Path(exists=True))
def benchmark_receipt_emit(run_id: str, workdir: str) -> None:
    """Seal a benchmark run into a signed trajectory receipt.

    RUN_ID is the benchmark run identifier recorded when the run was executed.
    The receipt is written under .sdd/eval/bench/ and anchored in the
    eval-bench lineage spine.

    Example:

        bernstein eval receipt emit run-2025-07-26-001
    """
    import json
    import math
    from pathlib import Path
    from typing import NoReturn

    from bernstein.core.security.audit import AuditKeyPermissionError, load_or_create_audit_key
    from bernstein.eval.metrics import EvalScoreComponents, TierScores
    from bernstein.eval.trajectory_receipt import (
        TaskTrajectoryAnchor,
        build_trajectory_receipt,
        trajectory_receipt_path,
    )

    _workdir = Path(workdir)
    try:
        key = load_or_create_audit_key()
    except (OSError, AuditKeyPermissionError) as exc:
        console.print(f"[red]Failed to load audit key: {exc}[/red]")
        raise SystemExit(1) from exc

    # Load persisted run record from .sdd/benchmarks/benchmark_runs.jsonl
    runs_path = _workdir / ".sdd" / "benchmarks" / "benchmark_runs.jsonl"
    if not runs_path.is_file():
        console.print(f"[red]No benchmark runs found at {runs_path}[/red]")
        raise SystemExit(1)

    run_record: dict | None = None
    with runs_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("run_id") == run_id:
                run_record = rec
                break

    if run_record is None:
        console.print(f"[red]Run {run_id!r} not found in {runs_path}[/red]")
        raise SystemExit(1)

    # Build TaskTrajectoryAnchor list from the persisted run record.
    # Reject any task that has no real journal head — placeholder zero-hashes
    # would seal a receipt that verifies clean with no real trajectory behind
    # it, which is exactly the fabrication mode the receipt is designed to
    # detect.
    _ZERO_HASH = "sha256:" + "0" * 64
    _REQUIRED_TASK_FIELDS = ("task_id", "journal_head_hash", "events_content_hash", "model_id", "config_fingerprint")
    _COMPONENT_FIELDS = ("task_success", "code_quality", "efficiency", "reliability", "safety")

    def _reject(message: str) -> NoReturn:
        console.print(f"[red]{message}[/red]")
        raise SystemExit(1)

    def _required_scalar(mapping: object, field: str, where: str) -> float:
        if not isinstance(mapping, dict) or field not in mapping:
            _reject(f"{where} is missing {field!r}; refusing to substitute a default into a signed receipt.")
        value = mapping[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            _reject(f"{where} has a non-numeric or non-finite {field!r}: {value!r}")
        return float(value)

    task_anchors: list[TaskTrajectoryAnchor] = []
    for position, task in enumerate(run_record.get("tasks", [])):
        where = f"Task at index {position}"
        if not isinstance(task, dict):
            _reject(f"{where} is not an object.")
        for field in _REQUIRED_TASK_FIELDS:
            value = task.get(field)
            if not isinstance(value, str) or not value:
                _reject(f"{where} is missing a usable {field!r}; refusing to invent one.")
        where = f"Task {task['task_id']!r}"
        for field in ("journal_head_hash", "events_content_hash"):
            if task[field] == _ZERO_HASH:
                _reject(
                    f"{where} carries a placeholder {field!r}; a receipt sealed over placeholder "
                    f"anchors verifies clean with no trajectory behind it."
                )
        components = task.get("components")
        task_anchors.append(
            TaskTrajectoryAnchor(
                task_id=task["task_id"],
                journal_head_hash=task["journal_head_hash"],
                events_content_hash=task["events_content_hash"],
                model_id=task["model_id"],
                config_fingerprint=task["config_fingerprint"],
                components=EvalScoreComponents(
                    **{field: _required_scalar(components, field, where) for field in _COMPONENT_FIELDS}
                ),
            )
        )

    pt = run_record.get("per_tier")
    per_tier = TierScores(
        **{tier: _required_scalar(pt, tier, "per_tier") for tier in ("smoke", "standard", "stretch", "adversarial")}
    )

    receipt = build_trajectory_receipt(
        run_id=run_id,
        task_anchors=task_anchors,
        per_tier=per_tier,
        workdir=_workdir,
        lineage_root=_workdir / ".sdd" / "lineage",
        hmac_key=key,
    )

    path = trajectory_receipt_path(_workdir, receipt.receipt_hash)
    console.print(f"[green]Trajectory receipt emitted:[/green] {receipt.receipt_hash}")
    console.print(f"  Tasks:  {len(receipt.task_anchors)}")
    console.print(f"  Score:  {receipt.published_score:.4f}")
    console.print(f"  Status: {receipt.status}")
    console.print(f"  Path:   {path}")


@benchmark_receipt_group.command("verify")
@click.argument("receipt_hash")
@click.option("--workdir", default=".", show_default=True, help="Project root.", type=click.Path(exists=True))
def benchmark_receipt_verify(receipt_hash: str, workdir: str) -> None:
    """Verify a trajectory receipt offline.

    RECEIPT_HASH is the sha256: content hash printed by 'emit'.
    Re-derives the benchmark score from the embedded per-task components;
    exits non-zero if any step fails.

    Example:

        bernstein eval receipt verify sha256:abc123...
    """
    from pathlib import Path

    from bernstein.core.security.audit import AuditKeyPermissionError, load_or_create_audit_key
    from bernstein.eval.trajectory_receipt import verify_trajectory_receipt

    _workdir = Path(workdir)
    try:
        key = load_or_create_audit_key()
    except (OSError, AuditKeyPermissionError) as exc:
        console.print(f"[red]Failed to load audit key: {exc}[/red]")
        raise SystemExit(1) from exc

    result = verify_trajectory_receipt(
        workdir=_workdir,
        lineage_root=_workdir / ".sdd" / "lineage",
        hmac_key=key,
        receipt_hash=receipt_hash,
    )

    if result.ok:
        r = result.receipt
        console.print(f"[green]✓ Trajectory receipt verified:[/green] {receipt_hash[:32]}…")
        if r is not None:
            console.print(f"  Run:    {r.run_id}")
            console.print(f"  Tasks:  {len(r.task_anchors)}")
            console.print(f"  Score:  {r.published_score:.4f}")
            console.print(f"  Status: {r.status}")
    else:
        task_idx = result.failing_task_index
        loc = f" (task [{task_idx}])" if task_idx >= 0 else ""
        console.print(f"[red]✗ Trajectory receipt FAILED{loc}:[/red] {result.reason}")
        raise SystemExit(1)


@click.group("eval", invoke_without_command=True)
@click.option(
    "--reliability",
    "reliability_k",
    type=click.IntRange(min=1),
    default=None,
    metavar="K",
    help=(
        "Run each suite task K times under fixed coordination and emit a signed "
        "pass^k reliability receipt — an alias for 'bernstein bench run "
        "--reliability K'. Verify the receipt with 'bernstein bench "
        "reliability-verify'; probe coordination byte-identity with 'bernstein "
        "bench reliability-check'."
    ),
)
@click.option(
    "--suite",
    "reliability_suite",
    default="golden-v1",
    show_default=True,
    help="Suite name or .json path (--reliability mode only).",
)
@click.option(
    "--out",
    "reliability_out",
    default="reliability.json",
    show_default=True,
    help="Output path for the reliability receipt (--reliability mode only).",
)
@click.option(
    "--scheduler",
    "reliability_scheduler",
    default="default",
    show_default=True,
    help="Scheduler name to embed in the receipt (--reliability mode only).",
)
@click.option(
    "--stub-signer",
    "reliability_stub_signer",
    is_flag=True,
    default=False,
    help="Use the stub signer instead of the install identity (--reliability mode only, for testing).",
)
@click.pass_context
def eval_group(
    ctx: click.Context,
    reliability_k: int | None,
    reliability_suite: str,
    reliability_out: str,
    reliability_scheduler: str,
    reliability_stub_signer: bool,
) -> None:
    """Evaluation harness with multiplicative scoring.

    \b
      bernstein eval --reliability 5        # pass^k reliability floor
      bernstein eval run                    # golden suite run

    --reliability K is a thin alias for 'bernstein bench run --reliability K'
    (issue #2933): same runner, same signed reliability receipt. Verify with
    'bernstein bench reliability-verify' and probe coordination byte-identity
    with 'bernstein bench reliability-check'.
    """
    if ctx.invoked_subcommand is not None:
        if reliability_k is not None:
            raise click.UsageError(
                "--reliability runs the pass^k reliability floor directly; "
                "it cannot be combined with an eval subcommand."
            )
        return
    if reliability_k is None:
        # Preserve the pre-alias behaviour of a bare `bernstein eval`:
        # show help and exit 2 (Click's no-args-is-help path).
        click.echo(ctx.get_help())
        ctx.exit(2)
    # Delegate to the exact code path `bernstein bench run --reliability K`
    # uses: same ReliabilityRunner, same signed ReliabilityReceipt, same
    # verification story. No reliability logic lives on the eval surface.
    from bernstein.eval.bench.bench_cli import bench_run

    ctx.invoke(
        bench_run,
        suite=reliability_suite,
        out=reliability_out,
        scheduler=reliability_scheduler,
        stub_signer=reliability_stub_signer,
        reliability_k=reliability_k,
    )


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
@click.argument("spec", required=False, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--tier",
    type=click.Choice(["smoke", "standard", "stretch", "adversarial"]),
    default=None,
    help="Run only tasks from this tier.",
)
@click.option("--compare", "compare_prev", is_flag=True, default=False, help="Compare vs previous run.")
@click.option("--save/--no-save", default=True, show_default=True, help="Persist results to disk.")
@click.option(
    "--output",
    "output_json",
    type=click.Path(dir_okay=False),
    default=None,
    help="When SPEC is given, also write the JSON report to this path.",
)
def eval_run(
    spec: str | None,
    tier: str | None,
    compare_prev: bool,
    save: bool,
    output_json: str | None,
) -> None:
    """Run the golden benchmark suite or a YAML eval spec.

    \b
      bernstein eval run                       # run full golden suite
      bernstein eval run --tier smoke          # smoke tier only
      bernstein eval run --compare             # compare vs previous run
      bernstein eval run path/to/eval.yaml     # YAML spec run
    """
    if spec is not None:
        _run_yaml_spec(spec_path=spec, save=save, output_json=output_json)
        return

    from rich.table import Table

    from bernstein.eval.harness import EvalHarness, TaskEvalResult

    workdir = Path()
    state_dir = workdir / ".sdd"
    harness = EvalHarness(state_dir=state_dir, repo_root=workdir)

    tier_filter: Tier | None = tier  # type: ignore[assignment]
    tasks = harness.load_golden_tasks(tier_filter=tier_filter)

    if not tasks:
        console.print("[yellow]No golden tasks found.[/yellow]")
        console.print(f"[dim]Expected at: {state_dir / 'eval' / 'golden'}/<tier>/*.md[/dim]")
        raise SystemExit(1)

    console.print(f"[bold]Eval harness[/bold]: {len(tasks)} golden task(s)")

    # Evaluate each task (with empty telemetry for now - real runs
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


def _run_yaml_spec(*, spec_path: str, save: bool, output_json: str | None) -> None:
    """Execute a YAML eval spec and surface a Rich-formatted summary."""
    from rich.table import Table

    from bernstein.eval.yaml_runner import (
        YAMLRunner,
        lineage_stub_for,
        load_spec,
        save_report,
    )

    path = Path(spec_path).resolve()
    eval_spec = load_spec(path)
    report = YAMLRunner().run(eval_spec, base_dir=path.parent)

    console.print(f"[bold]YAML eval[/bold]: {eval_spec.name} (adapters={len(eval_spec.adapters)})")

    table = Table(title="Per-adapter results", header_style=_STYLE_BOLD_CYAN, show_lines=False)
    table.add_column("Adapter", min_width=14)
    table.add_column("Prompts", justify="right")
    table.add_column("Golden pass", justify="right")
    table.add_column("Golden %", justify="right")
    table.add_column("Judge mean", justify="right")
    table.add_column("Overall", justify="right")

    for agg in report.per_adapter:
        table.add_row(
            agg.adapter,
            str(agg.prompt_count),
            str(agg.golden_passed),
            f"{agg.golden_pass_rate * 100:.1f}%",
            f"{agg.judge_mean:.3f}",
            f"{agg.overall_score:.3f}",
        )

    console.print(table)

    if report.threshold_failures:
        console.print("\n[bold red]Threshold failures:[/bold red]")
        for failure in report.threshold_failures:
            console.print(f"  [red]-[/red] {failure}")

    if save:
        state_dir = Path(".sdd")
        json_path, md_path = save_report(report, state_dir=state_dir)
        stub = lineage_stub_for(json_path, lineage_tag=report.lineage_tag)
        lineage_path = json_path.with_suffix(".lineage.json")
        lineage_path.write_text(
            __import__("json").dumps(stub.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        console.print(f"[dim]JSON  -> {json_path}[/dim]")
        if md_path is not None:
            console.print(f"[dim]MD    -> {md_path}[/dim]")
        console.print(f"[dim]Lineage -> {lineage_path}[/dim]")

    if output_json:
        Path(output_json).write_text(report.to_json() + "\n", encoding="utf-8")
        console.print(f"[dim]Wrote JSON report -> {output_json}[/dim]")

    if not report.thresholds_passed:
        raise SystemExit(1)


@eval_group.command("list")
@click.option(
    "--state-dir",
    "state_dir",
    type=click.Path(file_okay=False),
    default=".sdd",
    show_default=True,
    help="Bernstein state directory.",
)
def eval_list(state_dir: str) -> None:
    """List persisted YAML eval runs (newest first).

    \b
      bernstein eval list
    """
    from bernstein.eval.yaml_runner import list_runs

    runs = list_runs(Path(state_dir))
    if not runs:
        console.print("[yellow]No YAML eval runs found.[/yellow]")
        return
    # The paths go through click.echo rather than the Rich console because they
    # are machine-consumable tokens, and Rich would corrupt them two ways: it
    # wraps at the console width (80 whenever stdout is not a terminal, which is
    # exactly the piped case) and it consumes a "[...]" path segment as markup.
    # The message above is prose for a human and stays on the console.
    for path in runs:
        click.echo(f"  {path}")


@eval_group.command("diff")
@click.argument("run_a", type=click.Path(exists=True, dir_okay=False))
@click.argument("run_b", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--output",
    "output_json",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write the diff JSON to this path (stdout if omitted).",
)
def eval_diff(run_a: str, run_b: str, output_json: str | None) -> None:
    """Diff two persisted YAML eval runs.

    \b
      bernstein eval diff .sdd/eval/yaml_runs/yaml_run_A.json .sdd/eval/yaml_runs/yaml_run_B.json
    """
    import json as _json

    from bernstein.eval.yaml_runner import diff_runs

    diff = diff_runs(Path(run_a), Path(run_b))
    payload = _json.dumps(diff.to_dict(), indent=2, sort_keys=True)
    if output_json:
        Path(output_json).write_text(payload + "\n", encoding="utf-8")
        console.print(f"[dim]Wrote diff -> {output_json}  winner={diff.winner}[/dim]")
    else:
        click.echo(payload)


@eval_group.command("report")
def eval_report() -> None:
    """Generate a markdown report from the most recent eval run."""
    from bernstein.eval.harness import EvalHarness

    workdir = Path()
    state_dir = workdir / ".sdd"
    prev = EvalHarness(state_dir=state_dir, repo_root=workdir).load_previous_run()
    if not prev:
        console.print(_NO_EVAL_RUNS_MSG)
        raise SystemExit(1)

    console.print(f"[bold]Eval report[/bold]: score: {prev.score:.4f}")

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

    workdir = Path()
    runs_dir = workdir / ".sdd" / "eval" / "runs"

    if not runs_dir.is_dir():
        console.print(_NO_EVAL_RUNS_MSG)
        raise SystemExit(1)

    run_files = sorted(runs_dir.glob("eval_run_*.json"), reverse=True)
    if not run_files:
        console.print(_NO_EVAL_RUNS_MSG)
        raise SystemExit(1)

    failures = json_mod.loads(run_files[0].read_text(encoding="utf-8")).get("failures", [])

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


@eval_group.command("sync-incidents")
@click.option("--workdir", default=".", type=click.Path(exists=True), help="Project root.")
@click.option("--dry-run", is_flag=True, default=False, help="Print what would be created without writing files.")
def eval_sync_incidents(workdir: str, dry_run: bool) -> None:
    """Convert dead-letter and post-mortem incidents into eval cases.

    \b
      bernstein eval sync-incidents              # write new YAML cases
      bernstein eval sync-incidents --dry-run    # preview only
    """
    from bernstein.eval.incident_synthesizer import IncidentSynthesizer

    root = Path(workdir).resolve()
    result = IncidentSynthesizer(root).sync(dry_run=dry_run)

    if dry_run:
        console.print(f"[bold]Dry run[/bold]: {len(result.created)} case(s) would be created:")
    else:
        console.print(f"[bold]{len(result.created)} new incident eval case(s) emitted[/bold]")
    for case in result.created:
        sev_color = {"P0": "red", "P1": "yellow", "P2": "dim"}.get(case.severity, "white")
        console.print(f"  [{sev_color}]{case.severity}[/{sev_color}] {case.id}  ← {case.source_incident}")
    console.print(
        f"[dim]skipped duplicates={result.skipped_duplicates} unredactable={result.skipped_unredactable}[/dim]",
    )


@eval_group.command("synth-list")
def eval_synth_list() -> None:
    """List the synthetic scenario registry with declared axes."""
    from bernstein.eval.scenario_generator import is_disabled, list_scenarios

    if is_disabled():
        console.print("[yellow]Synthetic eval generator disabled via BERNSTEIN_SYNTHETIC_EVAL_OFF.[/yellow]")
        return

    rows = list_scenarios()
    if not rows:
        console.print("[yellow]No synthetic scenarios registered.[/yellow]")
        return

    from rich.table import Table

    table = Table(title="Synthetic scenarios", header_style=_STYLE_BOLD_CYAN)
    table.add_column("ID", min_width=18)
    table.add_column("Severity", min_width=10)
    table.add_column("Axes", min_width=40)
    for row in rows:
        axes_repr = ", ".join(f"{k}={list(v)}" for k, v in row["axes"].items())
        table.add_row(row["id"], row["severity"], axes_repr)
    console.print(table)


@eval_group.command("synth-generate")
@click.option("--scenario", "scenario_id", required=True, help="Scenario id from the registry.")
@click.option("--params", "params_str", default="", show_default=False, help="Override axes (k=v,...).")
@click.option("--count", "count", type=int, default=1, show_default=True, help="Number of cases to emit.")
@click.option("--seed", "seed", type=int, default=42, show_default=True, help="Base seed for determinism.")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Output directory (default: eval/golden_data/synthetic).",
)
def eval_synth_generate(
    scenario_id: str,
    params_str: str,
    count: int,
    seed: int,
    out_dir: str | None,
) -> None:
    """Generate N synthetic eval cases for a single scenario.

    \b
      bernstein eval synth-generate --scenario large_diff --count 3
      bernstein eval synth-generate --scenario flaky_tests --params flake_rate=0.3 --count 5
    """
    from bernstein.eval.scenario_generator import (
        DEFAULT_OUT_DIR,
        is_disabled,
        materialise_and_write,
        parse_param_string,
    )

    if is_disabled():
        console.print("[yellow]Synthetic eval generator disabled via BERNSTEIN_SYNTHETIC_EVAL_OFF.[/yellow]")
        return

    try:
        params = parse_param_string(params_str)
    except ValueError as exc:
        console.print(f"[red]Invalid --params:[/red] {exc}")
        raise SystemExit(2) from exc

    out_path = Path(out_dir) if out_dir else Path().joinpath(*DEFAULT_OUT_DIR)

    try:
        cases, written = materialise_and_write(
            scenario_id,
            params=params,
            count=count,
            seed=seed,
            out_dir=out_path,
        )
    except (KeyError, ValueError) as exc:
        console.print(f"[red]Generation failed:[/red] {exc}")
        raise SystemExit(2) from exc

    console.print(f"[bold]Synthetic eval[/bold]: {len(cases)} case(s) materialised, {len(written)} written")
    for path in written:
        console.print(f"  [green]wrote[/green] {path}")


@eval_group.command("generate-scenarios")
@click.option(
    "--from-traces",
    "from_traces",
    type=int,
    default=5,
    show_default=True,
    help="How many of the latest .sdd/traces/*.jsonl to scan.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Output directory (default: eval/golden_data/synthetic).",
)
@click.option("--seed", "seed", type=int, default=42, show_default=True, help="Base seed for determinism.")
def eval_generate_scenarios(from_traces: int, out_dir: str | None, seed: int) -> None:
    """Generate synthetic scenarios from production trace patterns.

    \b
      bernstein eval generate-scenarios --from-traces 10
      bernstein eval generate-scenarios --from-traces 5 --out eval/cases/synthetic
    """
    from bernstein.eval.scenario_generator import DEFAULT_OUT_DIR, generate_from_traces, is_disabled

    if is_disabled():
        console.print("[yellow]Synthetic eval generator disabled via BERNSTEIN_SYNTHETIC_EVAL_OFF.[/yellow]")
        return

    workdir = Path.cwd()
    out_path = Path(out_dir) if out_dir else workdir.joinpath(*DEFAULT_OUT_DIR)

    result = generate_from_traces(
        workdir=workdir,
        out_dir=out_path,
        from_traces=from_traces,
        seed=seed,
    )

    console.print(
        f"[bold]Synthetic eval[/bold]: {len(result.created)} new case(s) "
        f"[dim](skipped duplicates={result.skipped_duplicates}, "
        f"invalid traces={result.skipped_invalid_traces})[/dim]"
    )
    for case in result.created:
        sev_color = {"P0": "red", "P1": "yellow", "P2": "dim"}.get(case.severity, "white")
        console.print(f"  [{sev_color}]{case.severity}[/{sev_color}] {case.id}  scenario={case.scenario}")


@eval_group.command("ab")
@click.option(
    "--variant-a",
    "variant_a_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="YAML file with the A variant (keys: name, prompt, [model], [metadata]).",
)
@click.option(
    "--variant-b",
    "variant_b_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="YAML file with the B variant.",
)
@click.option(
    "--tasks",
    "tasks_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="YAML file with a top-level 'tasks: [...]' list.",
)
@click.option(
    "--suite",
    "suite_path",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Suite YAML ('tasks: [...]') for the profile-arm mode.",
)
@click.option(
    "--arm-a",
    "arm_a",
    default=None,
    help="Response profile for arm A (three-arm mode: 'baseline' or 'balanced').",
)
@click.option(
    "--arm-b",
    "arm_b",
    default=None,
    help="Response profile for arm B (the candidate in three-arm mode).",
)
@click.option(
    "--arms",
    "arm_count",
    type=click.IntRange(2, 3),
    default=2,
    show_default=True,
    help="2 compares the named profiles; 3 adds the unset baseline and the built-in minimal-control arm.",
)
@click.option(
    "--trials",
    "trials",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help="Trials per (arm, task) pair.",
)
@click.option(
    "--executor",
    "executor_name",
    type=click.Choice(["synthetic", "spawn"]),
    default="synthetic",
    show_default=True,
    help="'synthetic' is deterministic and offline; 'spawn' runs each arm as a real task in an isolated worktree.",
)
@click.option("--model", "model", default=None, help="Model pin for spawned arms and model label in the artifact.")
@click.option("--role", "role", default="backend", show_default=True, help="Agent role for spawned arms.")
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=1800,
    show_default=True,
    help="Max seconds to wait per spawned task.",
)
@click.option(
    "--ledger",
    "ledger_path",
    type=click.Path(dir_okay=False),
    default=None,
    help=(
        "Spend ledger the runs are joined against "
        "(defaults: synthetic -> .sdd/eval/ab/ledger.jsonl, spawn -> .sdd/cost/ledger.jsonl)."
    ),
)
@click.option(
    "--reports-dir",
    "reports_dir",
    type=click.Path(file_okay=False),
    default=".sdd/reports/eval_ab",
    show_default=True,
    help="Directory the content-addressed comparison artifact is written to.",
)
@click.option(
    "--audit-dir",
    "audit_dir",
    type=click.Path(file_okay=False),
    default=".sdd/audit",
    show_default=True,
    help="Audit chain directory the comparison event is appended to.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON (suite mode).")
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write comparison JSON here (stdout if omitted).",
)
@click.option(
    "--scorer",
    type=click.Choice(["exact", "none"]),
    default="exact",
    show_default=True,
    help="Built-in scorer for the variant mode (synthetic-friendly).",
)
def eval_ab(
    variant_a_path: str | None,
    variant_b_path: str | None,
    tasks_path: str | None,
    suite_path: str | None,
    arm_a: str | None,
    arm_b: str | None,
    arm_count: int,
    trials: int,
    executor_name: str,
    model: str | None,
    role: str,
    timeout_seconds: int,
    ledger_path: str | None,
    reports_dir: str,
    audit_dir: str,
    as_json: bool,
    output_path: str | None,
    scorer: str,
) -> None:
    """Run an A/B comparison; emit a deterministic comparison JSON.

    Two modes:

    Variant mode (--variant-a/--variant-b/--tasks) compares two prompt
    variants with the deterministic ``echo_executor`` - offline, zero
    LLM cost.

    Suite mode (--suite/--arm-a/--arm-b) runs the suite under two
    response profiles and emits one chain-anchored artifact carrying
    both the cost delta (ledger-referenced) and the quality delta.
    With ``--arms 3`` the candidate is measured against a minimal
    built-in control arm (the honest delta) as well as the unset
    baseline (shown, labelled conflated).

    \b
      bernstein eval ab --variant-a a.yaml --variant-b b.yaml --tasks tasks.yaml
      bernstein eval ab --suite suite.yaml --arm-a balanced --arm-b terse --json
      bernstein eval ab --suite suite.yaml --arm-a baseline --arm-b terse --arms 3 --trials 3
      bernstein eval ab --suite suite.yaml --arm-a balanced --arm-b terse --executor spawn
    """
    suite_mode = suite_path is not None or arm_a is not None or arm_b is not None
    if suite_mode:
        if not (suite_path and arm_a and arm_b):
            raise click.UsageError("suite mode requires --suite, --arm-a, and --arm-b together")
        _run_profile_ab_command(
            suite_path=suite_path,
            arm_a=arm_a,
            arm_b=arm_b,
            arm_count=arm_count,
            trials=trials,
            executor_name=executor_name,
            model=model,
            role=role,
            timeout_seconds=timeout_seconds,
            ledger_path=ledger_path,
            reports_dir=reports_dir,
            audit_dir=audit_dir,
            as_json=as_json,
            output_path=output_path,
        )
        return

    if not (variant_a_path and variant_b_path and tasks_path):
        raise click.UsageError(
            "provide --variant-a/--variant-b/--tasks (variant mode) or --suite/--arm-a/--arm-b (suite mode)"
        )

    from bernstein.eval.ab_runner import (
        echo_executor,
        exact_match_scorer,
        load_tasks_yaml,
        load_variant_yaml,
        run_ab,
    )

    variant_a = load_variant_yaml(Path(variant_a_path))
    variant_b = load_variant_yaml(Path(variant_b_path))
    tasks = load_tasks_yaml(Path(tasks_path))

    chosen_scorer = exact_match_scorer if scorer == "exact" else None

    comparison = run_ab(
        variant_a,
        variant_b,
        tasks,
        executor=echo_executor,
        scorer=chosen_scorer,
    )

    payload = comparison.to_json()
    if output_path:
        Path(output_path).write_text(payload + "\n", encoding="utf-8")
        console.print(f"[green]wrote[/green] {output_path}  winner={comparison.winner}")
    else:
        click.echo(payload)


def _run_profile_ab_command(
    *,
    suite_path: str,
    arm_a: str,
    arm_b: str,
    arm_count: int,
    trials: int,
    executor_name: str,
    model: str | None,
    role: str,
    timeout_seconds: int,
    ledger_path: str | None,
    reports_dir: str,
    audit_dir: str,
    as_json: bool,
    output_path: str | None,
) -> None:
    """Run the suite under the arm plan and emit the comparison artifact.

    The artifact is content-addressed canonical JSON, written under
    *reports_dir*, appended to the audit chain, and registered in the
    pair index that ``bernstein cost profile-report`` links from.
    """
    import json as _json

    from bernstein import __version__
    from bernstein.core.security.audit_chain import AuditChainStore, record_eval_ab_comparison
    from bernstein.eval.ab_comparison import (
        append_comparison_index,
        build_arms,
        build_comparison_artifact,
        run_arms,
        spawn_arm_executor,
        suite_file_sha256,
        synthetic_arm_executor,
        write_comparison_artifact,
    )
    from bernstein.eval.ab_runner import load_tasks_yaml

    suite = Path(suite_path)
    tasks = load_tasks_yaml(suite)
    if not tasks:
        console.print("[yellow]Suite has no tasks.[/yellow]")
        raise SystemExit(1)

    try:
        plan = build_arms(arm_a, arm_b, arms=arm_count, workdir=Path())
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    if executor_name == "spawn":
        from bernstein.cli.helpers import SERVER_URL

        lpath = Path(ledger_path) if ledger_path else Path(".sdd/cost/ledger.jsonl")
        executor = spawn_arm_executor(
            SERVER_URL,
            role=role,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    else:
        from bernstein.core.cost.spend_ledger import SpendLedger

        # Synthetic figures never land in the production spend ledger.
        lpath = Path(ledger_path) if ledger_path else Path(".sdd/eval/ab/ledger.jsonl")
        executor = synthetic_arm_executor(SpendLedger(path=lpath, run_id="eval-ab"))

    rows = run_arms(plan, tasks, executor=executor, trials=trials)
    artifact = build_comparison_artifact(
        plan=plan,
        rows=rows,
        ledger_path=lpath,
        suite_sha256=suite_file_sha256(suite),
        suite_name=suite.name,
        adapter_versions={"bernstein": __version__},
        trials=trials,
        model=model,
    )
    artifact_path = write_comparison_artifact(artifact, Path(reports_dir))

    winner = artifact.content["winner"]
    try:
        chain = AuditChainStore(Path(audit_dir))
        record_eval_ab_comparison(
            chain=chain,
            artifact_sha256=artifact.sha256,
            suite_sha256=str(artifact.content["suite_sha256"]),
            profile_a_sha256=str(artifact.content["profile_a_sha256"]),
            profile_b_sha256=str(artifact.content["profile_b_sha256"]),
            arm_count=len(artifact.content["arms"]),
            row_count=len(artifact.content["per_task"]),
            winner_arm=str(winner["arm"]),
            artifact_name=artifact.artifact_name,
        )
    except Exception as exc:
        # The artifact is only trustworthy once anchored; refuse to
        # pretend otherwise.
        if as_json:
            click.echo(_json.dumps({"error": f"Audit chain append failed: {exc}", "artifact": str(artifact_path)}))
        else:
            console.print(f"[red]Audit chain append failed:[/red] {exc}")
        raise SystemExit(1) from exc

    append_comparison_index(Path(reports_dir), profile_pair=plan.profile_pair, artifact=artifact)

    payload = _json.dumps(
        {"artifact": str(artifact_path), "sha256": artifact.sha256, "content": artifact.content},
        indent=2,
        sort_keys=True,
    )
    if output_path:
        Path(output_path).write_text(payload + "\n", encoding="utf-8")
    if as_json:
        click.echo(payload)
        return

    _render_profile_ab_human(artifact.content, artifact.sha256, artifact_path)


def _render_profile_ab_human(content: dict[str, object], sha256: str, artifact_path: Path) -> None:
    """Rich rendering of the suite-mode comparison artifact."""
    from typing import cast

    from rich.table import Table

    aggregates = cast("dict[str, dict[str, object]]", content["aggregates"])
    table = Table(title="Profile A/B comparison", header_style=_STYLE_BOLD_CYAN, show_lines=False)
    table.add_column("Arm", min_width=12)
    table.add_column("Profile", min_width=10)
    table.add_column("Runs", justify="right")
    table.add_column("Pass rate", justify="right")
    table.add_column("Median tokens", justify="right")
    table.add_column("Median USD", justify="right")

    arms = cast("dict[str, dict[str, object]]", content["arms"])
    for arm_name, agg in aggregates.items():
        pass_rate = agg["pass_rate"]
        median_tokens = agg["median_tokens"]
        median_usd = agg["median_usd"]
        table.add_row(
            arm_name,
            str(arms[arm_name]["profile"] or "(unset)"),
            str(agg["runs"]),
            f"{pass_rate:.1%}" if isinstance(pass_rate, (int, float)) else "not measured",
            f"{median_tokens:.0f}" if isinstance(median_tokens, (int, float)) else "not measured",
            f"${median_usd:.6f}" if isinstance(median_usd, (int, float)) else "not measured",
        )
    console.print(table)

    deltas = cast("dict[str, dict[str, object]]", content["deltas"])
    for name, delta in deltas.items():
        label = "conflated" if delta["conflated"] else "honest"
        console.print(
            f"  {name} [{label}]: pass_rate {delta['pass_rate_delta']}  "
            f"median_usd {delta['median_usd_delta']}  median_tokens {delta['median_tokens_delta']}"
        )

    winner = cast("dict[str, object]", content["winner"])
    console.print(f"\n[bold]Winner:[/bold] {winner['arm']}  [dim]{winner['reason']}[/dim]")
    if winner["missing"]:
        console.print(f"  [yellow]missing:[/yellow] {', '.join(cast('list[str]', winner['missing']))}")
    console.print(f"\n  Artifact sha256: {sha256}")
    console.print(f"  Artifact:        {artifact_path}")
    console.print("  [dim]Appended to the audit chain as eval.ab_comparison[/dim]")


# ---------------------------------------------------------------------------
# eval scenario - scenario-style evals (precision/recall, e.g. security-pentest)
# ---------------------------------------------------------------------------


_DEFAULT_EVAL_SCENARIOS_DIR = Path(__file__).resolve().parents[4] / "eval" / "scenarios"


@eval_group.command("scenario")
@click.argument("scenario_id")
@click.option("--model", "model", default="mock", show_default=True, help="Model name passed to the adapter.")
@click.option(
    "--adapters",
    "adapters_csv",
    default=None,
    help=(
        "Comma-separated list of adapter slugs (e.g. 'a,b'). When supplied, "
        "the scenario fans out across the listed adapters and the consensus "
        "is reported alongside the per-adapter split."
    ),
)
@click.option(
    "--scenarios-dir",
    "scenarios_dir",
    type=click.Path(file_okay=False, exists=True),
    default=None,
    help="Override the eval scenarios directory (defaults to <repo>/eval/scenarios).",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write the JSON result to this path (stdout if omitted).",
)
def eval_scenario(
    scenario_id: str,
    model: str,
    adapters_csv: str | None,
    scenarios_dir: str | None,
    output_path: str | None,
) -> None:
    """Run a precision/recall eval scenario from ``eval/scenarios/``.

    Currently supports the ``security-pentest`` scenario. The scenario
    runs the configured adapter against the synthetic codebase and emits
    a JSON precision/recall/F1 report. Exit code is 0 when all configured
    thresholds are met and 1 otherwise.

    \b
      bernstein eval scenario security-pentest --model mock
      bernstein eval scenario security-pentest --model sonnet --output run.json
      bernstein eval scenario security-pentest --adapters mock,mock
    """
    import json as _json

    from bernstein.eval.pentest_runner import (
        PentestAdapter,
        load_scenario_config,
        mock_adapter,
        run_multi_adapter,
        run_scenario,
    )
    from bernstein.eval.pentest_scorer import PentestScorer

    base_dir = Path(scenarios_dir) if scenarios_dir else _DEFAULT_EVAL_SCENARIOS_DIR
    slug = scenario_id.replace("-", "_")
    candidates = [
        base_dir / f"{slug}.yaml",
        base_dir / f"{scenario_id}.yaml",
    ]
    chosen: Path | None = next((c for c in candidates if c.exists()), None)
    if chosen is None:
        console.print(f"[red]Scenario not found:[/red] {scenario_id} (searched {base_dir})")
        raise SystemExit(1)

    config = load_scenario_config(chosen)

    if adapters_csv:
        # Multi-adapter fan-out path.
        names = [n.strip() for n in adapters_csv.split(",") if n.strip()]
        if not names:
            console.print("[red]--adapters supplied but resolved to an empty list[/red]")
            raise SystemExit(1)
        # Resolve adapter slugs to callables. Today only ``mock`` is
        # available out-of-the-box; production callers register their
        # own slugs via plugin loading. Disambiguate duplicate slugs
        # so the call_order tuple remains unique.
        resolved: dict[str, PentestAdapter] = {}
        slug_counts: dict[str, int] = {}
        for slug_name in names:
            base = slug_name
            count = slug_counts.get(base, 0)
            slug_counts[base] = count + 1
            key = base if count == 0 else f"{base}-{count + 1}"
            # All slugs currently map to the mock adapter; production
            # registry wiring happens in a follow-up ticket.
            resolved[key] = mock_adapter
        multi = run_multi_adapter(adapters=resolved, config=config)
        split = PentestScorer().score_multi(multi)
        envelope: dict[str, object] = multi.to_dict()
        envelope["per_adapter_score"] = {
            name: {
                "precision": round(score.precision, 6),
                "recall": round(score.recall, 6),
                "f1": round(score.f1, 6),
            }
            for name, score in split.per_adapter.items()
        }
        envelope["consensus_score"] = {
            "precision": round(split.consensus.precision, 6),
            "recall": round(split.consensus.recall, 6),
            "f1": round(split.consensus.f1, 6),
        }
        payload = _json.dumps(envelope, indent=2, sort_keys=True)
        if output_path:
            Path(output_path).write_text(payload + "\n", encoding="utf-8")
            console.print(
                f"[green]wrote[/green] {output_path}  "
                f"adapters={','.join(multi.call_order)}  "
                f"consensus_p={split.consensus.precision:.2f} "
                f"consensus_r={split.consensus.recall:.2f}"
            )
        else:
            click.echo(payload)
        return

    result = run_scenario(config, model=model)
    payload = _json.dumps(result.to_dict(), indent=2, sort_keys=True)
    if output_path:
        Path(output_path).write_text(payload + "\n", encoding="utf-8")
        verdict = "PASS" if result.passed else "FAIL"
        console.print(
            f"[green]wrote[/green] {output_path}  {verdict}  "
            f"p={result.score.precision:.2f} r={result.score.recall:.2f} f1={result.score.f1:.2f}"
        )
    else:
        click.echo(payload)
    if not result.passed:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# eval calibration - Brier + ECE report over the on-disk calibration log
# ---------------------------------------------------------------------------


@eval_group.group("calibration")
def calibration_group() -> None:
    """Inspect calibration of router/judge probability outputs."""


@calibration_group.command("report")
@click.option(
    "--since",
    "since",
    default=None,
    help="Restrict to records within this duration (e.g. '7d', '30m', '24h').",
)
@click.option(
    "--kind",
    "decision_kind",
    default=None,
    help="Filter records by decision_kind (e.g. 'model_route').",
)
@click.option(
    "--log-path",
    "log_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Override the calibration JSONL log path.",
)
@click.option(
    "--bins",
    "bin_count",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="Number of reliability buckets.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write report JSON to this file (stdout if omitted).",
)
def calibration_report(
    since: str | None,
    decision_kind: str | None,
    log_path: str | None,
    bin_count: int,
    output_path: str | None,
) -> None:
    """Print a Brier + ECE + reliability report for the calibration log.

    \b
      bernstein eval calibration report --since 7d
      bernstein eval calibration report --since 24h --kind model_route
    """
    import json as _json

    from bernstein.eval.calibration import (
        DEFAULT_LOG_PATH,
        compute_report,
        load_log,
        parse_duration,
    )

    path = Path(log_path) if log_path else DEFAULT_LOG_PATH
    since_seconds = parse_duration(since) if since else None
    records = load_log(path, since_seconds=since_seconds, decision_kind=decision_kind)
    report = compute_report(
        records,
        bin_count=bin_count,
        decision_kind=decision_kind,
        since=since,
    )
    payload = _json.dumps(report.to_dict(), indent=2, sort_keys=True)
    if output_path:
        Path(output_path).write_text(payload + "\n", encoding="utf-8")
        console.print(f"[green]wrote[/green] {output_path}  decisions={report.decisions}")
    else:
        click.echo(payload)


# ---------------------------------------------------------------------------
# eval gate - statistical eval gating: signed verdict receipts, deterministic
# stage promotion, offline verification (#2520)
# ---------------------------------------------------------------------------


def _load_result_set(path: str) -> dict[str, bool]:
    """Load a per-task pass/fail JSON object from ``path``."""
    import json as _json

    raw = _json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"result set {path} must be a JSON object mapping task id -> bool"
        raise click.UsageError(msg)
    outcomes: dict[str, bool] = {}
    for task_id, passed in raw.items():
        if not isinstance(passed, bool):
            msg = f"result set {path}: task {task_id!r} must map to a JSON boolean"
            raise click.UsageError(msg)
        outcomes[str(task_id)] = passed
    return outcomes


@eval_group.command("gate")
@click.option(
    "--baseline",
    "baseline_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSON object mapping task id -> bool for the baseline arm.",
)
@click.option(
    "--candidate",
    "candidate_path",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSON object mapping task id -> bool for the candidate arm.",
)
@click.option("--candidate-id", "candidate_id", default="candidate", show_default=True, help="Candidate config id.")
@click.option("--baseline-id", "baseline_id", default="baseline", show_default=True, help="Baseline config id.")
@click.option(
    "--workdir",
    "workdir",
    type=click.Path(file_okay=False),
    default=".",
    show_default=True,
    help="Project root holding .sdd/eval/gate receipts and .sdd/lineage.",
)
@click.option(
    "--audit-dir",
    "audit_dir",
    type=click.Path(file_okay=False),
    default=".sdd/audit",
    show_default=True,
    help="Audit chain directory the verdict is mirrored into.",
)
@click.option("--alpha", "alpha", type=float, default=None, help="Significance level (default 0.05).")
@click.option("--margin", "margin", type=float, default=None, help="Non-inferiority margin (default 0.05).")
@click.option("--min-n", "min_n", type=int, default=None, help="Minimum n per arm (default 12).")
@click.option("--timestamp", "timestamp", type=int, default=0, show_default=True, help="Stable receipt timestamp.")
@click.option("--no-audit", "no_audit", is_flag=True, default=False, help="Do not mirror into the audit chain.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def eval_gate_cmd(
    baseline_path: str,
    candidate_path: str,
    candidate_id: str,
    baseline_id: str,
    workdir: str,
    audit_dir: str,
    alpha: float | None,
    margin: float | None,
    min_n: int | None,
    timestamp: int,
    no_audit: bool,
    as_json: bool,
) -> None:
    """Emit a signed verdict receipt for two paired result sets (#2520).

    The verdict is a pure function of the paired 2x2 discordance table, so the
    same two result sets in any ingestion order produce a byte-identical
    receipt. A gate invoked below the minimum n per arm refuses a promoting
    verdict with an explicit machine-readable reason.

    \b
      bernstein eval gate --baseline base.json --candidate cand.json
      bernstein eval gate --baseline base.json --candidate cand.json --min-n 20
    """
    import json as _json

    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.eval.gate_receipt import build_verdict_receipt
    from bernstein.eval.significance import (
        DEFAULT_ALPHA,
        DEFAULT_MIN_N,
        DEFAULT_NON_INFERIORITY_MARGIN,
    )

    baseline_outcomes = _load_result_set(baseline_path)
    candidate_outcomes = _load_result_set(candidate_path)

    root = Path(workdir)
    chain = None if no_audit else AuditChainStore(Path(audit_dir))
    try:
        receipt = build_verdict_receipt(
            baseline_outcomes=baseline_outcomes,
            candidate_outcomes=candidate_outcomes,
            candidate_config_id=candidate_id,
            baseline_config_id=baseline_id,
            workdir=root,
            lineage_root=root / ".sdd" / "lineage",
            hmac_key=load_or_create_audit_key(),
            timestamp=timestamp,
            alpha=DEFAULT_ALPHA if alpha is None else alpha,
            non_inferiority_margin=DEFAULT_NON_INFERIORITY_MARGIN if margin is None else margin,
            min_n=DEFAULT_MIN_N if min_n is None else min_n,
            chain=chain,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    evidence = receipt.evidence
    if as_json:
        click.echo(_json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
        return

    console.print(f"[bold]Verdict:[/bold] {receipt.verdict.value}  [dim]({evidence.reason})[/dim]")
    console.print(f"  receipt_hash: {receipt.receipt_hash}")
    console.print(
        f"  n/arm: {evidence.n_candidate}  effect: {evidence.effect:+.4f}  "
        f"interval: [{evidence.interval_low:+.4f}, {evidence.interval_high:+.4f}]"
    )
    console.print(
        f"  base_rate: {evidence.base_rate:.4f}  cand_rate: {evidence.cand_rate:.4f}  "
        f"alpha: {evidence.alpha}  min_n_satisfied: {evidence.min_n_satisfied}"
    )
    if not evidence.min_n_satisfied:
        console.print(
            f"  [yellow]below minimum n[/yellow] ({evidence.n_candidate} < {evidence.min_n}): "
            "a promoting verdict is refused."
        )


@eval_group.command("promotions")
@click.option(
    "--workdir",
    "workdir",
    type=click.Path(file_okay=False),
    default=".",
    show_default=True,
    help="Project root holding .sdd/eval/gate receipts.",
)
@click.option("--candidate-id", "candidate_id", default=None, help="Filter to one candidate config id.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def eval_promotions_cmd(workdir: str, candidate_id: str | None, as_json: bool) -> None:
    """Project the promotion stage history from the verdict receipt chain (#2520).

    The stage assignment at every prefix of the chain is recomputed from the
    receipts alone, with no auxiliary state file: the receipt chain IS the
    assignment.

    \b
      bernstein eval promotions
      bernstein eval promotions --candidate-id my-template --json
    """
    import json as _json

    from bernstein.eval.gate_receipt import read_verdict_receipt
    from bernstein.eval.promotion import project, steps_from_receipts

    gate_dir = Path(workdir) / ".sdd" / "eval" / "gate"
    receipts = []
    if gate_dir.is_dir():
        for path in gate_dir.glob("sha256:*.json"):
            receipt = read_verdict_receipt(Path(workdir), path.stem)
            if receipt is None:
                continue
            if candidate_id is not None and receipt.candidate_config_id != candidate_id:
                continue
            receipts.append(receipt)

    receipts.sort(key=lambda r: (r.timestamp, r.receipt_hash))
    projection = project(steps_from_receipts(receipts))

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "final_stage": projection.final_stage.value,
                    "default_config_id": projection.default_config_id,
                    "stage_at_prefix": [s.value for s in projection.stage_at_prefix],
                    "revocations": [r.to_dict() for r in projection.revocations],
                    "receipts": [r.receipt_hash for r in receipts],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    if not receipts:
        console.print("[yellow]No verdict receipts found under .sdd/eval/gate.[/yellow]")
        return

    console.print(f"[bold]Promotion projection[/bold]: {len(receipts)} verdict receipt(s)")
    for index, (receipt, stage) in enumerate(zip(receipts, projection.stage_at_prefix, strict=True)):
        console.print(f"  {index}: {receipt.verdict.value:24s} -> {stage.value}  [dim]{receipt.receipt_hash}[/dim]")
    console.print(f"\n[bold]Final stage:[/bold] {projection.final_stage.value}")
    console.print(f"[bold]Default config:[/bold] {projection.default_config_id}")
    for revocation in projection.revocations:
        console.print(
            f"  [red]rollback[/red] at step {revocation.step_index}: "
            f"revoked {len(revocation.revoked_receipt_hashes)} receipt(s), "
            f"reverts to {revocation.reverts_to_stage}"
        )


@eval_group.command("gate-verify")
@click.argument("receipt_hash")
@click.option(
    "--workdir",
    "workdir",
    type=click.Path(file_okay=False),
    default=".",
    show_default=True,
    help="Project root holding .sdd/eval/gate receipts and .sdd/lineage.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def eval_gate_verify_cmd(receipt_hash: str, workdir: str, as_json: bool) -> None:
    """Verify a verdict receipt offline against the lineage spine (#2520).

    Re-derives the receipt hash from the stored body and re-derives the verdict
    from the embedded evidence, so a receipt whose evidence does not entail its
    verdict fails even when its hashes are internally consistent.

    \b
      bernstein eval gate-verify sha256:<hash>
    """
    import json as _json

    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.eval.gate_receipt import verify_verdict_receipt

    root = Path(workdir)
    result = verify_verdict_receipt(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=load_or_create_audit_key(),
        receipt_hash=receipt_hash,
    )
    if as_json:
        click.echo(_json.dumps({"ok": result.ok, "reason": result.reason, "receipt_hash": receipt_hash}))
        raise SystemExit(0 if result.ok else 1)
    if result.ok:
        console.print(f"[green]Verdict receipt verified:[/green] {receipt_hash}")
        return
    console.print(f"[red]Verdict receipt verification failed:[/red] {result.reason}")
    raise SystemExit(1)


@eval_group.group("clean-run")
def eval_clean_run_group() -> None:
    """Clean-run attestation surface (#2930)."""


@eval_clean_run_group.command("verify")
@click.argument("attestation_hash")
@click.option(
    "--workdir",
    "workdir",
    type=click.Path(file_okay=False),
    default=".",
    show_default=True,
    help=("Project root holding .sdd/eval/clean_run attestations, .sdd/runs/<run-id>/journal.jsonl, and .sdd/lineage."),
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def eval_clean_run_verify_cmd(attestation_hash: str, workdir: str, as_json: bool) -> None:
    """Verify a clean-run attestation offline (#2930).

    Re-derives the verdict from the embedded activity digests and contraband
    commitment (the stored verdict is never trusted), re-checks that the
    scanned activity set chains to the recorded journal head, and re-checks
    the eval-clean-run lineage-spine anchor.

    \b
      bernstein eval clean-run verify sha256:<hash>
    """
    import json as _json

    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.eval.clean_run import verify_clean_run_attestation

    root = Path(workdir)
    result = verify_clean_run_attestation(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=load_or_create_audit_key(),
        attestation_hash=attestation_hash,
    )
    verdict = result.attestation.verdict if result.attestation is not None else ""
    if as_json:
        click.echo(
            _json.dumps(
                {
                    "ok": result.ok,
                    "reason": result.reason,
                    "attestation_hash": attestation_hash,
                    "verdict": verdict,
                }
            )
        )
        raise SystemExit(0 if result.ok else 1)
    if result.ok:
        console.print(f"[green]Clean-run attestation verified:[/green] {attestation_hash} (verdict: {verdict})")
        return
    console.print(f"[red]Clean-run attestation verification failed:[/red] {result.reason}")
    raise SystemExit(1)


# Every subcommand the deprecated ``benchmark`` group carries needs a home on
# ``eval`` before the group is unregistered in v4.0.0, otherwise the removal
# deletes a capability rather than a spelling.  ``run`` and ``swe-bench``
# already existed on ``eval``; the remaining four are registered here as the
# *same* Command objects, so the two spellings cannot drift.
#
# ``simulate`` is not the top-level ``bernstein simulate``: that command is a
# digital-twin simulation of a plan against historical traces (#1374), while
# this one replays the standard benchmark task set for throughput/cost/quality
# (disjoint options, disjoint inputs, disjoint outputs).  They share a verb and
# nothing else, so the top-level command is not a migration target for it.
eval_group.add_command(benchmark_programbench, "programbench")
eval_group.add_command(benchmark_compare, "compare")
eval_group.add_command(benchmark_simulate, "simulate")
eval_group.add_command(benchmark_receipt_group, "receipt")


@click.group("benchmark", help="[Deprecated] Use 'bernstein eval' instead.")
@click.pass_context
def benchmark_alias_group(ctx: click.Context) -> None:
    """[Deprecated] Use 'bernstein eval' instead."""
    if ctx.invoked_subcommand is not None:
        click.echo(
            "WARNING: 'bernstein benchmark' is deprecated and will be removed in v4.0.0 (#3143): "
            "use 'bernstein eval' instead.",
            err=True,
        )


for _cmd_name, _cmd_obj in benchmark_group.commands.items():
    benchmark_alias_group.add_command(_cmd_obj, _cmd_name)


# ---------------------------------------------------------------------------
# workspace - multi-repo workspace management
