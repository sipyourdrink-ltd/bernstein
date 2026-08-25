"""CLI handler for ``bernstein review --pipeline ...``.

Glue between the Click frontend in :mod:`bernstein.cli.commands.task_cmd`
and the review pipeline runner.  Handles four modes:

* ``--validate-only``: parse the YAML, exit 0/1 with a friendly message.
* ``--dry-run``: print the resolved pipeline as a verdict table; no LLM.
* ``--fix`` / ``--until-checks-green``: run the fix-until-green contour and
  print one row per pass.
* default: fetch the PR's diff, run the pipeline, print the verdict table.

The contour itself lives in
:mod:`bernstein.core.quality.review_pipeline.contour`; this module only
assembles its inputs and forwards its exit code.
"""

from __future__ import annotations

import logging
import os
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
    ReviewRulesetError,
    check_log_fetcher,
    check_rollup_from_pr,
    command_fix_runner,
    diff_from_pr,
    gh_pr_view_json,
    load_pipeline,
    load_ruleset,
    receipt_emitter,
    run_pipeline_sync,
    run_review_contour,
)

if TYPE_CHECKING:
    from bernstein.core.quality.review_pipeline.contour import ContourResult
    from bernstein.core.quality.review_pipeline.ruleset import ReviewRuleset
    from bernstein.core.quality.review_pipeline.verdict import StageVerdict

logger = logging.getLogger(__name__)

#: Environment fallback for ``--fix-command``.
FIX_COMMAND_ENV = "BERNSTEIN_REVIEW_FIX_COMMAND"


def run_review_pipeline_cli(
    *,
    pipeline_path: str | None,
    pr_number: int | None,
    validate_only: bool,
    dry_run: bool,
    workdir: str = ".",
    fix: bool = False,
    until_checks_green: bool = False,
    max_passes: int = 3,
    fix_command: str | None = None,
) -> int:
    """Drive the review pipeline CLI flow.

    Args:
        pipeline_path: Path to the pipeline YAML.
        pr_number: PR under review.
        validate_only: Validate the YAML and stop.
        dry_run: Print the resolved pipeline and stop.
        workdir: Repository root ``gh`` runs in.
        fix: Run a fix pass between review passes.
        until_checks_green: Withhold approval while the checks are not green.
        max_passes: Review budget for the contour.
        fix_command: Command the fix pass runs; falls back to
            ``$BERNSTEIN_REVIEW_FIX_COMMAND``.

    Returns:
        The process exit code (0 on approve, 1 on request_changes,
        ``needs-operator``, or error; 2 on a usage mistake).
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
                "[bold]Dry run: no agents spawned, no LLM calls.[/bold]",
                border_style="blue",
                expand=False,
            )
        )
        return 0

    if pr_number is None:
        console.print("[red]--pr <N> is required to run the pipeline.[/red]")
        return 2

    root = Path(workdir)
    try:
        ruleset = load_ruleset(
            repo_root=root,
            rules=pipeline.rules,
            base_dir=Path(pipeline_path).parent,
        )
    except ReviewRulesetError as exc:
        console.print(Panel(f"[bold red]{exc}[/bold red]", border_style="red", expand=False))
        return 1

    if fix or until_checks_green:
        return _run_contour(
            pipeline,
            ruleset=ruleset,
            pr_number=pr_number,
            root=root,
            fix=fix,
            until_checks_green=until_checks_green,
            max_passes=max_passes,
            fix_command=fix_command,
        )

    try:
        diff_src = diff_from_pr(pr_number, repo_root=root)
    except RuntimeError as exc:
        console.print(
            Panel(
                f"[bold red]Could not fetch PR #{pr_number}[/bold red]\n{exc}",
                border_style="red",
                expand=False,
            )
        )
        return 1

    verdict = run_pipeline_sync(pipeline, diff_src, ruleset=ruleset)
    _print_verdict_table(pipeline, verdict, diff_src)
    return 0 if verdict.verdict == "approve" else 1


def _repo_slug(pr_url: str) -> str:
    """Derive ``owner/repo`` from a pull-request url, offline."""
    parts = [p for p in pr_url.split("/") if p]
    if len(parts) >= 4 and parts[-2] == "pull":
        return f"{parts[-4]}/{parts[-3]}"
    return ""


def _run_contour(
    pipeline: ReviewPipeline,
    *,
    ruleset: ReviewRuleset,
    pr_number: int,
    root: Path,
    fix: bool,
    until_checks_green: bool,
    max_passes: int,
    fix_command: str | None,
) -> int:
    """Assemble the contour's collaborators and forward its exit code."""
    try:
        meta = gh_pr_view_json(pr_number, "url,body", repo_root=root)
    except RuntimeError as exc:
        console.print(
            Panel(
                f"[bold red]Could not read PR #{pr_number}[/bold red]\n{exc}",
                border_style="red",
                expand=False,
            )
        )
        return 1

    pr_url = str(meta.get("url", ""))
    emit = None
    if pr_url:
        emit = receipt_emitter(
            workdir=root,
            pr_url=pr_url,
            repo=_repo_slug(pr_url),
            issue_body=str(meta.get("body", "")),
        )
    else:
        console.print("[yellow]No PR url available - passes will run without receipts.[/yellow]")

    command = fix_command or os.environ.get(FIX_COMMAND_ENV, "")
    fix_runner = command_fix_runner(command, repo_root=root) if (fix and command) else None
    if fix and fix_runner is None:
        console.print(
            f"[yellow]--fix needs a fix command (--fix-command or ${FIX_COMMAND_ENV}); "
            "the contour will review once and hand back.[/yellow]"
        )

    result = run_review_contour(
        pipeline,
        fetch_diff=lambda: diff_from_pr(pr_number, repo_root=root),
        read_rollup=lambda: check_rollup_from_pr(pr_number, repo_root=root),
        review=lambda diff_src: run_pipeline_sync(pipeline, diff_src, ruleset=ruleset),
        ruleset=ruleset,
        fetch_logs=check_log_fetcher(),
        fix_runner=fix_runner,
        emit_receipt=emit,
        max_passes=max_passes,
        until_checks_green=until_checks_green,
        pr_number=pr_number,
    )
    _print_contour_table(result, pr_number=pr_number, ruleset=ruleset)
    return result.exit_code


def _print_contour_table(result: ContourResult, *, pr_number: int, ruleset: ReviewRuleset) -> None:
    """Render one row per pass plus the contour's single outcome."""
    approved = result.outcome == "approved"
    headline = "[bold green]APPROVED[/bold green]" if approved else "[bold red]NEEDS OPERATOR[/bold red]"
    console.print(
        Panel(
            f"{headline}  target=[bold]PR #{pr_number}[/bold]  ruleset=[bold]{ruleset.digest}[/bold]",
            border_style="green" if approved else "red",
            expand=False,
        )
    )

    table = Table(title="Review passes")
    table.add_column("#", style="dim", width=3)
    table.add_column("Verdict")
    table.add_column("Checks")
    table.add_column("Diff")
    table.add_column("Receipt")
    table.add_column("Fix pushed")
    for record in result.passes:
        marker = "[green]approve[/green]" if record.verdict == "approve" else "[red]request_changes[/red]"
        table.add_row(
            str(record.index),
            marker,
            record.checks_state,
            record.diff_hash[:19],
            record.receipt_entry_hash[:19] or "-",
            "yes" if record.fix_pushed else "no",
        )
    console.print(table)

    if result.reason:
        console.print()
        console.print(f"[yellow]{result.reason}[/yellow]")


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
        agg_repr: str = stage.aggregator.strategy
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
