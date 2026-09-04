"""Evolution commands: evolve run/review/approve/status/export."""

from __future__ import annotations

import re as _re
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click

from bernstein.cli.helpers import console

if TYPE_CHECKING:
    from bernstein.core.persistence.runs_report import FailurePatternDraft

_SDD_NOT_FOUND_MSG = (
    "[red].sdd directory not found.[/red] Run [bold]bernstein[/bold] first to initialise the workspace."
)

# ---------------------------------------------------------------------------
# Duration parser
# ---------------------------------------------------------------------------


def _parse_duration(s: str) -> int:
    """Parse a duration string like '2h', '30m', '1h30m' into seconds."""
    total = 0
    for match in _re.finditer(r"(\d+)\s*([hms])", s.lower()):
        value = int(match.group(1))
        unit = match.group(2)
        if unit == "h":
            total += value * 3600
        elif unit == "m":
            total += value * 60
        elif unit == "s":
            total += value

    if total == 0:
        try:
            total = int(s)
        except ValueError:
            return 0
    return total


# ---------------------------------------------------------------------------
# evolve group
# ---------------------------------------------------------------------------


@click.group("evolve")
def evolve() -> None:
    """Manage self-evolution proposals.

    \b
      bernstein evolve review           # list proposals pending human review
      bernstein evolve approve <id>     # approve a specific proposal
      bernstein evolve run              # run the autoresearch evolution loop
      bernstein evolve status           # show evolution history table
      bernstein evolve export [path]    # export HTML/Markdown report
    """


