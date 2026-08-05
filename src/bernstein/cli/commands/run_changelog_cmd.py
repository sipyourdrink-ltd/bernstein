"""run-changelog command - generate a changelog from agent-produced diffs.

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

# The original ``run_changelog_cmd`` click command was the top-level
# ``bernstein run-changelog``. In v4.0.0 the bare name ``bernstein changelog``
# took over this behaviour (issue #3142), so the click registration now lives
# in ``bernstein.cli.commands.changelog_cmd`` under the ``changelog`` group.
# The pure-Python implementation stays here as ``run_changelog_default`` so
# the group default callback and the deprecated ``run-changelog`` alias can
# both call it without going through click.


def run_changelog_default(
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
    produces a human-readable changelog grouped by component. Invoked by
    ``bernstein changelog`` (the new bare name, see issue #3142) and by the
    deprecated ``bernstein run-changelog`` alias.

    \b
    Examples:
      bernstein changelog                              # last 24 h, console
      bernstein changelog --hours 48                   # last 48 h
      bernstein changelog --since v1.2.0               # since a tag
      bernstein changelog -o CHANGELOG-run.md          # write to file
      bernstein changelog --format markdown            # markdown to stdout
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
