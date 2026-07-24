"""One-shot adapter smoke command and the ``adapters`` discovery group."""

from __future__ import annotations

import importlib
import inspect
import json
import operator
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

# Override map for adapters whose on-disk CLI binary name differs from the
# registry key. Used by ``bernstein adapters list`` to detect whether the
# upstream CLI is installed locally. Missing entries fall back to the
# registry key.
_BINARY_OVERRIDES: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "devin_terminal": "devin",
    "q_dev": "q",
    "open_interpreter": "interpreter",
    "openai_agents": "python",  # SDK adapter, not a standalone binary
    "letta_code": "letta",
    "continue": "continue",
    "openhands": "openhands",
    "mock": "",  # internal test adapter, no binary
    "generic": "",  # generic wrapper, no binary
    "composio": "ao",
    "devin": "devin",
}

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
        console.print(f"[yellow]Timeout after {timeout}s: killing pid {result.pid}[/yellow]")
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
    match = re.search(r'(?:file|path)\s+([^\s\'"]+)', prompt, re.IGNORECASE)
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


def _resolve_source_file(adapter_obj: Any) -> str:
    """Return a repo-relative source path for the adapter's defining module."""
    try:
        target = adapter_obj if inspect.isclass(adapter_obj) else type(adapter_obj)
        src = inspect.getsourcefile(target) or inspect.getfile(target)
    except (TypeError, OSError):
        return "<unknown>"
    if not src:
        return "<unknown>"
    path = Path(src).resolve()
    parts = path.parts
    if "bernstein" in parts:
        idx = parts.index("bernstein")
        return str(Path(*parts[idx:]))
    return path.name


def _binary_for_adapter(name: str) -> str:
    """Map a registry key to its expected CLI binary name.

    Explicit overrides win, then the adapter's capability profile (which
    already declares its binary), then the registry key itself. Mirrors
    :func:`bernstein.adapters.report._binary_for_adapter` so this surface
    and ``bernstein adapters check`` agree.
    """
    if name in _BINARY_OVERRIDES:
        return _BINARY_OVERRIDES[name]
    from bernstein.adapters.capability_profile import profile_binary_for

    return profile_binary_for(name) or name


def _enumerate_adapters() -> list[dict[str, str]]:
    """Snapshot the live adapter registry as plain dicts.

    The registry is the source of truth - we never enumerate by scanning
    files. Includes the synthetic ``generic`` adapter, which is built
    on demand inside ``get_adapter`` and not present in the ``_ADAPTERS``
    dict.
    """
    # Import lazily so test patches at module level keep working.
    registry = importlib.import_module("bernstein.adapters.registry")
    registry._load_entrypoint_adapters()
    rows: list[dict[str, str]] = []
    for name, adapter in sorted(registry._ADAPTERS.items()):
        binary = _binary_for_adapter(name)
        installed = bool(binary) and shutil.which(binary) is not None
        rows.append(
            {
                "name": name,
                "source": _resolve_source_file(adapter),
                "binary": binary,
                "status": "installed" if installed else "missing",
            }
        )
    # The ``generic`` adapter is constructed lazily inside ``get_adapter``;
    # surface it here so the listing matches what ``--help`` would suggest.
    if not any(r["name"] == "generic" for r in rows):
        rows.append(
            {
                "name": "generic",
                "source": _resolve_source_file(importlib.import_module("bernstein.adapters.generic").GenericAdapter),
                "binary": "",
                "status": "n/a",
            }
        )
    rows.sort(key=operator.itemgetter("name"))
    return rows


@click.group("adapters")
def adapters_group() -> None:
    """Inspect Bernstein's CLI agent adapters."""


# Wire the contract-check sub-command into the ``adapters`` group. Kept
# behind a lazy import so the click decorators don't fire if the
# contract module is missing (e.g. when bernstein is installed as a
# wheel without the tests/contract/ tree).
def _register_contract_subcommand() -> None:
    try:
        from bernstein.cli.commands.adapters_contract_cmd import contract_check_cmd
    except Exception:  # pragma: no cover -- defensive
        return
    adapters_group.add_command(contract_check_cmd, "contract-check")


_register_contract_subcommand()


def _register_check_subcommand() -> None:
    """Attach the conformance + capability report subcommands."""
    try:
        from bernstein.cli.commands.adapters_cmd import register_adapters_check
    except Exception:  # pragma: no cover -- defensive
        return
    register_adapters_check(adapters_group)


_register_check_subcommand()


@adapters_group.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of a Rich table.",
)
def list_adapters(as_json: bool) -> None:
    """Enumerate every CLI adapter Bernstein knows about.

    Pulls the catalogue from ``bernstein.adapters.registry`` (built-ins
    plus any third-party adapters registered through the
    ``bernstein.adapters`` entry-point group). For each adapter we also
    report whether its upstream CLI binary is on ``$PATH``.
    """
    rows = _enumerate_adapters()

    if as_json:
        click.echo(json.dumps({"count": len(rows), "adapters": rows}, indent=2, sort_keys=True))
        return

    from rich.table import Table

    table = Table(title=f"Bernstein adapters ({len(rows)})", show_lines=False)
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("source", style="dim")
    table.add_column("binary", style="white")
    table.add_column("status", style="green")
    for row in rows:
        status_style = {
            "installed": "[green]installed[/green]",
            "missing": "[yellow]missing[/yellow]",
            "n/a": "[dim]n/a[/dim]",
        }[row["status"]]
        table.add_row(row["name"], row["source"], row["binary"] or "-", status_style)
    console.print(table)
