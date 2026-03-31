"""Stop commands: soft/hard stop, shutdown signals, session save, ticket recovery."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import time
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import (
    SDD_PID_SERVER,
    SDD_PID_SPAWNER,
    SDD_PID_WATCHDOG,
    SERVER_URL,
    auth_headers,
    console,
    is_alive,
    kill_pid_hard,
    print_banner,
)

# ---------------------------------------------------------------------------
# Shared helpers used by stop and the main CLI group
# ---------------------------------------------------------------------------


def write_shutdown_signals(reason: str = "User requested stop") -> list[str]:
    """Write SHUTDOWN signal files for all active agents.

    Creates a ``SHUTDOWN`` file in ``.sdd/runtime/signals/{session_id}/``
    for each agent listed in ``agents.json``.  Agents that poll for signal
    files will see this and save their work before exiting.

    Args:
        reason: Human-readable reason written into the signal file.

    Returns:
        List of session IDs that were signaled.
    """
    signals_dir = Path(".sdd/runtime/signals")
    agents_json = Path(".sdd/runtime/agents.json")
    signaled: list[str] = []
    if not agents_json.exists():
        return signaled
    try:
        agent_data = json.loads(agents_json.read_text())
        for agent in agent_data.get("agents", []):
            session_id: str = agent.get("id", "")
            if session_id:
                sig_dir = signals_dir / session_id
                sig_dir.mkdir(parents=True, exist_ok=True)
                (sig_dir / "SHUTDOWN").write_text(
                    f"# SHUTDOWN\nReason: {reason}\nSave your work, commit WIP, and exit.\n"
                )
                signaled.append(session_id)
    except (OSError, ValueError):
        pass
    return signaled


def return_claimed_to_open() -> int:
    """Move all claimed backlog tickets back to open.

    Files in ``.sdd/backlog/claimed/`` are moved to ``.sdd/backlog/open/``
    so they can be picked up by the next run.  Files whose ticket number
    already exists in ``backlog/closed/`` (i.e. duplicate of a completed
    task) are silently deleted instead.

    Returns:
        Number of files moved back to open.
    """
    claimed_dir = Path(".sdd/backlog/claimed")
    open_dir = Path(".sdd/backlog/open")
    if not claimed_dir.exists():
        return 0

    open_dir.mkdir(parents=True, exist_ok=True)

    closed_nums: set[str] = set()
    closed_dir = Path(".sdd/backlog/closed")
    if closed_dir.exists():
        closed_nums = {f.name.split("-")[0] for f in [*closed_dir.glob("*.yaml"), *closed_dir.glob("*.md")]}
    # Also check backlog/done/ which some codepaths use
    done_dir = Path(".sdd/backlog/done")
    if done_dir.exists():
        closed_nums |= {f.name.split("-")[0] for f in [*done_dir.glob("*.yaml"), *done_dir.glob("*.md")]}

    count = 0
    for f in [*claimed_dir.glob("*.yaml"), *claimed_dir.glob("*.md")]:
        num = f.name.split("-")[0]
        if num in closed_nums:
            f.unlink()  # already completed — remove duplicate
        else:
            f.rename(open_dir / f.name)
            count += 1
    return count


def save_session_on_stop(workdir: Path) -> None:
    """Persist session state to disk so the next run can resume quickly.

    Queries the running task server for current task statuses and writes a
    proper ``session.json`` snapshot via the session module.  Falls back to
    a lightweight ``session_state.json`` diagnostic file if the server is
    unreachable.

    Args:
        workdir: Project root directory containing ``.sdd/``.
    """
    # Try to save a rich session.json (used by bootstrap for fast resume)
    saved_proper = False
    with contextlib.suppress(Exception):
        import httpx as _httpx

        from bernstein.core.session import SessionState, save_session

        resp = _httpx.get(f"{SERVER_URL}/tasks", timeout=3.0, headers=auth_headers())
        resp.raise_for_status()
        task_list: list[dict[str, Any]] = resp.json() if isinstance(resp.json(), list) else []
        done_ids = [t["id"] for t in task_list if t.get("status") == "done"]
        pending_ids = [t["id"] for t in task_list if t.get("status") in ("claimed", "in_progress")]
        state = SessionState(
            saved_at=time.time(),
            goal="",
            completed_task_ids=done_ids,
            pending_task_ids=pending_ids,
            cost_spent=0.0,
        )
        save_session(workdir, state)
        saved_proper = True

    if not saved_proper:
        # Fallback: lightweight diagnostic snapshot (not used by resume logic)
        runtime_dir = workdir / ".sdd" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        fallback: dict[str, Any] = {
            "stopped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "open_tasks": sum(1 for _ in (workdir / ".sdd" / "backlog" / "open").glob("*.yaml"))
            if (workdir / ".sdd" / "backlog" / "open").exists()
            else 0,
            "claimed_tasks": sum(1 for _ in (workdir / ".sdd" / "backlog" / "claimed").glob("*.yaml"))
            if (workdir / ".sdd" / "backlog" / "claimed").exists()
            else 0,
        }
        (runtime_dir / "session_state.json").write_text(json.dumps(fallback, indent=2))


def recover_orphaned_claims() -> int:
    """On startup, return claimed tickets from dead sessions to open.

    Since we are starting a fresh run, any tickets still in
    ``backlog/claimed/`` are orphaned from a previous session and should
    be returned to ``backlog/open/`` so they can be picked up again.

    Returns:
        Number of tickets returned to open.
    """
    return return_claimed_to_open()


def sigint_handler(signum: int, frame: Any) -> None:
    """Handle Ctrl+C: save state, return claimed tickets, then exit.

    This handler is installed while the dashboard is running so that an
    interactive Ctrl+C still persists session state and avoids orphaning
    claimed tickets.

    Args:
        signum: Signal number (always ``SIGINT``).
        frame: Current stack frame (unused).
    """
    console.print("\n[yellow]Ctrl+C received — saving state…[/yellow]")
    with contextlib.suppress(OSError):
        save_session_on_stop(Path.cwd())
    moved = return_claimed_to_open()
    if moved:
        console.print(f"[dim]Returned {moved} claimed ticket(s) to open.[/dim]")
    console.print("[yellow]Use 'bernstein stop' for graceful shutdown.[/yellow]")
    raise SystemExit(130)


def register_sigint_handler() -> None:
    """Install :func:`sigint_handler` for ``SIGINT``."""
    signal.signal(signal.SIGINT, sigint_handler)


# ---------------------------------------------------------------------------
# Soft / hard stop implementation
# ---------------------------------------------------------------------------


def soft_stop(timeout: int) -> None:
    """Graceful drain via DrainCoordinator.

    Args:
        timeout: Maximum seconds to wait for agents to exit gracefully.
    """
    import asyncio

    from bernstein.core.drain import DrainConfig, DrainCoordinator

    workdir = Path.cwd()
    config = DrainConfig(wait_timeout_s=timeout)
    coordinator = DrainCoordinator(workdir, config=config)

    def on_update(phase: object, agents: object) -> None:
        # Print progress to stdout.
        name = getattr(phase, "name", "")
        number = getattr(phase, "number", 0)
        detail = getattr(phase, "detail", "")
        print(f"\r  Phase {number}/6: {name} -- {detail}    ", end="", flush=True)

    report = asyncio.run(coordinator.run(callback=on_update))
    print()  # newline after carriage returns

    # Print summary.
    merged_count = sum(1 for m in report.merges if m.action == "merged")
    console.print("\n[bold]Drain complete:[/bold]")
    console.print(f"  Tasks: {report.tasks_done} done, {report.tasks_partial} partial")
    console.print(f"  Merged: {merged_count} branches")
    console.print(f"  Cleanup: {report.worktrees_removed} worktrees, {report.branches_deleted} branches")
    console.print(f"  Duration: {report.total_duration_s:.0f}s")


def _kill_agent_pid(pid: int, label: str, killed: set[int]) -> None:
    """SIGKILL an agent process group, tracking killed PIDs."""
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    killed.add(pid)
    console.print(f"[red]Killed agent {label} (PID {pid}) with SIGKILL[/red]")


def hard_stop() -> None:
    """Hard stop: SIGKILL everything, best-effort save, return tickets."""
    # 1. Kill watchdog immediately
    kill_pid_hard(SDD_PID_WATCHDOG, "Watchdog")

    # 2. Kill spawner immediately
    kill_pid_hard(SDD_PID_SPAWNER, "Spawner")

    # 3. Kill all spawned agents with SIGKILL.
    #    Try multiple sources: agents.json, PID files, and process scan.
    killed_pids: set[int] = set()

    # Source A: agents.json
    agents_json = Path(".sdd/runtime/agents.json")
    if agents_json.exists():
        try:
            agent_data = json.loads(agents_json.read_text())
            for agent in agent_data.get("agents", []):
                pid = agent.get("pid")
                if pid and is_alive(pid):
                    _kill_agent_pid(pid, agent.get("id", "?"), killed_pids)
        except (OSError, ValueError):
            pass

    # Source B: PID metadata files
    pids_dir = Path(".sdd/runtime/pids")
    if pids_dir.is_dir():
        for pid_file in pids_dir.glob("*.json"):
            try:
                meta = json.loads(pid_file.read_text())
                pid = int(meta.get("pid", 0))
                if pid and pid not in killed_pids and is_alive(pid):
                    _kill_agent_pid(pid, meta.get("session_id", pid_file.stem), killed_pids)
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    # Source C: scan for claude agent processes (catches anything missed above)
    import subprocess as _sp

    try:
        result = _sp.run(
            ["pgrep", "-f", "claude.*--dangerously-skip-permissions"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split():
            if line.isdigit():
                pid = int(line)
                if pid not in killed_pids:
                    _kill_agent_pid(pid, f"orphan-{pid}", killed_pids)
    except Exception:
        pass

    # 4. Kill server immediately
    kill_pid_hard(SDD_PID_SERVER, "Task server")

    # 5. Best-effort session save
    try:
        save_session_on_stop(Path.cwd())
        console.print("[dim]Session state saved (best-effort).[/dim]")
    except OSError:
        console.print("[yellow]Could not save session state.[/yellow]")

    # 6. Return claimed tickets to open
    try:
        moved = return_claimed_to_open()
        if moved:
            console.print(f"[dim]Returned {moved} claimed ticket(s) to open.[/dim]")
    except OSError:
        console.print("[yellow]Could not return claimed tickets.[/yellow]")

    console.print("\n[red]Bernstein stopped (hard).[/red]")


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("stop")
@click.option(
    "--timeout",
    default=30,
    show_default=True,
    help="Seconds to wait for agents (soft stop).",
)
@click.option(
    "--force",
    "--hard",
    is_flag=True,
    default=False,
    help="Hard stop: kill immediately without waiting.",
)
def stop(timeout: int, force: bool) -> None:
    """Stop all agents and the task server.

    Default (soft stop): writes SHUTDOWN signal files so agents can save
    their work, waits up to ``--timeout`` seconds, saves session state,
    returns claimed tickets to open, then kills remaining processes with
    SIGTERM.

    With ``--force`` / ``--hard``: skips signal files and waiting, kills
    everything immediately with SIGKILL, then does best-effort session
    save and ticket recovery.
    """
    print_banner()

    if force:
        console.print("[bold red]Hard stop — killing everything immediately…[/bold red]\n")
        hard_stop()
    else:
        console.print("[bold]Soft stop — giving agents time to save…[/bold]\n")
        soft_stop(timeout)
