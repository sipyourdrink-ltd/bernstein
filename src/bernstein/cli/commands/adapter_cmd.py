"""One-shot adapter smoke command."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import click

from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.registry import get_adapter
from bernstein.cli.helpers import console
from bernstein.core.models import ModelConfig

_DEFAULT_SMOKE_MODELS: dict[str, str] = {
    "aider": "sonnet",
    "amp": "sonnet",
    "claude": "sonnet",
    "codex": "gpt-5.4-mini",
    "cursor": "sonnet",
    "gemini": "gemini-3-flash",
    "kiro": "sonnet",
    "kilo": "sonnet",
    "opencode": "gpt-5.4-mini",
    "qwen": "qwen-coder",
}


def _read_last_lines(log_path: Path, n: int = 40) -> list[str]:
    """Read the last N lines from a log file."""
    try:
        return log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def _wait_for_exit(adapter: Any, result: Any, timeout: int) -> str | int:
    """Wait for the spawned adapter process to finish or time out."""
    if not (result.proc and hasattr(result.proc, "wait")):
        console.print("[yellow]Warning: adapter did not return a waitable process handle.[/yellow]")
        return "running"
    try:
        return result.proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        console.print(f"[yellow]Timeout after {timeout}s — killing pid {result.pid}[/yellow]")
        adapter.kill(result.pid)
        return "timed out"


def _print_log_tail(result: Any) -> None:
    """Print the last 40 lines of the adapter log."""
    if not result.log_path.exists():
        console.print(f"[red]Log file missing:[/red] {result.log_path}")
        return
    lines = _read_last_lines(result.log_path, n=40)
    console.print("\n[bold]--- Last 40 lines of log --------------------------------------------------[/bold]")
    if not lines:
        console.print("[dim](log is empty)[/dim]")
    for line in lines:
        console.print(line)
    console.print("[bold]--------------------------------------------------------------------------[/bold]\n")


def _check_expected_file(prompt: str, worktree: Path) -> None:
    """Heuristic check for a file path mentioned in the prompt."""
    match = re.search(r'(?:file|path)\s+([^\s\'"]+)', prompt, re.I)
    if not match:
        match = re.search(r"(/[\w\.\-/]+|[\w\.\-/]+\.\w+)", prompt)
    if not match:
        return
    expected_path = Path(match.group(1))
    if not expected_path.is_absolute():
        expected_path = worktree / expected_path
    if expected_path.exists():
        console.print(f"[green]\u2713 Expected file exists:[/green] {expected_path}")
    else:
        console.print(f"[red]\u2717 Expected file missing:[/red] {expected_path}")


@click.command("test-adapter")
@click.option("--adapter", "adapter_name", required=True, help="Adapter to test (e.g. gemini, codex).")
@click.option("--task", "prompt", required=True, help="Task for the adapter to execute.")
@click.option("--model", default=None, help="Model to use for the smoke run.")
@click.option("--timeout", type=int, default=120, help="Wait up to N seconds for exit.")
def test_adapter(adapter_name: str, prompt: str, model: str | None, timeout: int) -> None:
    """Spawn a single headless adapter run, wait for exit, and verify output."""
    resolved_model = model or _DEFAULT_SMOKE_MODELS.get(adapter_name, "sonnet")
    adapter = get_adapter(adapter_name)
    timestamp = int(time.time())
    session_id = f"test-{adapter_name}-{timestamp}"

    worktree = Path.cwd() / ".sdd" / "worktrees" / session_id
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".sdd" / "runtime").mkdir(parents=True, exist_ok=True)

    result: Any = None
    try:
        console.print(f"[bold]Testing adapter:[/bold] {adapter_name} (model={resolved_model})")
        console.print(f"[dim]Workdir: {worktree}[/dim]")
        console.print(f"[dim]Task: {prompt}[/dim]\n")

        result = adapter.spawn(
            prompt=prompt,
            workdir=worktree,
            model_config=ModelConfig(model=resolved_model, effort="medium"),
            session_id=session_id,
            timeout_seconds=timeout,
        )

        exit_code = _wait_for_exit(adapter, result, timeout)
        console.print(f"\n[bold]Exit code:[/bold] {exit_code}")
        _print_log_tail(result)
        _check_expected_file(prompt, worktree)

    except Exception as exc:
        console.print(f"[red]Error during adapter test:[/red] {exc}")
        raise SystemExit(1) from exc
    finally:
        if result is not None:
            CLIAdapter.cancel_timeout(result)
        if worktree.exists():
            try:
                shutil.rmtree(worktree)
                console.print(f"[dim]Cleaned up worktree: {worktree}[/dim]")
            except Exception as e:
                console.print(f"[yellow]Warning: failed to clean up {worktree}: {e}[/yellow]")
