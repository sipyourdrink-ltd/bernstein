"""CLI command for A/B testing two models on the same task.

Usage:
    bernstein ab-test --model-a opus --model-b sonnet --task "Fix the login bug"
"""

from __future__ import annotations

import click

from bernstein.cli.helpers import SERVER_URL, console


@click.command("ab-test")
@click.option("--model-a", required=True, help="First model to test (e.g. opus).")
@click.option("--model-b", required=True, help="Second model to test (e.g. sonnet).")
@click.option("--task", "task_description", required=True, help="Task description for both models.")
@click.option("--role", default="backend", show_default=True, help="Agent role.")
@click.option(
    "--scope",
    type=click.Choice(["small", "medium", "large"]),
    default="medium",
    show_default=True,
    help="Task scope.",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=int,
    default=1800,
    show_default=True,
    help="Max seconds to wait for both tasks.",
)
def ab_test_cmd(
    model_a: str,
    model_b: str,
    task_description: str,
    role: str,
    scope: str,
    timeout_seconds: int,
) -> None:
    """Run the same task with two models and compare results.

    Creates two identical tasks pinned to MODEL_A and MODEL_B, waits for
    both to finish, then prints a side-by-side comparison.

    \b
      bernstein ab-test --model-a opus --model-b sonnet --task "Add JWT auth"
      bernstein ab-test --model-a opus --model-b haiku --task "Fix flaky test" --role qa
    """
    from rich.markdown import Markdown

    from bernstein.core.ab_test import ABTestConfig, run_ab_test

    config = ABTestConfig(
        task_description=task_description,
        model_a=model_a,
        model_b=model_b,
        role=role,
        scope=scope,
        timeout_seconds=timeout_seconds,
    )

    console.print(
        f"[bold]A/B Test[/bold] — {model_a} vs {model_b}\n"
        f"[dim]Task:[/dim] {task_description}\n"
        f"[dim]Role:[/dim] {role}  [dim]Scope:[/dim] {scope}  "
        f"[dim]Timeout:[/dim] {timeout_seconds}s\n"
    )
    console.print("[dim]Creating tasks on server...[/dim]")

    try:
        report = run_ab_test(config, SERVER_URL)
    except Exception as exc:
        console.print(f"[red]A/B test failed:[/red] {exc}")
        raise SystemExit(1) from exc

    md = report.to_markdown()
    console.print(Markdown(md))

    if report.timed_out:
        console.print("[yellow]Warning: test timed out before both tasks completed.[/yellow]")
        raise SystemExit(2)
