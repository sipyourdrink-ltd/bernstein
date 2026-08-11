"""``bernstein issue-to-pr`` -- inspect the issue-to-PR pipeline state.

The CLI is intentionally thin: real logic lives in
:mod:`bernstein.core.orchestration.issue_to_pr`. Only the read-only ``trace``
subcommand is exposed; advancing the pipeline remains a Python API concern.
"""

from __future__ import annotations

import click

from bernstein.cli.helpers import console
from bernstein.core.orchestration.issue_to_pr import (
    IssuePRClient,
    IssueToPRConfig,
    IssueToPRPipeline,
)


@click.group("issue-to-pr")
def issue_to_pr_group() -> None:
    """Inspect the issue -> plan-comment -> PR pipeline."""


@issue_to_pr_group.command("trace")
@click.argument("issue_id")
@click.option(
    "--repo",
    required=True,
    help="GitHub owner/repo slug, e.g. acme/web.",
)
def trace_cmd(issue_id: str, repo: str) -> None:
    """Print the pipeline state for ``<owner>/<repo>#<issue_id>``.

    Reads sticky-comment markers and prints one fact per line. This command is
    read-only and exits 0 after rendering a snapshot.
    """
    try:
        issue_number = int(issue_id.lstrip("#"))
    except ValueError as exc:
        raise click.BadParameter(f"issue_id must be numeric, got {issue_id!r}") from exc

    trace = IssueToPRPipeline(
        config=IssueToPRConfig(),
        client=IssuePRClient(),
    ).trace(repo, issue_number)
    console.print(trace.render())