def _load_evolve_config_from_seed(
    root: Path,
    github_sync: bool,
    github_repo: str | None,
) -> tuple[bool, str | None]:
    """Read evolve config from bernstein.yaml if CLI flags were not set."""
    for seed_name in ("bernstein.yaml", "bernstein.yml"):
        seed_path = root / seed_name
        if not seed_path.exists():
            continue
        with suppress(Exception):
            import yaml as _yaml

            raw = _yaml.safe_load(seed_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                break
            evolve_cfg = cast("dict[str, Any]", raw).get("evolve", {})
            if not isinstance(evolve_cfg, dict):
                break
            evolve_dict = cast("dict[str, Any]", evolve_cfg)
            if not github_sync and evolve_dict.get("github_sync"):
                github_sync = True
            if github_repo is None and evolve_dict.get("github_repo"):
                github_repo = str(evolve_dict["github_repo"])
        break
    return github_sync, github_repo


def _generate_failure_drafts(state_dir: Path) -> list[FailurePatternDraft]:
    """Build failure-pattern drafts from the run ledgers under ``state_dir``.

    The signal source is the same classified run history ``bernstein runs
    report`` shows: every finished run under ``.sdd/runtime/ledger`` is
    classified, the failure outcomes are grouped by their failure signature,
    and each group becomes one fingerprinted draft.  The fingerprint is stable
    across scans, so a second pass over unchanged ledgers yields no new drafts.

    Args:
        state_dir: The install's ``.sdd`` directory.

    Returns:
        Failure-pattern drafts, most frequent first.
    """
    from bernstein.core.persistence.runs_report import detect_failure_patterns, list_finished_runs

    return detect_failure_patterns(list_finished_runs(state_dir))


def _show_failure_drafts(
    root: Path,
    state_dir: Path,
    github_sync: bool,
    github_repo: str | None,
) -> None:
    """Show failure-pattern drafts and optionally sync them to GitHub.

    In dry-run mode, prints the drafts without executing the evolution loop.
    When --github is also set, syncs each draft: creates an issue for a
    fingerprint nothing tracks yet, or comments on the issue that already
    tracks it.
    """
    del root  # the ledger root is resolved from state_dir
    drafts = _generate_failure_drafts(state_dir)

    if not drafts:
        console.print("[dim]No failure-pattern drafts found.[/dim]")
        return

    console.print(f"[bold]Failure-pattern drafts ({len(drafts)}):[/bold]\n")

    from rich.table import Table

    draft_table = Table(
        title="Failure-Pattern Drafts",
        show_lines=True,
        header_style="bold cyan",
    )
    draft_table.add_column("Fingerprint", min_width=10)
    draft_table.add_column("Pattern", min_width=30)
    draft_table.add_column("Runs", justify="right", min_width=6)
    draft_table.add_column("Most recent run", min_width=16)

    for draft in drafts:
        draft_table.add_row(
            draft.fingerprint[:8],
            draft.title,
            str(draft.occurrence_count),
            draft.most_recent_run_id,
        )
    console.print(draft_table)

    # Sync to GitHub if requested.
    if github_sync:
        _sync_failure_drafts_to_github(drafts, github_repo)


def _failure_draft_comment(draft: FailurePatternDraft) -> str:
    """Render the comment posted on an issue that already tracks a pattern."""
    contributing = ", ".join(draft.contributing_run_ids[:10])
    return (
        f"**This failure pattern recurred.**\n\n"
        f"- Occurrences: {draft.occurrence_count}\n"
        f"- Most recent run: `{draft.most_recent_run_id}`\n"
        f"- Contributing runs: {contributing}\n"
        f"- Evidence: {draft.sample_evidence}\n"
    )


def _sync_failure_drafts_to_github(
    drafts: list[FailurePatternDraft],
    github_repo: str | None,
) -> None:
    """Create or update one GitHub issue per failure-pattern fingerprint.

    A fingerprint no open issue carries yet gets a new issue labelled with it.
    A fingerprint that is already tracked gets a comment on that issue, so a
    recurring failure updates one item instead of filing a fresh one per cycle.
    """
    from bernstein.core.github import GitHubClient

    gh = GitHubClient(repo=github_repo)
    if not gh.available:
        console.print(
            "[yellow]Warning:[/yellow] [bold]gh[/bold] CLI not available - skipping GitHub sync for failure drafts."
        )
        return

    console.print("\n[bold]Syncing to GitHub...[/bold]")
    created = 0
    commented = 0

    for draft in drafts:
        existing = gh.find_by_fingerprint(draft.fingerprint)
        if existing is not None:
            if gh.comment_on_issue(existing.number, _failure_draft_comment(draft)):
                console.print(f"  [dim]Commented on issue #{existing.number}:[/dim] {draft.title}")
                commented += 1
        else:
            issue = gh.create_issue(
                title=draft.title,
                body=draft.body,
                fingerprint=draft.fingerprint,
            )
            if issue is not None:
                console.print(f"  [green]Created issue #{issue.number}:[/green] {draft.title}")
                created += 1

    console.print(f"\n[dim]GitHub sync complete: {created} created, {commented} commented[/dim]")


@evolve.command("run")
@click.option(
    "--window",
    default="2h",
    show_default=True,
    help="Evolution window duration (e.g. 2h, 30m, 1h30m).",
)
@click.option(
    "--max-proposals",
    default=24,
    show_default=True,
    help="Maximum proposals to evaluate per session.",
)
@click.option(
    "--cycle",
    default=300,
    show_default=True,
    help="Seconds per experiment cycle (default 300 = 5 min).",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
@click.option(
    "--github",
    "github_sync",
    is_flag=True,
    default=False,
    help="Sync proposals as GitHub Issues for distributed coordination.",
)
@click.option(
    "--github-repo",
    default=None,
    help="GitHub repo slug (owner/repo). Inferred from git remote if omitted.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show failure-pattern drafts without running the evolution loop or syncing to GitHub.",
)
def evolve_run(
    window: str,
    max_proposals: int,
    cycle: int,
    workdir: str,
    github_sync: bool,
    github_repo: str | None,
    dry_run: bool,
) -> None:
    """Run the autoresearch evolution loop.

    \b
    Runs time-boxed experiment cycles that:
    1. Analyze metrics and detect improvement opportunities
    2. Generate low-risk proposals (L0/L1 only)
    3. Sandbox validate each proposal
    4. Auto-apply improvements that pass validation
    5. Log all results to .sdd/evolution/experiments.jsonl

    L2+ proposals are saved to .sdd/evolution/deferred.jsonl for human review.

    When --github is set, each proposal is published as a GitHub Issue with
    label ``bernstein-evolve``.  Multiple instances running concurrently will
    claim different issues, preventing duplicate work.

    When --dry-run is set, only failure-pattern drafts are printed and
    the evolution loop does not execute.  Use --github with --dry-run to
    preview what would be synced to GitHub.

    \b
      bernstein evolve run                         # default: 2h window, 24 proposals
      bernstein evolve run --window 30m            # short session
      bernstein evolve run --max-proposals 48      # more experiments
      bernstein evolve run --github                # sync proposals to GitHub Issues
      bernstein evolve run --github --github-repo owner/myrepo
      bernstein evolve run --dry-run               # preview failure-pattern drafts only
    """
    from bernstein.evolution.loop import EvolutionLoop

    root = Path(workdir).resolve()
    state_dir = root / ".sdd"

    if not state_dir.is_dir():
        console.print(_SDD_NOT_FOUND_MSG)
        raise SystemExit(1)

    github_sync, github_repo = _load_evolve_config_from_seed(root, github_sync, github_repo)

    # Parse window duration string (e.g. "2h", "30m", "1h30m").
    window_seconds = _parse_duration(window)
    if window_seconds <= 0:
        console.print(f"[red]Invalid window duration:[/red] {window}")
        raise SystemExit(1)

    # Dry-run mode: show failure-pattern drafts without running the loop.
    if dry_run:
        _show_failure_drafts(root, state_dir, github_sync, github_repo)
        return

    # Check GitHub availability early so we can warn before the loop starts.
    if github_sync:
        from bernstein.core.github import GitHubClient

        _gh_check = GitHubClient(repo=github_repo)
        if not _gh_check.available:
            console.print(
                "[yellow]Warning:[/yellow] --github requested but [bold]gh[/bold] CLI "
                "is not available or not authenticated.\n"
                "GitHub sync will be skipped. Run [bold]gh auth login[/bold] to enable it."
            )
            github_sync = False

    github_line = "  GitHub:     enabled\n" if github_sync else ""
    console.print(
        f"[bold]Evolution loop starting[/bold]\n"
        f"  Window:     {window} ({window_seconds}s)\n"
        f"  Max props:  {max_proposals}\n"
        f"  Cycle:      {cycle}s\n"
        f"  State dir:  {state_dir}\n" + github_line
    )

    loop = EvolutionLoop(
        state_dir=state_dir,
        repo_root=root,
        cycle_seconds=cycle,
        max_proposals=max_proposals,
        window_seconds=window_seconds,
        github_sync=github_sync,
    )
    if github_sync and github_repo:
        # Pass the explicit repo slug to the lazily-created GitHubClient.
        from bernstein.core.github import GitHubClient

        loop._github = GitHubClient(repo=github_repo)  # type: ignore[reportPrivateUsage]

    try:
        results = loop.run(
            window_seconds=window_seconds,
            max_proposals=max_proposals,
        )
    except KeyboardInterrupt:
        loop.stop()
        results = loop._experiments  # type: ignore[reportPrivateUsage]
        console.print("\n[dim]Evolution loop interrupted.[/dim]")

    # Print summary.
    summary = loop.get_summary()
    console.print(
        f"\n[bold]Evolution complete[/bold]\n"
        f"  Experiments:  {summary['experiments_run']}\n"
        f"  Accepted:     {summary['proposals_accepted']}\n"
        f"  Rate:         {summary['acceptance_rate']:.0%}\n"
        f"  Cost:         ${summary['total_cost_usd']:.4f}\n"
        f"  Elapsed:      {summary['elapsed_seconds']:.0f}s\n"
    )

    if results:
        from rich.table import Table

        result_table = Table(
            title="Experiment Results",
            show_lines=False,
            header_style="bold cyan",
        )
        result_table.add_column("Proposal", min_width=12)
        result_table.add_column("Title", min_width=30)
        result_table.add_column("Risk", min_width=8)
        result_table.add_column("Delta", justify="right", min_width=8)
        result_table.add_column("Result", min_width=10)

        for r in results:
            color = "green" if r.accepted else "red"
            delta_str = f"{r.delta:+.3f}" if r.delta != 0 else "-"
            result_table.add_row(
                r.proposal_id,
                r.title,
                r.risk_level,
                delta_str,
                f"[{color}]{'accepted' if r.accepted else 'rejected'}[/{color}]",
            )
        console.print(result_table)


@evolve.command("review")
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def evolve_review(workdir: str) -> None:
    """Show upgrade proposals pending human review."""
    from bernstein.evolution.gate import ApprovalGate

    root = Path(workdir).resolve()
    decisions_dir = root / ".sdd" / "evolution"
    pending = ApprovalGate(decisions_dir=decisions_dir).get_pending_decisions()

    if not pending:
        console.print("[dim]No proposals pending review.[/dim]")
        return

    from rich.table import Table

    review_table = Table(title="Proposals Pending Review", show_lines=True, header_style="bold cyan")
    review_table.add_column("ID", style="dim", min_width=12)
    review_table.add_column("Risk", min_width=12)
    review_table.add_column("Confidence", justify="right", min_width=10)
    review_table.add_column("Outcome", min_width=22)
    review_table.add_column("Reason")

    for d in sorted(pending, key=lambda x: x.decided_at):
        outcome_color = "red" if "immediate" in d.outcome.value else "yellow"
        review_table.add_row(
            d.proposal_id,
            d.risk_level.value,
            f"{d.confidence:.0%}",
            f"[{outcome_color}]{d.outcome.value}[/{outcome_color}]",
            d.reason,
        )

    console.print(review_table)
    console.print("\n[dim]Approve with:[/dim] [bold]bernstein evolve approve <id>[/bold]")


@evolve.command("approve")
@click.argument("proposal_id")
@click.option(
    "--reviewer",
    default="human",
    show_default=True,
    help="Name of the approver.",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def evolve_approve(proposal_id: str, reviewer: str, workdir: str) -> None:
    """Approve an upgrade proposal by ID."""
    from bernstein.evolution.gate import ApprovalGate

    root = Path(workdir).resolve()
    decisions_dir = root / ".sdd" / "evolution"
    decision = ApprovalGate(decisions_dir=decisions_dir).approve(proposal_id, reviewer=reviewer)

    if decision is None:
        console.print(
            f"[red]No pending proposal found:[/red] {proposal_id}\n"
            "Run [bold]bernstein evolve review[/bold] to list pending proposals."
        )
        raise SystemExit(1)

    console.print(f"[green]Approved:[/green] [bold]{proposal_id}[/bold] (reviewer={reviewer})")


@evolve.command("status")
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def evolve_status(workdir: str) -> None:
    """Show evolution history as a rich table.

    Reads .sdd/metrics/evolve_cycles.jsonl and .sdd/evolution/experiments.jsonl
    and displays a per-cycle breakdown with cumulative improvement metrics.

    \b
      bernstein evolve status           # history from current directory
      bernstein evolve status --dir /path/to/project
    """
    from bernstein.evolution.report import EvolutionReport

    root = Path(workdir).resolve()
    state_dir = root / ".sdd"

    if not state_dir.is_dir():
        console.print(_SDD_NOT_FOUND_MSG)
        raise SystemExit(1)

    report = EvolutionReport(state_dir=state_dir)
    report.load()
    report.print_status()


@evolve.command("export")
@click.argument("output", default="evolution_report", required=False)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["html", "md", "markdown"], case_sensitive=False),
    default="html",
    show_default=True,
    help="Output format: html or md/markdown.",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def evolve_export(output: str, fmt: str, workdir: str) -> None:
    """Export a static evolution report (HTML or Markdown).

    OUTPUT is the output file path (without extension). Defaults to
    'evolution_report' in the current directory.

    \b
      bernstein evolve export                        # evolution_report.html
      bernstein evolve export --format md            # evolution_report.md
      bernstein evolve export docs/evolution         # docs/evolution.html
    """
    from bernstein.evolution.report import EvolutionReport

    root = Path(workdir).resolve()
    state_dir = root / ".sdd"

    if not state_dir.is_dir():
        console.print(_SDD_NOT_FOUND_MSG)
        raise SystemExit(1)

    report = EvolutionReport(state_dir=state_dir)
    report.load()

    if not report.cycles:
        console.print("[dim]No evolution data found to export.[/dim]")
        raise SystemExit(1)

    is_markdown = fmt.lower() in ("md", "markdown")
    ext = ".md" if is_markdown else ".html"
    out_path = Path(output)
    if out_path.suffix.lower() not in (".html", ".md"):
        out_path = out_path.with_suffix(ext)

    if is_markdown:
        report.export_markdown(out_path)
    else:
        report.export_html(out_path)

    console.print(
        f"[green]Report written:[/green] {out_path} "
        f"({report.total_cycles} cycles, {report.total_tasks_completed} tasks completed)"
    )
