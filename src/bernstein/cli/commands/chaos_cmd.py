"""Chaos engineering CLI for agent systems.

Periodically inject failures to test resilience:
- Kill a random agent mid-task
- Simulate rate limit
- Remove a file being edited
- Inject random errors

Usage:
  bernstein chaos agent-kill     Kill a random active agent
  bernstein chaos rate-limit     Simulate API rate limiting
  bernstein chaos file-remove    Remove a file an agent is editing
  bernstein chaos status         Show chaos experiment history
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console

CHAOS_DIR = Path(".sdd/runtime/chaos")


@click.group("chaos")
def chaos_group() -> None:
    """Chaos engineering: inject failures to test agent resilience."""


def _find_active_agents() -> list[tuple[str, int]]:
    """Scan .sdd/runtime/agents/ for agents with live PIDs."""
    from bernstein.cli.helpers import is_process_alive

    agents_dir = Path(".sdd/runtime/agents")
    if not agents_dir.is_dir():
        return []

    active: list[tuple[str, int]] = []
    for agent_dir in agents_dir.iterdir():
        if not agent_dir.is_dir():
            continue
        pid_file = agent_dir / "pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if is_process_alive(pid):
                    active.append((agent_dir.name, pid))
            except (ValueError, OSError):
                continue
    return active


def _select_target(
    active: list[tuple[str, int]],
    agent_id: str | None,
) -> tuple[str, int] | None:
    """Pick the target agent to kill; returns None on mismatch."""
    if agent_id:
        targets = [(name, pid) for name, pid in active if name == agent_id]
        if not targets:
            console.print(f"[red]Agent '{agent_id}' not found or not active.[/red]")
            return None
        return targets[0]
    return random.choice(active)


@chaos_group.command("agent-kill")
@click.option("--agent-id", default=None, help="Specific agent to kill (default: random).")
def agent_kill(agent_id: str | None) -> None:
    """Kill a random active agent mid-task to test crash recovery."""
    import signal as _signal

    active = _find_active_agents()
    if not active:
        console.print("[yellow]No active agents to kill.[/yellow]")
        return

    target = _select_target(active, agent_id)
    if target is None:
        return
    target_name, target_pid = target

    console.print(f"[bold red]CHAOS:[/bold red] Killing agent {target_name} (PID {target_pid})")

    from bernstein.core.platform_compat import kill_process

    if kill_process(target_pid, _signal.SIGTERM):
        _record_chaos_event("agent-kill", target_name, success=True)
        console.print(f"[green]Agent {target_name} killed. Crash recovery should handle it.[/green]")
    else:
        _record_chaos_event("agent-kill", target_name, success=False, error="kill_process returned False")
        console.print("[red]Failed to kill agent.[/red]")


@chaos_group.command("rate-limit")
@click.option("--duration", default=60, help="Simulated rate limit duration in seconds.")
@click.option("--provider", default="claude", help="Provider to simulate rate limit for.")
def rate_limit(duration: int, provider: str) -> None:
    """Simulate API rate limiting to test fallback routing."""
    CHAOS_DIR.mkdir(parents=True, exist_ok=True)
    rate_limit_file = CHAOS_DIR / "rate_limit_active.json"

    event = {
        "provider": provider,
        "started_at": time.time(),
        "duration_seconds": duration,
        "expires_at": time.time() + duration,
    }

    rate_limit_file.write_text(json.dumps(event, indent=2))
    _record_chaos_event("rate-limit", f"{provider} for {duration}s", success=True)

    console.print(
        Panel(
            f"Provider [bold]{provider}[/bold] rate-limited for [bold]{duration}s[/bold]\n"
            f"Expires at: {time.strftime('%H:%M:%S', time.localtime(event['expires_at']))}\n\n"
            "The orchestrator should detect this and route to fallback providers.",
            title="[red]CHAOS: Rate Limit Simulation[/red]",
        )
    )


@chaos_group.command("file-remove")
@click.option("--pattern", default="*.py", help="Glob pattern for files to consider.")
def file_remove(pattern: str) -> None:
    """Remove a random file from agent worktrees to test error handling."""
    worktrees_dir = Path(".claude/worktrees")
    if not worktrees_dir.is_dir():
        console.print("[yellow]No agent worktrees found.[/yellow]")
        return

    candidates: list[Path] = []
    for wt in worktrees_dir.iterdir():
        if wt.is_dir():
            candidates.extend(wt.glob(f"src/**/{pattern}"))

    # Filter out __init__.py and critical files
    candidates = [f for f in candidates if f.name != "__init__.py" and not f.name.startswith(".")]

    if not candidates:
        console.print("[yellow]No suitable files found in worktrees.[/yellow]")
        return

    target = random.choice(candidates)
    console.print(f"[bold red]CHAOS:[/bold red] Removing {target}")

    try:
        # Preserve content for recovery audit
        content = target.read_text()
        backup = target.with_suffix(target.suffix + ".chaos_backup")
        backup.write_text(content)
        target.unlink()
        _record_chaos_event("file-remove", str(target), success=True)
        console.print(f"[green]File removed. Backup at {backup}[/green]")
        console.print("Agent should detect the missing file and handle gracefully.")
    except OSError as exc:
        _record_chaos_event("file-remove", str(target), success=False, error=str(exc))
        console.print(f"[red]Failed: {exc}[/red]")


@chaos_group.command("agent-oom")
@click.option("--agent-id", default=None, help="Specific agent to target.")
def agent_oom(agent_id: str | None) -> None:
    """Simulate OOM by injecting memory-intensive task to an agent."""
    # For now we just record it, as real OOM is hard to inject safely from outside
    # without agent cooperation.
    target = agent_id or "random-active"
    console.print(f"[bold red]CHAOS:[/bold red] Simulating OOM for agent {target}")
    _record_chaos_event("agent-oom", target, success=True)


@chaos_group.command("disk-full")
@click.option("--duration", default=60, help="Duration in seconds.")
def disk_full(duration: int) -> None:
    """Simulate disk full condition for all components."""
    CHAOS_DIR.mkdir(parents=True, exist_ok=True)
    disk_full_file = CHAOS_DIR / "disk_full_active.json"

    event = {
        "started_at": time.time(),
        "duration_seconds": duration,
        "expires_at": time.time() + duration,
    }

    disk_full_file.write_text(json.dumps(event, indent=2))
    _record_chaos_event("disk-full", f"all for {duration}s", success=True)

    console.print(
        Panel(
            f"Simulating disk full for [bold]{duration}s[/bold]\n"
            f"Expires at: {time.strftime('%H:%M:%S', time.localtime(event['expires_at']))}",
            title="[red]CHAOS: Disk Full Simulation[/red]",
        )
    )


@chaos_group.command("status")
@click.option("--limit", default=20, help="Number of recent events to show.")
def chaos_status(limit: int) -> None:
    """Show chaos experiment history."""
    log_path = CHAOS_DIR / "chaos_log.jsonl"
    if not log_path.exists():
        console.print("[yellow]No chaos experiments recorded yet.[/yellow]")
        console.print("Run [bold]bernstein chaos agent-kill[/bold] to get started.")
        return

    events: list[dict[str, object]] = []
    try:
        for line in log_path.read_text().strip().split("\n"):
            if line.strip():
                events.append(json.loads(line))
    except (json.JSONDecodeError, OSError) as exc:
        console.print(f"[red]Error reading chaos log: {exc}[/red]")
        return

    events = events[-limit:]

    table = Table(title="Chaos Experiment History")
    table.add_column("Time", style="dim")
    table.add_column("Scenario", style="bold")
    table.add_column("Target")
    table.add_column("Result")

    for event in reversed(events):
        ts = time.strftime("%H:%M:%S", time.localtime(float(str(event.get("timestamp", 0)))))
        scenario = str(event.get("scenario", ""))
        target = str(event.get("target", ""))
        success = event.get("success", False)
        result_style = "green" if success else "red"
        result_text = "OK" if success else str(event.get("error", "FAIL"))
        table.add_row(ts, scenario, target[:40], f"[{result_style}]{result_text}[/{result_style}]")

    console.print(table)
    _show_active_rate_limit()


def _show_active_rate_limit() -> None:
    """Show active rate-limit simulation if one exists and is not expired."""
    rate_limit_file = CHAOS_DIR / "rate_limit_active.json"
    if not rate_limit_file.exists():
        return
    try:
        rl = json.loads(rate_limit_file.read_text())
        expires_at = float(str(rl.get("expires_at", 0)))
        if expires_at <= time.time():
            rate_limit_file.unlink(missing_ok=True)
            return
        console.print(
            f"\n[yellow]Active rate limit simulation:[/yellow] "
            f"provider={rl.get('provider')} "
            f"expires={time.strftime('%H:%M:%S', time.localtime(expires_at))}"
        )
    except (json.JSONDecodeError, OSError):
        pass


@chaos_group.command("slo")
def chaos_slo() -> None:
    """Show current SLO dashboard with traffic-light status."""
    slo_path = Path(".sdd/metrics/slos.json")
    if not slo_path.exists():
        console.print("[yellow]No SLO data available yet.[/yellow]")
        console.print("SLOs are tracked automatically during [bold]bernstein run[/bold].")
        return

    try:
        data = json.loads(slo_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        console.print(f"[red]Error reading SLO data: {exc}[/red]")
        return

    # SLO table
    table = Table(title="Agent SLO Dashboard")
    table.add_column("SLO", style="bold")
    table.add_column("Target")
    table.add_column("Current")
    table.add_column("Status")

    status_icons = {
        "green": "[green]\u25cf GREEN[/green]",
        "yellow": "[yellow]\u25cf YELLOW[/yellow]",
        "red": "[red]\u25cf RED[/red]",
    }

    for slo in data.get("slos", []):
        status = status_icons.get(slo.get("status", ""), slo.get("status", ""))
        table.add_row(
            slo.get("description", ""),
            f"{slo.get('target', 0):.0%}",
            f"{slo.get('current', 0):.1%}",
            status,
        )

    console.print(table)

    # Error budget
    eb = data.get("error_budget", {})
    budget_status = status_icons.get(eb.get("status", ""), eb.get("status", ""))
    console.print(
        Panel(
            f"Total tasks: {eb.get('total_tasks', 0)}  |  "
            f"Failed: {eb.get('failed_tasks', 0)}  |  "
            f"Budget remaining: {eb.get('budget_remaining', 0)}/{eb.get('budget_total', 0)}  |  "
            f"Status: {budget_status}",
            title="Error Budget",
        )
    )

    actions = data.get("actions", [])
    if actions:
        console.print(f"\n[bold red]Active remediation:[/bold red] {', '.join(actions)}")


def _record_chaos_event(
    scenario: str,
    target: str,
    *,
    success: bool,
    error: str = "",
) -> None:
    """Append a chaos event to the log."""
    CHAOS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = CHAOS_DIR / "chaos_log.jsonl"
    event = {
        "scenario": scenario,
        "target": target,
        "success": success,
        "error": error,
        "timestamp": time.time(),
    }
    try:
        with log_path.open("a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass  # Best-effort logging; non-critical
