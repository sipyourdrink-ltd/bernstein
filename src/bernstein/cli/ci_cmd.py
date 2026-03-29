"""CI autofix CLI commands.

Provides the ``bernstein ci`` command group:

  bernstein ci fix <run-url>   One-shot fix of a specific failing run.
  bernstein ci watch <repo>    Continuous poll loop for new CI failures.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

import click

from bernstein.cli.helpers import SERVER_URL, console

if TYPE_CHECKING:
    from bernstein.core.ci_fix import CIAutofixPipeline
    from bernstein.core.ci_monitor import CIMonitor


def _parse_run_url(url: str) -> tuple[str, int]:
    """Extract owner/repo and run_id from a GitHub Actions run URL.

    Args:
        url: Full GitHub Actions URL, e.g.
            ``https://github.com/owner/repo/actions/runs/123456``.

    Returns:
        Tuple of (``"owner/repo"``, ``run_id``).

    Raises:
        click.BadParameter: If the URL does not match the expected pattern.
    """
    m = re.match(
        r"https?://github\.com/([^/]+/[^/]+)/actions/runs/(\d+)",
        url,
    )
    if not m:
        raise click.BadParameter(
            f"Invalid GitHub Actions run URL: {url}\nExpected: https://github.com/OWNER/REPO/actions/runs/RUN_ID",
        )
    return m.group(1), int(m.group(2))


@click.group("ci")
def ci_group() -> None:
    """CI autofix: detect and fix failing GitHub Actions runs."""


@ci_group.command("fix")
@click.argument("run_url")
@click.option("--token", envvar="GITHUB_TOKEN", default=None, help="GitHub token (or set GITHUB_TOKEN).")
@click.option("--server", default=SERVER_URL, help="Bernstein task server URL.")
def ci_fix(run_url: str, token: str | None, server: str) -> None:
    """One-shot fix of a specific failing GitHub Actions run.

    Parses the run URL, downloads logs, extracts failure context,
    and creates a Bernstein fix task.
    """
    if not token:
        console.print("[red]Error:[/red] GitHub token required. Set GITHUB_TOKEN or use --token.")
        raise SystemExit(1)

    repo, run_id = _parse_run_url(run_url)
    console.print(f"[bold]Fetching failure logs for [cyan]{repo}[/cyan] run [cyan]{run_id}[/cyan]...[/bold]")

    from bernstein.core.ci_fix import CIAutofixPipeline
    from bernstein.core.ci_monitor import CIMonitor

    monitor = CIMonitor()

    try:
        ctx = asyncio.run(monitor.parse_failure_logs(repo, run_id, token))
    except Exception as exc:
        console.print(f"[red]Failed to download/parse logs:[/red] {exc}")
        raise SystemExit(1) from exc

    console.print(f"  Test:  [yellow]{ctx.test_name or '(not identified)'}[/yellow]")
    console.print(f"  Error: [red]{ctx.error_message[:120]}[/red]")
    if ctx.file_path:
        console.print(f"  File:  [dim]{ctx.file_path}:{ctx.line_number}[/dim]")

    pipeline = CIAutofixPipeline(server_url=server)
    task_id = pipeline.create_fix_task(ctx, run_url=run_url)

    if task_id:
        console.print(f"\n[green]Fix task created:[/green] [bold]{task_id}[/bold]")
        console.print("[dim]Run 'bernstein run' to start the fix agent.[/dim]")
    else:
        console.print("[red]Failed to create fix task. Is the Bernstein server running?[/red]")
        raise SystemExit(1)


@ci_group.command("watch")
@click.argument("repo")
@click.option("--token", envvar="GITHUB_TOKEN", default=None, help="GitHub token (or set GITHUB_TOKEN).")
@click.option("--server", default=SERVER_URL, help="Bernstein task server URL.")
@click.option("--interval", default=60, help="Poll interval in seconds.", show_default=True)
def ci_watch(repo: str, token: str | None, server: str, interval: int) -> None:
    """Continuously watch a repo for new CI failures and auto-create fix tasks.

    Polls the GitHub Actions API every INTERVAL seconds for new failing
    runs.  When a new failure is detected, downloads the logs, parses
    the failure context, and creates a Bernstein fix task.

    REPO should be in ``owner/repo`` format.
    """
    if not token:
        console.print("[red]Error:[/red] GitHub token required. Set GITHUB_TOKEN or use --token.")
        raise SystemExit(1)

    console.print(f"[bold]Watching [cyan]{repo}[/cyan] for CI failures (every {interval}s)...[/bold]")
    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

    from bernstein.core.ci_fix import CIAutofixPipeline
    from bernstein.core.ci_monitor import CIMonitor

    monitor = CIMonitor()
    pipeline = CIAutofixPipeline(server_url=server)

    try:
        while True:
            _poll_once(monitor, pipeline, repo, token)
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped watching.[/dim]")


def _poll_once(
    monitor: CIMonitor,
    pipeline: CIAutofixPipeline,
    repo: str,
    token: str,
) -> None:
    """Execute one poll cycle: discover failures, create tasks.

    Args:
        monitor: CI monitor instance (tracks seen run IDs).
        pipeline: Autofix pipeline for task creation.
        repo: Repository in ``owner/repo`` format.
        token: GitHub token.
    """
    try:
        failures = asyncio.run(monitor.poll_failures(repo, token))
    except Exception as exc:
        console.print(f"[yellow]Poll error:[/yellow] {exc}")
        return

    if not failures:
        console.print(f"[dim]{time.strftime('%H:%M:%S')} — no new failures[/dim]")
        return

    for failure in failures:
        console.print(
            f"[red]New failure:[/red] {failure.workflow_name} on [cyan]{failure.branch}[/cyan] (run {failure.run_id})"
        )
        try:
            ctx = asyncio.run(monitor.parse_failure_logs(repo, failure.run_id, token))
        except Exception as exc:
            console.print(f"  [yellow]Could not parse logs:[/yellow] {exc}")
            continue

        task_id = pipeline.create_fix_task(ctx, run_url=failure.failure_url)
        if task_id:
            console.print(f"  [green]Task created:[/green] {task_id}")
        else:
            console.print("  [yellow]Failed to create task (server down?)[/yellow]")
