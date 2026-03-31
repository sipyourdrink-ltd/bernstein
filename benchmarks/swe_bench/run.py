"""CLI entry point for the SWE-Bench evaluation harness.

Usage examples
--------------
# Run all 4 scenarios on 10 instances (quick smoke-test)
uv run python benchmarks/swe_bench/run.py eval --limit 10

# Run a single scenario
uv run python benchmarks/swe_bench/run.py eval --scenarios bernstein-sonnet --limit 50

# Run all 300 instances of SWE-Bench Lite (full evaluation)
uv run python benchmarks/swe_bench/run.py eval

# Generate markdown report from saved results
uv run python benchmarks/swe_bench/run.py report

# List available scenarios
uv run python benchmarks/swe_bench/run.py list-scenarios

Prerequisites
-------------
  uv add datasets swebench      # HuggingFace datasets + SWE-Bench eval harness
  docker                         # Docker daemon (SWE-Bench test runner)
  ANTHROPIC_API_KEY              # Claude API key in environment
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import click

# ---------------------------------------------------------------------------
# Bootstrap path so the script works when invoked directly without install
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.swe_bench.harness import Harness, HarnessConfig  # noqa: E402
from benchmarks.swe_bench.metrics import ResultStore, ScenarioSummary  # noqa: E402
from benchmarks.swe_bench.report import generate_from_results_dir  # noqa: E402
from benchmarks.swe_bench.scenarios import ALL_SCENARIOS, SCENARIO_BY_NAME  # noqa: E402

from bernstein.benchmark.head_to_head import CANONICAL_COMPARISON, generate_full_report  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bernstein.swe_bench")

_DEFAULT_RESULTS_DIR = _REPO_ROOT / "benchmarks" / "swe_bench" / "results"


@click.group()
def cli() -> None:
    """SWE-Bench evaluation harness for the Bernstein scaffolding thesis."""


@cli.command()
@click.option(
    "--scenarios",
    "-s",
    multiple=True,
    type=click.Choice(list(SCENARIO_BY_NAME.keys()), case_sensitive=False),
    default=[],
    help="Scenarios to evaluate (default: all 4).",
)
@click.option(
    "--limit",
    "-n",
    type=int,
    default=None,
    help="Limit evaluation to first N instances (useful for smoke-tests).",
)
@click.option(
    "--results-dir",
    "-r",
    type=click.Path(path_type=Path),
    default=_DEFAULT_RESULTS_DIR,
    show_default=True,
    help="Directory to store per-instance JSONL results and summaries.",
)
@click.option(
    "--dataset",
    default="princeton-nlp/SWE-bench_Lite",
    show_default=True,
    help="HuggingFace dataset identifier.",
)
@click.option(
    "--agent-timeout",
    type=int,
    default=300,
    show_default=True,
    help="Per-agent timeout in seconds.",
)
@click.option(
    "--no-report",
    is_flag=True,
    default=False,
    help="Skip generating the markdown report after evaluation.",
)
def eval(
    scenarios: tuple[str, ...],
    limit: int | None,
    results_dir: Path,
    dataset: str,
    agent_timeout: int,
    no_report: bool,
) -> None:
    """Run SWE-Bench Lite evaluation across one or more scenarios."""
    selected = [SCENARIO_BY_NAME[n] for n in scenarios] if scenarios else ALL_SCENARIOS

    cfg = HarnessConfig(
        results_dir=results_dir,
        dataset=dataset,
        agent_timeout_s=agent_timeout,
    )
    harness = Harness(cfg)

    click.echo(f"\nEvaluating {len(selected)} scenario(s) on SWE-Bench Lite")
    if limit:
        click.echo(f"  Limit: {limit} instances per scenario")
    click.echo(f"  Results directory: {results_dir}\n")

    # Load instances once to share across scenarios
    try:
        from datasets import load_dataset  # type: ignore[import]

        click.echo("Loading dataset…")
        ds = load_dataset(dataset, split="test")  # pyright: ignore[reportUnknownVariableType]
        instances: list[Any] = list(ds)  # pyright: ignore[reportUnknownArgumentType]
        click.echo(f"Loaded {len(instances)} instances.\n")
    except ImportError:
        click.echo(
            "ERROR: 'datasets' package not installed.\nInstall it with: uv add datasets swebench\n",
            err=True,
        )
        sys.exit(1)

    eval_summaries: dict[str, ScenarioSummary] = {}
    for scenario in selected:
        click.echo(f"{'─' * 60}")
        click.echo(f"Scenario: {scenario.name}")
        click.echo(f"  {scenario.description}")
        click.echo(f"  Estimated cost/instance: ${scenario.estimated_cost_per_instance:.4f}")
        click.echo()

        summary = harness.run_scenario(scenario, instances=instances, limit=limit)
        eval_summaries[scenario.name] = summary

        click.echo(
            f"  Done. Resolve rate: {summary.resolve_rate * 100:.1f}% "
            f"({summary.resolved}/{summary.total_instances - summary.skipped})  "
            f"Mean cost: ${summary.mean_cost_per_instance_usd:.4f}/issue  "
            f"Total: ${summary.total_cost_usd:.2f}"
        )

    click.echo(f"\n{'═' * 60}")
    click.echo("SUMMARY")
    click.echo(f"{'═' * 60}")
    _print_summary_table(list(eval_summaries.values()))

    if not no_report:
        report_path = generate_from_results_dir(results_dir)
        click.echo(f"\nReport written to: {report_path}")


@cli.command()
@click.option(
    "--results-dir",
    "-r",
    type=click.Path(path_type=Path),
    default=_DEFAULT_RESULTS_DIR,
    show_default=True,
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Output path for the report (default: <results-dir>/report.md).",
)
def report(results_dir: Path, output: Path | None) -> None:
    """Generate a markdown report from saved evaluation results."""
    if not results_dir.exists():
        click.echo(f"ERROR: results directory not found: {results_dir}", err=True)
        sys.exit(1)

    summary_files = list(results_dir.glob("*_summary.json"))
    if not summary_files:
        click.echo(
            "No summary files found. Run 'eval' first to generate results.",
            err=True,
        )
        sys.exit(1)

    report_path = generate_from_results_dir(results_dir, output_path=output)
    click.echo(f"Report written to: {report_path}")


@cli.command(name="list-scenarios")
def list_scenarios() -> None:
    """List all available evaluation scenarios."""
    click.echo("\nAvailable scenarios:\n")
    for s in ALL_SCENARIOS:
        click.echo(f"  {s.name}")
        click.echo(f"    {s.description}")
        click.echo(f"    Agents: {', '.join(f'{a.role}({a.model})' for a in s.agents)}")
        click.echo(f"    Estimated cost/instance: ${s.estimated_cost_per_instance:.4f}")
        click.echo()


@cli.command()
@click.option(
    "--scenarios",
    "-s",
    multiple=True,
    type=click.Choice(list(SCENARIO_BY_NAME.keys()), case_sensitive=False),
    default=[],
    help="Scenarios to simulate (default: all 4).",
)
@click.option(
    "--instances",
    "-n",
    type=int,
    default=300,
    show_default=True,
    help="Number of synthetic instances per scenario.",
)
@click.option(
    "--results-dir",
    "-r",
    type=click.Path(path_type=Path),
    default=_DEFAULT_RESULTS_DIR,
    show_default=True,
)
@click.option(
    "--seed",
    type=int,
    default=42,
    show_default=True,
    help="Random seed for reproducible mock data.",
)
@click.option(
    "--no-report",
    is_flag=True,
    default=False,
    help="Skip generating the markdown report.",
)
def mock(
    scenarios: tuple[str, ...],
    instances: int,
    results_dir: Path,
    seed: int,
    no_report: bool,
) -> None:
    """Generate simulated evaluation results without running real agents.

    Produces realistic per-instance JSONL files and a summary report that
    demonstrates the scaffolding thesis narrative.  Use this to preview the
    report format or for CI smoke-tests before a full Docker-based run.
    """
    selected = [SCENARIO_BY_NAME[n] for n in scenarios] if scenarios else ALL_SCENARIOS

    cfg = HarnessConfig(results_dir=results_dir)
    harness = Harness(cfg)

    click.echo(
        f"\nGenerating mock results for {len(selected)} scenario(s), {instances} instances each  (seed={seed})\n"
    )

    mock_summaries: dict[str, ScenarioSummary] = {}
    for scenario in selected:
        summary = harness.mock_scenario(scenario, n_instances=instances, seed=seed)
        mock_summaries[scenario.name] = summary
        click.echo(
            f"  {scenario.name:<22}  "
            f"{summary.resolve_rate * 100:.1f}%  "
            f"${summary.mean_cost_per_instance_usd:.4f}/issue  "
            f"${summary.total_cost_usd:.2f} total"
        )

    click.echo(f"\n{'═' * 60}")
    click.echo("MOCK SUMMARY")
    click.echo(f"{'═' * 60}")
    _print_summary_table(list(mock_summaries.values()))

    if not no_report:
        report_path = generate_from_results_dir(results_dir)
        click.echo(f"\nReport written to: {report_path}")


@cli.command()
@click.option(
    "--results-dir",
    "-r",
    type=click.Path(path_type=Path),
    default=_DEFAULT_RESULTS_DIR,
    show_default=True,
)
def status(results_dir: Path) -> None:
    """Show current evaluation progress."""
    if not results_dir.exists():
        click.echo("No results directory found. Run 'eval' to start.")
        return

    store = ResultStore(results_dir)
    all_results = store.load_all()

    if not all_results:
        click.echo("No results found yet.")
        return

    click.echo("\nEvaluation progress:\n")
    for scenario_name, results in all_results.items():
        resolved = sum(1 for r in results if r.resolved)
        errors = sum(1 for r in results if r.status == "error")
        total_cost = sum(r.total_cost_usd for r in results)
        click.echo(
            f"  {scenario_name}: {len(results)} instances evaluated, "
            f"{resolved} resolved ({resolved / len(results) * 100:.1f}%), "
            f"{errors} errors, ${total_cost:.2f} spent"
        )


@cli.command()
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Output path for the report (default: benchmarks/swe_bench/results/head_to_head.md).",
)
def compare(output: Path | None) -> None:
    """Generate the head-to-head comparison report: Bernstein vs. CrewAI vs. LangGraph.

    Uses static competitor estimates from community benchmarks since neither CrewAI
    nor LangGraph publish official SWE-Bench numbers.  Bernstein figures come from
    benchmarks/swe_bench/results/ (simulated by default; replace with real runs).

    To replace simulated Bernstein figures with real results, run:
        benchmarks/swe_bench/run.py eval
    then re-run this command — the canonical comparison will pick up real data
    from benchmarks/swe_bench/results/*_summary.json automatically.
    """
    if output is None:
        output = _DEFAULT_RESULTS_DIR / "head_to_head.md"

    report_md = generate_full_report(CANONICAL_COMPARISON)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report_md, encoding="utf-8")
    click.echo(f"Head-to-head comparison written to: {output}")
    click.echo("")
    click.echo("NOTE: Bernstein figures are simulated. Competitor figures are estimated")
    click.echo("      from community benchmarks. See the Data Sources section for details.")


def _print_summary_table(summaries: list[ScenarioSummary]) -> None:
    col_w = [20, 14, 12, 18, 12]
    headers = ["Scenario", "Resolve rate", "Mean time", "Cost/issue", "Total cost"]
    header_line = "  ".join(h.ljust(w) for h, w in zip(headers, col_w, strict=True))
    click.echo(header_line)
    click.echo("─" * len(header_line))

    for s in summaries:
        cols = [
            s.scenario_name,
            f"{s.resolve_rate * 100:.1f}% ({s.resolved}/{s.total_instances - s.skipped})",
            f"{s.mean_wall_time_s:.0f}s",
            f"${s.mean_cost_per_instance_usd:.4f}",
            f"${s.total_cost_usd:.2f}",
        ]
        click.echo("  ".join(c.ljust(w) for c, w in zip(cols, col_w, strict=True)))


if __name__ == "__main__":
    cli()
