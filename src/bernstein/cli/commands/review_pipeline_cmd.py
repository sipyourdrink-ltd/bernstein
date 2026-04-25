"""CLI handler for ``bernstein review --pipeline ...``.

Glue between the Click frontend in :mod:`bernstein.cli.commands.task_cmd`
and the review pipeline runner.  Handles three modes:

* ``--validate-only``: parse the YAML, exit 0/1 with a friendly message.
* ``--dry-run``: print the resolved pipeline as a verdict table; no LLM.
* default: fetch the PR's diff, run the pipeline, print the verdict table.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.quality.review_pipeline import (
    DiffSource,
    PipelineVerdict,
    ReviewPipeline,
    ReviewPipelineError,
    diff_from_pr,
    load_pipeline,
    run_pipeline_sync,
)

if TYPE_CHECKING:
    from bernstein.core.quality.review_pipeline.verdict import StageVerdict

logger = logging.getLogger(__name__)


def run_review_pipeline_cli(
    *,
    pipeline_path: str | None,
    pr_number: int | None,
    validate_only: bool,
    dry_run: bool,
    workdir: str = ".",
) -> int:
    """Drive the review pipeline CLI flow.

    Returns the process exit code (0 on approve, 1 on request_changes or error).
    """
    if pipeline_path is None:
        console.print("[red]--pipeline is required when using --validate-only / --dry-run / --pr.[/red]")
        return 2

    try:
        pipeline = load_pipeline(Path(pipeline_path))
    except ReviewPipelineError as exc:
        console.print(
            Panel(
                f"[bold red]Pipeline validation failed[/bold red]\n{exc}",
                border_style="red",
                expand=False,
            )
        )
        return 1

    if validate_only:
        _print_pipeline_summary(pipeline)
        console.print(Panel("[bold green]Pipeline OK[/bold green]", border_style="green", expand=False))
        return 0

    if dry_run:
        _print_pipeline_summary(pipeline)
        console.print(
            Panel(
                "[bold]Dry run — no agents spawned, no LLM calls.[/bold]",
                border_style="blue",
                expand=False,
            )
        )
        return 0

    if pr_number is None:
        console.print("[red]--pr <N> is required to run the pipeline.[/red]")
        return 2

    try:
        diff_src = diff_from_pr(pr_number, repo_root=Path(workdir))
    except RuntimeError as exc:
        console.print(
            Panel(
                f"[bold red]Could not fetch PR #{pr_number}[/bold red]\n{exc}",
                border_style="red",
                expand=False,
            )
        )
        return 1

    verdict = run_pipeline_sync(pipeline, diff_src)
    _print_verdict_table(pipeline, verdict, diff_src)
    return 0 if verdict.verdict == "approve" else 1


def _print_pipeline_summary(pipeline: ReviewPipeline) -> None:
    """Pretty-print a pipeline overview before validation / dry-run."""
    name = pipeline.name or "<unnamed>"
    title = f"[bold]Review pipeline:[/bold] {name}"
    console.print(Panel(title, border_style="blue", expand=False))

    table = Table(title="Stages", show_lines=False)
    table.add_column("#", style="dim", width=3)
    table.add_column("Stage")
    table.add_column("Parallelism", justify="right")
    table.add_column("Aggregator")
    table.add_column("Agents")
    for idx, stage in enumerate(pipeline.stages, start=1):
        agents_repr = ", ".join(f"{a.role}({a.model or 'cascade'})" for a in stage.agents)
        agg_repr = stage.aggregator.strategy
        if stage.aggregator.pass_threshold is not None:
            agg_repr += f"@{stage.aggregator.pass_threshold:.2f}"
        table.add_row(str(idx), stage.name, str(stage.parallelism), agg_repr, agents_repr)
    console.print(table)
    console.print(f"[dim]pass_threshold={pipeline.pass_threshold:.2f}  block_on_fail={pipeline.block_on_fail}[/dim]")


def _print_verdict_table(
    pipeline: ReviewPipeline,
    verdict: PipelineVerdict,
    diff_src: DiffSource,
) -> None:
    """Render a Rich table summarising every stage's verdict."""
    pr_label = f"PR #{diff_src.pr_number}" if diff_src.pr_number else diff_src.title
    pipeline_name = pipeline.name or "<unnamed>"
    overall = (
        "[bold green]APPROVE[/bold green]" if verdict.verdict == "approve" else "[bold red]REQUEST CHANGES[/bold red]"
    )
    console.print(
        Panel(
            f"{overall}  pipeline=[bold]{pipeline_name}[/bold]  target=[bold]{pr_label}[/bold]",
            border_style="green" if verdict.verdict == "approve" else "red",
            expand=False,
        )
    )

    table = Table(title="Stage verdicts")
    table.add_column("#", style="dim", width=3)
    table.add_column("Stage")
    table.add_column("Verdict")
    table.add_column("Score", justify="right")
    table.add_column("Approve/Total", justify="right")
    table.add_column("Feedback")
    for idx, sv in enumerate(verdict.stages, start=1):
        sv_v: StageVerdict = sv
        marker = "[green]approve[/green]" if sv_v.verdict == "approve" else "[red]request_changes[/red]"
        table.add_row(
            str(idx),
            sv_v.stage,
            marker,
            f"{sv_v.pass_score:.2f}",
            f"{sv_v.approve_count}/{sv_v.total_count}",
            sv_v.feedback,
        )
    console.print(table)

    if verdict.issues:
        console.print()
        console.print("[bold]Issues:[/bold]")
        for issue in verdict.issues:
            console.print(f"  [red]-[/red] {issue}")

    console.print()
    console.print(f"[dim]{verdict.feedback}[/dim]")
