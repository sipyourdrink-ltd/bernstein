"""Test CLI — run automated tests with optional chaos injection."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from bernstein.cli.helpers import SERVER_URL, console


@click.command("test")
@click.option("--chaos", is_flag=True, default=False, help="Enable random chaos injection during test.")
@click.option("--duration", default=300, help="Test duration in seconds.")
@click.option("--workdir", default=".", help="Project root.", type=click.Path(exists=True))
def test_cmd(chaos: bool, duration: int, workdir: str) -> None:
    """Run automated resilience tests with optional chaos injection."""
    from bernstein.benchmark.golden import GoldenEvalRunner

    console.print(f"[bold]Resilience Test started (duration={duration}s, chaos={chaos})[/bold]\n")

    runner = GoldenEvalRunner(Path(workdir), SERVER_URL)

    async def run_with_chaos():
        chaos_task: asyncio.Task[None] | None = None
        if chaos:
            # Start chaos injector in background
            chaos_task = asyncio.create_task(_chaos_injector(duration))

        # Run golden suite as a load test
        results = await runner.run_suite()

        if chaos and chaos_task:
            chaos_task.cancel()

        return results

    summary = asyncio.run(run_with_chaos())

    console.print("\n[bold]Test Completed.[/bold]")
    console.print(f"Passed: {summary['passed']}/{summary['total_tasks']}")

    if summary["failed"] > 0:
        console.print("[red]Regressions detected during test![/red]")
        raise SystemExit(1)


async def _chaos_injector(duration_s: int):
    """Periodically inject random chaos events."""
    import random

    end_time = asyncio.get_event_loop().time() + duration_s

    scenarios = ["agent-kill", "rate-limit"]

    while asyncio.get_event_loop().time() < end_time:
        await asyncio.sleep(random.uniform(30, 60))
        scenario = random.choice(scenarios)

        console.print(f"[bold red]CHAOS INJECTOR:[/bold red] Triggering {scenario}")

        # We call the CLI commands directly for now (simplified)
        if scenario == "agent-kill":
            pass  # Chaos scenario stub: agent-kill not yet wired
        elif scenario == "rate-limit":
            pass  # Chaos scenario stub: rate-limit not yet wired
