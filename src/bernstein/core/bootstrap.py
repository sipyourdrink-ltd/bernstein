"""Bootstrap: parse seed -> init .sdd -> start server -> plan -> orchestrate.

This is the single entry point for the "drop bernstein.yaml, run one command"
UX. Called by `bernstein run` and by the bare `bernstein` invocation when a
seed file is detected.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

from bernstein.core.router import TierAwareRouter, load_providers_from_yaml
from bernstein.core.seed import NotifyConfig, SeedConfig, parse_seed, seed_to_initial_task

logger = logging.getLogger(__name__)

# Dirs that make up the .sdd workspace.
SDD_DIRS = (
    ".sdd",
    ".sdd/backlog",
    ".sdd/backlog/open",
    ".sdd/backlog/done",
    ".sdd/agents",
    ".sdd/runtime",
    ".sdd/docs",
    ".sdd/decisions",
)

_SERVER_READY_TIMEOUT_S = 10.0
_SERVER_POLL_INTERVAL_S = 0.25

console = Console()


def _send_webhook(config: NotifyConfig, payload: dict[str, Any]) -> None:
    """POST a JSON payload to the configured webhook URL.

    Errors are logged but never propagate — this must never crash the run.

    Args:
        config: Notification configuration containing the webhook URL.
        payload: JSON-serialisable dict to POST.
    """
    if not config.webhook_url:
        return
    try:
        resp = httpx.post(config.webhook_url, json=payload, timeout=10.0)
        logger.info("Webhook POST %s -> %d", config.webhook_url, resp.status_code)
    except Exception:
        logger.exception("Webhook POST to %s failed (ignored)", config.webhook_url)


@dataclass
class BootstrapResult:
    """Outcome of a full bootstrap run.

    Attributes:
        seed: The parsed seed config.
        server_pid: PID of the launched task server.
        spawner_pid: PID of the launched spawner process.
        manager_task_id: ID of the initial manager task.
    """

    seed: SeedConfig
    server_pid: int
    spawner_pid: int
    manager_task_id: str


def _clean_stale_runtime(workdir: Path) -> None:
    """Remove stale PID files and old logs from .sdd/runtime/.

    Called before starting a new run to prevent "server already running"
    errors from crashed previous runs.

    Args:
        workdir: Project root directory.
    """
    runtime_dir = workdir / ".sdd" / "runtime"
    if not runtime_dir.exists():
        return

    # Remove stale PID files (check if process is actually alive)
    for pid_file in runtime_dir.glob("*.pid"):
        pid = _read_pid(pid_file)
        if pid is None or not _is_alive(pid):
            pid_file.unlink(missing_ok=True)

    # Clear old log files (they'll be recreated)
    for log_file in runtime_dir.glob("*.log"):
        log_file.unlink(missing_ok=True)

    # Clear stale tasks.jsonl to start fresh
    jsonl = runtime_dir / "tasks.jsonl"
    if jsonl.exists():
        jsonl.unlink(missing_ok=True)


def _ensure_sdd(workdir: Path) -> bool:
    """Create .sdd/ workspace structure if it does not exist.

    Args:
        workdir: Project root directory.

    Returns:
        True if the workspace was newly created, False if it already existed.
    """
    created = False
    for d in SDD_DIRS:
        p = workdir / d
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created = True

    # Write default config if missing
    config_path = workdir / ".sdd" / "config.yaml"
    if not config_path.exists():
        config_path.write_text(
            "# Bernstein workspace config\n"
            "server_port: 8052\n"
            "max_workers: 4\n"
            "default_model: opus\n"
            "default_effort: max\n"
        )

    # .gitignore for runtime dir
    gi_path = workdir / ".sdd" / "runtime" / ".gitignore"
    if not gi_path.exists():
        gi_path.write_text("*.pid\n*.log\ntasks.jsonl\n")

    return created


def _read_pid(pid_path: Path) -> int | None:
    """Read a PID from a file, returning None if missing or invalid."""
    if pid_path.exists():
        try:
            return int(pid_path.read_text().strip())
        except ValueError:
            return None
    return None


def _is_alive(pid: int) -> bool:
    """Check whether a process with the given PID is alive."""

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _discover_catalog(workdir: Path) -> None:
    """Run CatalogRegistry.discover() against the project workspace.

    Loads the agent catalog from cache (if fresh) or re-fetches from providers.
    On failure the error is logged and startup continues — catalog is optional.

    Args:
        workdir: Project root directory.
    """

    from bernstein.agents.catalog import CatalogRegistry

    cache_path = workdir / ".sdd" / "agents" / "catalog.json"
    try:
        registry = CatalogRegistry.default()
        registry._cache_path = cache_path
        registry.discover()
        console.print(
            f"[dim]Catalog: {len(registry._cached_roles)} role(s) ready[/dim]"
        )
    except Exception:
        logger.warning("Catalog auto-discovery failed (non-fatal)", exc_info=True)


def create_router(workdir: Path) -> TierAwareRouter | None:
    """Create a TierAwareRouter from providers.yaml if it exists.

    Args:
        workdir: Project root directory.

    Returns:
        Configured TierAwareRouter, or None if no providers.yaml found.
    """
    providers_yaml = workdir / ".sdd" / "config" / "providers.yaml"
    if not providers_yaml.exists():
        return None
    router = TierAwareRouter()
    load_providers_from_yaml(providers_yaml, router)
    return router


def _start_server(workdir: Path, port: int) -> int:
    """Launch the task server as a background process.

    Args:
        workdir: Project root (server runs from here).
        port: TCP port for the uvicorn server.

    Returns:
        PID of the server process.

    Raises:
        RuntimeError: If a server is already running on the PID file.
    """
    pid_path = workdir / ".sdd" / "runtime" / "server.pid"
    existing = _read_pid(pid_path)
    if existing is not None and _is_alive(existing):
        raise RuntimeError(
            f"Server already running (PID {existing}). "
            "Run `bernstein stop` first."
        )

    log_path = workdir / ".sdd" / "runtime" / "server.log"
    # Keep the log file open — child inherits the fd via fork().
    # Closing it prematurely can cause the child's stdout to break.
    log_fh = log_path.open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "bernstein.core.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(workdir),
    )
    # Safe to close in parent after Popen — child has its own fd copy
    log_fh.close()
    pid_path.write_text(str(proc.pid))
    return proc.pid


def _wait_for_server(port: int) -> bool:
    """Block until the server responds to /health, or timeout.

    Args:
        port: Server port.

    Returns:
        True if the server is reachable, False on timeout.
    """
    deadline = time.monotonic() + _SERVER_READY_TIMEOUT_S
    url = f"http://127.0.0.1:{port}/health"
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=2.0)
            if resp.status_code == 200:
                return True
        except httpx.ConnectError:
            pass
        time.sleep(_SERVER_POLL_INTERVAL_S)
    return False


def _inject_manager_task(
    seed: SeedConfig,
    workdir: Path,
    port: int,
) -> str:
    """Create the initial manager task on the running server.

    Args:
        seed: Parsed seed configuration.
        workdir: Project root for resolving context files.
        port: Server port.

    Returns:
        The task ID assigned by the server.

    Raises:
        RuntimeError: If the server rejects the task.
    """
    task = seed_to_initial_task(seed, workdir=workdir)

    payload: dict[str, Any] = {
        "title": "Plan and decompose goal into tasks",
        "role": "manager",
        "description": task.description,
        "priority": 1,
        "scope": "large",
        "complexity": "high",
    }

    resp = httpx.post(
        f"http://127.0.0.1:{port}/tasks",
        json=payload,
        timeout=5.0,
    )
    if resp.status_code != 201:
        raise RuntimeError(f"Failed to create manager task: {resp.status_code} {resp.text}")

    data: dict[str, Any] = resp.json()
    return str(data.get("id", "unknown"))


def _start_spawner(workdir: Path, port: int) -> int:
    """Launch the spawner process in the background.

    Args:
        workdir: Project root.
        port: Task server port.

    Returns:
        PID of the spawner process.
    """
    pid_path = workdir / ".sdd" / "runtime" / "spawner.pid"
    log_path = workdir / ".sdd" / "runtime" / "spawner.log"

    log_fh = log_path.open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bernstein.core.orchestrator",
            "--port",
            str(port),
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(workdir),
    )
    log_fh.close()
    pid_path.write_text(str(proc.pid))
    return proc.pid


def bootstrap_from_seed(
    seed_path: Path,
    workdir: Path,
    port: int = 8052,
) -> BootstrapResult:
    """Full bootstrap: parse seed -> init .sdd -> start server -> plan -> orchestrate.

    This is the main entry point for the "one command" UX. It:
    1. Parses the seed file (bernstein.yaml).
    2. Creates the .sdd/ workspace if needed.
    3. Starts the task server.
    4. Waits for the server to be ready.
    5. Injects the initial manager task with goal + constraints + context.
    6. Starts the spawner (which launches the manager agent).

    Args:
        seed_path: Path to the bernstein.yaml seed file.
        workdir: Project root directory.
        port: TCP port for the task server.

    Returns:
        BootstrapResult with PIDs and task ID.

    Raises:
        bernstein.core.seed.SeedError: If the seed file is invalid.
        RuntimeError: If the server fails to start or respond.
    """
    # 1. Parse seed
    seed = parse_seed(seed_path)
    console.print(f"[bold]Goal:[/bold] {seed.goal}")
    if seed.budget_usd is not None:
        console.print(f"[bold]Budget:[/bold] ${seed.budget_usd:.2f}")
    if seed.team != "auto":
        console.print(f"[bold]Team:[/bold] {', '.join(seed.team)}")
    else:
        console.print("[bold]Team:[/bold] auto (manager decides)")
    if seed.constraints:
        console.print(f"[bold]Constraints:[/bold] {len(seed.constraints)} rules")

    # 2. Init .sdd/
    created = _ensure_sdd(workdir)
    if created:
        console.print("[green]Created .sdd/ workspace[/green]")
    else:
        console.print("[dim]Using existing .sdd/ workspace[/dim]")

    # 2b. Clean stale runtime from previous runs
    _clean_stale_runtime(workdir)
    console.print("[dim]Cleaned stale runtime state[/dim]")

    # 2c. Auto-discover agents from catalogs (loads cache or refreshes providers)
    _discover_catalog(workdir)

    # Verify/write safety invariants
    from bernstein.evolution.invariants import verify_invariants, write_lockfile
    ok, violations = verify_invariants(workdir)
    if not ok:
        console.print(f"[bold red]SAFETY: {len(violations)} locked file(s) modified[/bold red]")
        for v in violations:
            console.print(f"  [red]{v}[/red]")
        console.print("[yellow]Re-establishing baseline...[/yellow]")
    write_lockfile(workdir)
    console.print("[dim]Safety invariants locked[/dim]")

    # 3. Start server
    console.print("[dim]Starting task server...[/dim]")
    server_pid = _start_server(workdir, port)
    console.print(f"[green]Task server started[/green] (PID {server_pid}, port {port})")

    # 4. Wait for server
    console.print("[dim]Waiting for server...[/dim]")
    if not _wait_for_server(port):
        console.print("[yellow]Server did not respond within timeout -- proceeding anyway[/yellow]")

    # 5. Sync backlog tasks into the server. If backlog has tasks, skip manager.
    from bernstein.core.sync import sync_backlog_to_server

    server_url = f"http://127.0.0.1:{port}"
    sync_result = sync_backlog_to_server(workdir, server_url=server_url)
    backlog_count = len(sync_result.created) + len(sync_result.skipped)

    manager_task_id = ""
    if backlog_count > 0:
        console.print(
            f"[green]Loaded {len(sync_result.created)} task(s) from backlog[/green]"
            f" ({len(sync_result.skipped)} already synced)"
        )
    else:
        # No backlog — use the manager agent to plan from scratch
        manager_task_id = _inject_manager_task(seed, workdir, port)
        console.print(f"[green]Manager task created:[/green] {manager_task_id}")

    # 6. Start spawner
    spawner_pid = _start_spawner(workdir, port)
    console.print(f"[green]Orchestrator started[/green] (PID {spawner_pid})")

    # 7. Start watchdog to auto-restart server/orchestrator if they die
    _start_watchdog(workdir, port)

    console.print(
        "\n[bold green]Bernstein is running.[/bold green] "
        "Use [bold]bernstein stop[/bold] to stop."
    )

    result = BootstrapResult(
        seed=seed,
        server_pid=server_pid,
        spawner_pid=spawner_pid,
        manager_task_id=manager_task_id,
    )

    if seed.notify is not None and seed.notify.on_complete:
        _send_webhook(
            seed.notify,
            {
                "event": "complete",
                "goal": seed.goal,
                "manager_task_id": manager_task_id,
                "server_pid": server_pid,
                "spawner_pid": spawner_pid,
            },
        )

    return result


def _start_watchdog(workdir: Path, port: int) -> int:
    """Launch the watchdog as a background process.

    Args:
        workdir: Project root.
        port: Task server port.

    Returns:
        PID of the watchdog process.
    """
    pid_path = workdir / ".sdd" / "runtime" / "watchdog.pid"
    log_path = workdir / ".sdd" / "runtime" / "watchdog.log"

    log_fh = log_path.open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bernstein.core.bootstrap",
            "--watchdog",
            "--port",
            str(port),
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(workdir),
    )
    log_fh.close()
    pid_path.write_text(str(proc.pid))
    return proc.pid


def run_watchdog(workdir: Path, port: int, poll_s: float = 5.0) -> None:
    """Monitor the server and orchestrator, restarting them if they die.

    This blocks forever and should be run as a background daemon.

    Args:
        workdir: Project root directory.
        port: Task server port.
        poll_s: Seconds between health checks.
    """
    server_pid_path = workdir / ".sdd" / "runtime" / "server.pid"
    spawner_pid_path = workdir / ".sdd" / "runtime" / "spawner.pid"
    max_restarts = 5
    server_restarts = 0
    spawner_restarts = 0

    while True:
        time.sleep(poll_s)

        # Check server
        server_pid = _read_pid(server_pid_path)
        if server_pid is None or not _is_alive(server_pid):
            if server_restarts >= max_restarts:
                logger.error("Server exceeded max restarts (%d), giving up", max_restarts)
                continue
            logger.warning("Server (PID %s) is dead, restarting...", server_pid)
            try:
                new_pid = _start_server(workdir, port)
                logger.info("Server restarted (PID %d)", new_pid)
                server_restarts += 1
                _wait_for_server(port)
            except Exception:
                logger.exception("Failed to restart server")

        # Check orchestrator/spawner
        spawner_pid = _read_pid(spawner_pid_path)
        if spawner_pid is None or not _is_alive(spawner_pid):
            if spawner_restarts >= max_restarts:
                logger.error("Orchestrator exceeded max restarts (%d), giving up", max_restarts)
                continue
            # Only restart orchestrator if server is alive
            cur_server_pid = _read_pid(server_pid_path)
            if cur_server_pid is not None and _is_alive(cur_server_pid):
                logger.warning("Orchestrator (PID %s) is dead, restarting...", spawner_pid)
                try:
                    new_pid = _start_spawner(workdir, port)
                    logger.info("Orchestrator restarted (PID %d)", new_pid)
                    spawner_restarts += 1
                except Exception:
                    logger.exception("Failed to restart orchestrator")


def bootstrap_from_goal(
    goal: str,
    workdir: Path,
    port: int = 8052,
    cli: str = "claude",
) -> BootstrapResult:
    """Bootstrap from an inline goal string (no YAML file needed).

    Creates a minimal SeedConfig from the goal and delegates to the
    standard bootstrap flow.

    Args:
        goal: Plain-text project goal.
        workdir: Project root directory.
        port: TCP port for the task server.
        cli: CLI backend to use.

    Returns:
        BootstrapResult with PIDs and task ID.
    """
    seed = SeedConfig(goal=goal, cli=cli)  # type: ignore[arg-type]

    # Reuse the same flow but skip file parsing
    created = _ensure_sdd(workdir)
    if created:
        console.print("[green]Created .sdd/ workspace[/green]")
    else:
        console.print("[dim]Using existing .sdd/ workspace[/dim]")

    # Clean stale runtime from previous runs
    _clean_stale_runtime(workdir)
    console.print("[dim]Cleaned stale runtime state[/dim]")

    # Auto-discover agents from catalogs
    _discover_catalog(workdir)

    # Verify/write safety invariants
    from bernstein.evolution.invariants import verify_invariants, write_lockfile
    ok, violations = verify_invariants(workdir)
    if not ok:
        console.print(f"[bold red]SAFETY: {len(violations)} locked file(s) modified[/bold red]")
        for v in violations:
            console.print(f"  [red]{v}[/red]")
        console.print("[yellow]Re-establishing baseline...[/yellow]")
    write_lockfile(workdir)
    console.print("[dim]Safety invariants locked[/dim]")

    console.print(f"[bold]Goal:[/bold] {goal}")

    console.print("[dim]Starting task server...[/dim]")
    server_pid = _start_server(workdir, port)
    console.print(f"[green]Task server started[/green] (PID {server_pid}, port {port})")

    console.print("[dim]Waiting for server...[/dim]")
    if not _wait_for_server(port):
        console.print("[yellow]Server did not respond within timeout -- proceeding anyway[/yellow]")

    # Sync backlog first; only use manager if backlog is empty
    from bernstein.core.sync import sync_backlog_to_server

    server_url = f"http://127.0.0.1:{port}"
    sync_result = sync_backlog_to_server(workdir, server_url=server_url)
    backlog_count = len(sync_result.created) + len(sync_result.skipped)

    manager_task_id = ""
    if backlog_count > 0:
        console.print(
            f"[green]Loaded {len(sync_result.created)} task(s) from backlog[/green]"
            f" ({len(sync_result.skipped)} already synced)"
        )
    else:
        manager_task_id = _inject_manager_task(seed, workdir, port)
        console.print(f"[green]Manager task created:[/green] {manager_task_id}")

    spawner_pid = _start_spawner(workdir, port)
    console.print(f"[green]Orchestrator started[/green] (PID {spawner_pid})")

    _start_watchdog(workdir, port)

    console.print(
        "\n[bold green]Bernstein is running.[/bold green] "
        "Use [bold]bernstein stop[/bold] to stop."
    )

    return BootstrapResult(
        seed=seed,
        server_pid=server_pid,
        spawner_pid=spawner_pid,
        manager_task_id=manager_task_id,
    )


if __name__ == "__main__":
    import argparse as _argparse

    _parser = _argparse.ArgumentParser()
    _parser.add_argument("--watchdog", action="store_true")
    _parser.add_argument("--port", type=int, default=8052)
    _args = _parser.parse_args()

    if _args.watchdog:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        run_watchdog(Path.cwd(), _args.port)
