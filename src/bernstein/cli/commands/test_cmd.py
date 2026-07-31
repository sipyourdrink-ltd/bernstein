"""Test CLI: run automated tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from bernstein.cli.helpers import SERVER_URL, console


@click.command("test")
@click.option("--duration", default=300, help="Test duration in seconds.")
@click.option("--workdir", default=".", help="Project root.", type=click.Path(exists=True))
def test_cmd(duration: int, workdir: str) -> None:
    """Run automated resilience tests."""
    from bernstein.benchmark.golden import GoldenEvalRunner

    console.print(f"[bold]Resilience Test started (duration={duration}s)[/bold]\n")
    runner = GoldenEvalRunner(Path(workdir), SERVER_URL)
    summary = asyncio.run(runner.run_suite())
    console.print("\n[bold]Test Completed.[/bold]")
    console.print(f"Passed: {summary['passed']}/{summary['total_tasks']}")

    if summary["failed"] > 0:
        console.print("[red]Regressions detected during test![/red]")
        raise SystemExit(1)
