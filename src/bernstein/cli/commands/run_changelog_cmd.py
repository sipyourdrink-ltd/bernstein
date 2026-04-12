"""run-changelog command — generate a changelog from agent-produced diffs.

Distinct from ``bernstein changelog`` (which generates changelogs from
conventional commits).  This command analyses what Bernstein agents actually
changed during a run: groups changes by component, summarises each in plain
English, flags breaking changes, and links back to the originating task.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.panel import Panel

from bernstein.cli.helpers import SERVER_URL, console
from bernstein.core.run_changelog import (
    RunChangelog,
    format_console,
    format_markdown,
    generate_run_changelog,
)


@click.command("run-changelog")
@click.option(
    "--since",
    "since_ref",
    default=None,
    metavar="REF",
    help="Git ref (tag or SHA) marking the start of the run window. Only commits after this ref are included.",
)
@click.option(
    "--hours",
    "since_hours",
    default=None,
    type=float,
    metavar="N",
    help="Limit to tasks completed in the last N hours (default: 24). Ignored when --since is provided.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["console", "markdown"]),
    default="console",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Write Markdown output to this file (implies --format markdown).",
)
@click.option(
    "--repo-url",
    default=None,
    metavar="URL",
    help="Repository URL used to generate task links in Markdown output.",
)
@click.option(
    "--include-no-commits",
    is_flag=True,
    default=False,
    help="Include tasks that have no matching git commits (useful to surface tasks that completed without committing).",
)
@click.option(
    "--server-url",
    "server_url",
    default=None,
    metavar="URL",
    help="Bernstein task server URL (default: $BERNSTEIN_SERVER_URL or http://localhost:8052).",
)
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    type=click.Path(exists=True, file_okay=False),
    help="Project root (git repository).",
)
def run_changelog_cmd(
    since_ref: str | None,
    since_hours: float | None,
    fmt: str,
    output_path: str | None,
    repo_url: str | None,
    include_no_commits: bool,
    server_url: str | None,
    workdir: str,
) -> None:
    """Generate a changelog from what Bernstein agents actually changed.

    Queries the task server for completed tasks, maps each task to its git
    commits (via the ``Refs: #<task_id>`` footer added by ``bernstein``), and
    produces a human-readable changelog grouped by component.

    \b
    Examples:
      bernstein run-changelog                          # last 24 h, console
      bernstein run-changelog --hours 48               # last 48 h
      bernstein run-changelog --since v1.2.0           # since a tag
      bernstein run-changelog -o CHANGELOG-run.md      # write to file
      bernstein run-changelog --format markdown        # markdown to stdout
    """
    effective_server_url = server_url or SERVER_URL
    cwd = Path(workdir).resolve()

    console.print()
    console.print(
        Panel(
            "[bold]Run Changelog[/bold]  [dim]agent-produced diffs[/dim]",
            border_style="blue",
            expand=False,
        )
    )

    # Resolve hours: default 24 when neither --since nor --hours given
    effective_hours = since_hours if since_ref is None else None

    cl: RunChangelog = generate_run_changelog(
        cwd,
        server_url=effective_server_url,
        since_ref=since_ref,
        since_hours=effective_hours,
        include_no_commits=include_no_commits,
    )

    if not cl.changes and not include_no_commits:
        console.print(
            "[yellow]No agent-produced changes found. Try --include-no-commits or a wider --hours window.[/yellow]"
        )
        return

    # Determine effective format
    effective_fmt = fmt
    if output_path and fmt == "console":
        effective_fmt = "markdown"

    if effective_fmt == "console":
        console.print(format_console(cl, repo_url=repo_url))
    else:
        md = format_markdown(cl, repo_url=repo_url)
        if output_path:
            out = Path(output_path)
            out.write_text(md, encoding="utf-8")
            total = sum(len(v) for v in cl.changes.values())
            console.print(
                f"[green]Changelog written to[/green] [bold]{output_path}[/bold] "
                f"[dim]({total} changes, {len(cl.breaking_changes)} breaking)[/dim]"
            )
        else:
            click.echo(md)
