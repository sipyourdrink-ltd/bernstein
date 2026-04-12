"""Evolution commands: evolve run/review/approve/status/export."""

from __future__ import annotations

import re as _re
from pathlib import Path
from typing import Any, cast

import click

from bernstein.cli.helpers import console

# ---------------------------------------------------------------------------
# Duration parser
# ---------------------------------------------------------------------------


def _parse_duration(s: str) -> int:
    """Parse a duration string like '2h', '30m', '1h30m' into seconds."""
    total = 0
    for match in _re.finditer(r"(\d+)\s*(h|m|s)", s.lower()):
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
def evolve_run(
    window: str,
    max_proposals: int,
    cycle: int,
    workdir: str,
    github_sync: bool,
    github_repo: str | None,
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

    \b
      bernstein evolve run                         # default: 2h window, 24 proposals
      bernstein evolve run --window 30m            # short session
      bernstein evolve run --max-proposals 48      # more experiments
      bernstein evolve run --github                # sync proposals to GitHub Issues
      bernstein evolve run --github --github-repo owner/myrepo
    """
    from bernstein.evolution.loop import EvolutionLoop

    root = Path(workdir).resolve()
    state_dir = root / ".sdd"

    if not state_dir.is_dir():
        console.print(
            "[red].sdd directory not found.[/red] Run [bold]bernstein[/bold] first to initialise the workspace."
        )
        raise SystemExit(1)

    # Read evolve.github_sync / evolve.github_repo from bernstein.yaml if present
    # and the flags were not set on the CLI.
    for _seed_name in ("bernstein.yaml", "bernstein.yml"):
        _seed_path = root / _seed_name
        if _seed_path.exists():
            try:
                import yaml as _yaml

                _seed_raw = _yaml.safe_load(_seed_path.read_text(encoding="utf-8"))
                if isinstance(_seed_raw, dict):
                    _seed_dict = cast("dict[str, Any]", _seed_raw)
                    _evolve_cfg = _seed_dict.get("evolve", {})
                    if isinstance(_evolve_cfg, dict):
                        _evolve_dict = cast("dict[str, Any]", _evolve_cfg)
                        if not github_sync and _evolve_dict.get("github_sync"):
                            github_sync = True
                        if github_repo is None and _evolve_dict.get("github_repo"):
                            github_repo = str(_evolve_dict["github_repo"])
            except Exception:
                pass  # YAML parse errors are non-fatal here
            break

    # Parse window duration string (e.g. "2h", "30m", "1h30m").
    window_seconds = _parse_duration(window)
    if window_seconds <= 0:
        console.print(f"[red]Invalid window duration:[/red] {window}")
        raise SystemExit(1)

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
            delta_str = f"{r.delta:+.3f}" if r.delta != 0 else "—"
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
    gate = ApprovalGate(decisions_dir=decisions_dir)
    pending = gate.get_pending_decisions()

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
    gate = ApprovalGate(decisions_dir=decisions_dir)
    decision = gate.approve(proposal_id, reviewer=reviewer)

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
        console.print(
            "[red].sdd directory not found.[/red] Run [bold]bernstein[/bold] first to initialise the workspace."
        )
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
        console.print(
            "[red].sdd directory not found.[/red] Run [bold]bernstein[/bold] first to initialise the workspace."
        )
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
