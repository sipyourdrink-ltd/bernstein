"""Stop commands: soft/hard stop, shutdown signals, session save, ticket recovery."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import click

from bernstein.cli.display.icons import get_icons
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
    read_pid,
    sigkill_pid,
)
from bernstein.core.process_utils import process_cwd
from bernstein.core.runtime_state import read_supervisor_state

_LABEL_TASK_SERVER = "Task server"
_AGENTS_JSON_PATH = ".sdd/runtime/agents.json"
_YAML_GLOB = "*.yaml"

# ---------------------------------------------------------------------------
# Shared helpers used by stop and the main CLI group
# ---------------------------------------------------------------------------


def _unregister_mcp_discovery(workdir: Path) -> None:
    """Remove the Bernstein entry from .claude/mcp.json on shutdown.

    Prevents stale MCP server references in Claude Code sessions after
    Bernstein has stopped.

    Args:
        workdir: Project root directory.
    """
    mcp_path = workdir / ".claude" / "mcp.json"
    if not mcp_path.exists():
        return
    try:
        data = json.loads(mcp_path.read_text())
    except (ValueError, OSError):
        return
    servers = data.get("mcpServers", {})
    if "bernstein" not in servers:
        return
    servers.pop("bernstein")
    data["mcpServers"] = servers
    with contextlib.suppress(OSError):
        mcp_path.write_text(json.dumps(data, indent=2) + "\n")


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
    agents_json = Path(_AGENTS_JSON_PATH)
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
        closed_nums = {f.name.split("-")[0] for f in [*closed_dir.glob(_YAML_GLOB), *closed_dir.glob("*.md")]}
    # Also check backlog/done/ which some codepaths use
    done_dir = Path(".sdd/backlog/done")
    if done_dir.exists():
        closed_nums |= {f.name.split("-")[0] for f in [*done_dir.glob(_YAML_GLOB), *done_dir.glob("*.md")]}

    count = 0
    for f in [*claimed_dir.glob(_YAML_GLOB), *claimed_dir.glob("*.md")]:
        if not f.exists():
            continue
        num = f.name.split("-")[0]
        try:
            if num in closed_nums:
                f.unlink()  # already completed - remove duplicate
            else:
                f.rename(open_dir / f.name)
                count += 1
        except FileNotFoundError:
            pass  # already moved by drain coordinator
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
        body = resp.json()
        if isinstance(body, dict) and "tasks" in body:
            task_list = cast("list[dict[str, Any]]", body["tasks"])
        elif isinstance(body, list):
            task_list = cast("list[dict[str, Any]]", body)
        else:
            task_list: list[dict[str, Any]] = []
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
            "open_tasks": sum(1 for _ in (workdir / ".sdd" / "backlog" / "open").glob(_YAML_GLOB))
            if (workdir / ".sdd" / "backlog" / "open").exists()
            else 0,
            "claimed_tasks": sum(1 for _ in (workdir / ".sdd" / "backlog" / "claimed").glob(_YAML_GLOB))
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
    console.print("\n[yellow]Ctrl+C received - saving state...[/yellow]")
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


def _get_phase_icon(status: str, icons: object) -> str:
    """Return the icon for a drain phase status."""
    running = "..." if sys.platform == "win32" else "\u23f3"
    done = getattr(icons, "check", "+")
    failed = getattr(icons, "cross", "x")
    return {"running": running, "done": done}.get(status, failed)


def _format_agent_status_line(agents: object, icons: object) -> str | None:
    """Format a one-line agent status summary for the wait phase."""
    if not isinstance(agents, list) or not agents:
        return None
    running = "..." if sys.platform == "win32" else "\u23f3"
    done = getattr(icons, "check", "+")
    failed = getattr(icons, "cross", "x")
    commit = "[w]" if sys.platform == "win32" else "\U0001f4dd"
    sym_map = {"running": running, "exited": done, "killed": failed, "committing": commit}
    parts: list[str] = []
    for a_obj in cast("list[object]", agents):
        sid = getattr(a_obj, "session_id", "?")
        st = getattr(a_obj, "status", "?")
        parts.append(f"{sym_map.get(st, '?')} {sid}")
    return " | ".join(parts) if parts else None


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

    _last_phase: dict[str, int] = {"number": 0}

    def on_update(phase: object, agents: object) -> None:
        _icons = get_icons()
        number = getattr(phase, "number", 0)
        name = getattr(phase, "name", "")
        detail = getattr(phase, "detail", "")
        status = getattr(phase, "status", "")

        if number != _last_phase["number"]:
            if _last_phase["number"] > 0:
                print()
            icon = _get_phase_icon(status, _icons)
            print(f"  {icon} Phase {number}/6: {name}", flush=True)
            _last_phase["number"] = number

        if detail:
            print(f"\r    {detail}    ", end="", flush=True)

        if name == "wait":
            line = _format_agent_status_line(agents, _icons)
            if line:
                print(f"\r    {line}    ", end="", flush=True)

    report = asyncio.run(coordinator.run(callback=on_update))
    print()  # final newline

    # Print summary.
    merged_count = sum(1 for m in report.merges if m.action == "merged")
    console.print("\n[bold]Drain complete:[/bold]")
    console.print(f"  Tasks: {report.tasks_done} done, {report.tasks_partial} partial")
    console.print(f"  Merged: {merged_count} branches")
    console.print(f"  Cleanup: {report.worktrees_removed} worktrees, {report.branches_deleted} branches")
    console.print(f"  Duration: {report.total_duration_s:.0f}s")


def _kill_agent_pid(pid: int, label: str, killed: set[int]) -> None:
    """SIGKILL an agent process, verify death, and track the PID."""
    if pid in killed or not is_alive(pid):
        killed.add(pid)
        return
    dead = sigkill_pid(pid)
    killed.add(pid)
    if dead:
        console.print(f"[red]Killed agent {label} (PID {pid}).[/red]")
    else:
        console.print(f"[yellow]Agent {label} (PID {pid}) resisted SIGKILL.[/yellow]")


def _kill_named_pid(pid: int, label: str, killed: set[int]) -> None:
    """SIGKILL a non-agent process and track the PID."""
    if pid in killed or not is_alive(pid):
        killed.add(pid)
        return
    dead = sigkill_pid(pid)
    killed.add(pid)
    if dead:
        console.print(f"[red]Killed {label} (PID {pid}).[/red]")
    else:
        console.print(f"[yellow]{label} (PID {pid}) resisted SIGKILL - may need manual cleanup.[/yellow]")


def _kill_pid_file(path: str, label: str, killed: set[int]) -> None:
    """Kill a PID-file-managed process and include it in the killed set."""
    pid = read_pid(path)
    if pid is not None and is_alive(pid):
        killed.add(pid)
    kill_pid_hard(path, label)


def _collect_pids_from_agents_json(killed: set[int]) -> None:
    """Source A: kill agent PIDs from agents.json."""
    agents_json = Path(_AGENTS_JSON_PATH)
    if not agents_json.exists():
        return
    try:
        agent_data = json.loads(agents_json.read_text())
        for agent in agent_data.get("agents", []):
            pid = agent.get("pid")
            if pid and is_alive(pid):
                _kill_agent_pid(pid, agent.get("id", "?"), killed)
    except (OSError, ValueError):
        pass


def _extract_pids_from_meta(meta: dict[str, Any]) -> list[int]:
    """Extract valid PIDs from a metadata dict."""
    pids: list[int] = []
    for key in ("worker_pid", "child_pid", "pid"):
        raw = meta.get(key)
        if raw is None:
            continue
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid:
            pids.append(pid)
    return pids


def _collect_pids_from_metadata(killed: set[int]) -> None:
    """Source B: kill worker + child PIDs from .sdd/runtime/pids/*.json."""
    pids_dir = Path(".sdd/runtime/pids")
    if not pids_dir.is_dir():
        return
    for pid_file in pids_dir.glob("*.json"):
        try:
            meta = json.loads(pid_file.read_text())
        except (OSError, ValueError):
            continue
        label = meta.get("session", pid_file.stem)
        for pid in _extract_pids_from_meta(meta):
            if pid not in killed and is_alive(pid):
                _kill_agent_pid(pid, label, killed)
        pid_file.unlink(missing_ok=True)


def _collect_pids_from_supervisor_state(killed: set[int]) -> None:
    """Source C: kill the server from supervisor state when pid files are missing."""
    snapshot = read_supervisor_state(Path(".sdd"))
    if snapshot is None or snapshot.current_pid <= 0:
        return
    if snapshot.current_pid not in killed and is_alive(snapshot.current_pid):
        _kill_named_pid(snapshot.current_pid, _LABEL_TASK_SERVER, killed)


@dataclass(frozen=True)
class _ProcessSnapshot:
    """Minimal process metadata used for hard-stop fallback matching."""

    pid: int
    ppid: int
    pgid: int
    command: str


def _list_process_snapshots() -> list[_ProcessSnapshot]:
    """Return a best-effort snapshot of all local processes."""
    if sys.platform == "win32":
        return _list_process_snapshots_windows()
    return _list_process_snapshots_unix()


def _list_process_snapshots_windows() -> list[_ProcessSnapshot]:
    """Windows: use WMIC or PowerShell to list processes."""
    snapshots: list[_ProcessSnapshot] = []
    try:
        # Use PowerShell for better reliability
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process | Select-Object Id, @{N='ParentId';E={(Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.Id)\").ParentProcessId}}, Path | ConvertTo-Csv -NoTypeInformation",  # noqa: E501
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return []
        lines = result.stdout.strip().splitlines()
        if len(lines) < 2:
            return []
        # Skip header: "Id","ParentId","Path"
        for line in lines[1:]:
            # Parse CSV: "1234","5678","C:\path\to\exe"
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 3:
                try:
                    pid = int(parts[0])
                    ppid = int(parts[1]) if parts[1] else 0
                    command = parts[2] if parts[2] else ""
                    snapshots.append(_ProcessSnapshot(pid=pid, ppid=ppid, pgid=0, command=command))
                except (ValueError, IndexError):
                    continue
    except (OSError, subprocess.SubprocessError):
        pass
    return snapshots


def _list_process_snapshots_unix() -> list[_ProcessSnapshot]:
    """Unix: use ps to list processes."""
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "pid=,ppid=,pgid=,command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    snapshots: list[_ProcessSnapshot] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=3)
        if len(parts) != 4:
            continue
        try:
            snapshots.append(
                _ProcessSnapshot(
                    pid=int(parts[0]),
                    ppid=int(parts[1]),
                    pgid=int(parts[2]),
                    command=parts[3],
                )
            )
        except ValueError:
            continue
    return snapshots


def _collect_repo_processes(killed: set[int]) -> None:
    """Source D: scan repo-owned runtime processes when PID files are gone."""
    workdir = Path.cwd()
    my_pid = os.getpid()
    heartbeat_prefix = str(workdir / ".sdd" / "runtime" / "heartbeats")
    worktree_prefix = str(workdir / ".sdd" / "worktrees")

    for snapshot in _list_process_snapshots():
        if snapshot.pid in killed or snapshot.pid == my_pid:
            continue

        command = snapshot.command
        if heartbeat_prefix in command or worktree_prefix in command:
            _kill_agent_pid(snapshot.pid, f"orphan-{snapshot.pid}", killed)
            continue

        if "bernstein.core.bootstrap" in command and "--watchdog" in command:
            if process_cwd(snapshot.pid) == workdir:
                _kill_named_pid(snapshot.pid, "Watchdog", killed)
            continue

        if "bernstein.core.orchestrator" in command:
            if process_cwd(snapshot.pid) == workdir:
                _kill_named_pid(snapshot.pid, "Spawner", killed)
            continue

        if "uvicorn bernstein.core.server:app" in command and process_cwd(snapshot.pid) == workdir:
            _kill_named_pid(snapshot.pid, _LABEL_TASK_SERVER, killed)


def _kill_port_holder(port: int, killed: set[int]) -> None:
    """Kill whatever process is holding a port (last resort)."""
    try:
        pids = _find_port_pids_win32(port) if sys.platform == "win32" else _find_port_pids_unix(port)
        for pid in pids:
            if pid not in killed:
                _kill_named_pid(pid, f"Port {port} holder", killed)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass


def _find_port_pids_win32(port: int) -> list[int]:
    """Find PIDs holding a port on Windows via netstat."""
    result = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        if f":{port}" not in line or "LISTENING" not in line:
            continue
        parts = line.split()
        if parts:
            try:
                pids.append(int(parts[-1]))
            except ValueError:
                continue
    return pids


def _find_port_pids_unix(port: int) -> list[int]:
    """Find PIDs holding a port on Unix via lsof."""
    result = subprocess.run(
        ["lsof", "-ti", f":{port}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    pids: list[int] = []
    for line in result.stdout.strip().splitlines():
        pids.append(int(line.strip()))
    return pids


def _cleanup_runtime_artifacts() -> None:
    """Remove stale PID files and agents.json so the next stop is clean."""
    for path in (
        Path(_AGENTS_JSON_PATH),
        Path(".sdd/runtime/draining"),
        Path(".sdd/runtime/supervisor_state.json"),
        Path(".sdd/runtime/watchdog_state.json"),
        Path(".sdd/runtime/bernstein.pid"),
        Path(SDD_PID_SERVER),
        Path(SDD_PID_SPAWNER),
        Path(SDD_PID_WATCHDOG),
    ):
        path.unlink(missing_ok=True)
    signals_dir = Path(".sdd/runtime/signals")
    if signals_dir.is_dir():
        shutil.rmtree(signals_dir, ignore_errors=True)
    pids_dir = Path(".sdd/runtime/pids")
    if pids_dir.is_dir():
        for f in pids_dir.glob("*.json"):
            f.unlink(missing_ok=True)


def hard_stop() -> None:
    """Hard stop: SIGKILL everything, best-effort save, return tickets."""
    # 1. Best-effort session save while server is still alive
    try:
        save_session_on_stop(Path.cwd())
        console.print("[dim]Session state saved (best-effort).[/dim]")
    except OSError:
        console.print("[yellow]Could not save session state.[/yellow]")

    # 2. Kill infrastructure: watchdog, spawner, server
    killed_pids: set[int] = set()
    _kill_pid_file(SDD_PID_WATCHDOG, "Watchdog", killed_pids)
    _kill_pid_file(SDD_PID_SPAWNER, "Spawner", killed_pids)
    _kill_pid_file(SDD_PID_SERVER, _LABEL_TASK_SERVER, killed_pids)
    _collect_pids_from_supervisor_state(killed_pids)
    _kill_port_holder(8052, killed_pids)  # last resort: kill whatever holds the port

    # 3. Kill all spawned agents and repo-owned leftovers
    _collect_pids_from_agents_json(killed_pids)
    _collect_pids_from_metadata(killed_pids)
    _collect_repo_processes(killed_pids)

    # 4. Verification sweep - re-scan and retry anything still alive
    time.sleep(0.1)
    survivors: list[int] = [p for p in killed_pids if is_alive(p)]
    if survivors:
        console.print(f"[yellow]Retrying {len(survivors)} survivor(s)...[/yellow]")
        for pid in survivors:
            _kill_named_pid(pid, f"survivor-{pid}", killed_pids)
    _collect_repo_processes(killed_pids)

    # 5. Clean up stale runtime artifacts
    _cleanup_runtime_artifacts()

    # 6. Return claimed tickets to open
    try:
        moved = return_claimed_to_open()
        if moved:
            console.print(f"[dim]Returned {moved} claimed ticket(s) to open.[/dim]")
    except OSError:
        console.print("[yellow]Could not return claimed tickets.[/yellow]")

    # Remove Bernstein from .claude/mcp.json so stale references are cleaned up
    _unregister_mcp_discovery(Path.cwd())

    total = len(killed_pids)
    if total:
        console.print(f"\n[red]Bernstein stopped (hard) - killed {total} process(es).[/red]")
    else:
        console.print("\n[red]Bernstein stopped (hard) - no processes were running.[/red]")


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
        console.print("[bold red]Hard stop - killing everything immediately...[/bold red]\n")
        hard_stop()
    else:
        console.print("[bold]Soft stop - giving agents time to save...[/bold]\n")
        soft_stop(timeout)
        _unregister_mcp_discovery(Path.cwd())
