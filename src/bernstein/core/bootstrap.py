"""Bootstrap: parse seed -> init .sdd -> start server -> plan -> orchestrate.

This is the single entry point for the "drop bernstein.yaml, run one command"
UX. Called by `bernstein run` and by the bare `bernstein` invocation when a
seed file is detected.
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
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

# Binary install hints for each supported CLI adapter.
_CLI_INSTALL_HINT: dict[str, str] = {
    "claude": "https://claude.ai/code",
    "codex": "npm install -g @openai/codex",
    "gemini": "npm install -g @google/gemini-cli",
    "qwen": "npm install -g qwen-code",
}

# Primary API key env var(s) per adapter.
_CLI_API_KEY_ENV: dict[str, str] = {
    "claude": "ANTHROPIC_API_KEY",
    "codex": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
}

# Qwen supports multiple providers; any one of these is sufficient.
_QWEN_API_KEY_VARS: tuple[str, ...] = (
    "OPENROUTER_API_KEY_PAID",
    "OPENROUTER_API_KEY_FREE",
    "OPENAI_API_KEY",
    "TOGETHERAI_USER_KEY",
    "OXen_API_KEY",
    "G4F_API_KEY",
)


def _check_binary(cli: str) -> None:
    """Exit with an actionable message if the CLI binary is not in PATH.

    Args:
        cli: Adapter name (e.g. "claude", "codex", "gemini", "qwen").

    Raises:
        SystemExit: If the binary is not found.
    """
    binary = cli  # binary name matches adapter name for all supported adapters
    if shutil.which(binary) is None:
        hint = _CLI_INSTALL_HINT.get(cli, f"See documentation for {binary!r}")
        console.print(f"[bold red]Error:[/bold red] {binary!r} not found in PATH.")
        console.print(f"Install: {hint}")
        raise SystemExit(1)


def _check_api_key(cli: str) -> None:
    """Exit with an actionable message if the required API key is not set.

    Args:
        cli: Adapter name (e.g. "claude", "codex", "gemini", "qwen").

    Raises:
        SystemExit: If the required API key env var is missing.
    """
    if cli == "qwen":
        if not any(os.environ.get(v) for v in _QWEN_API_KEY_VARS):
            console.print(
                "[bold red]Error:[/bold red] No API key configured for qwen."
            )
            console.print(
                "Set one of: " + ", ".join(_QWEN_API_KEY_VARS)
            )
            raise SystemExit(1)
    else:
        env_var = _CLI_API_KEY_ENV.get(cli)
        if env_var and not os.environ.get(env_var):
            console.print(
                f"[bold red]Error:[/bold red] {env_var} is not set."
            )
            console.print(f"Export it: export {env_var}=<your-api-key>")
            raise SystemExit(1)


def _check_port_free(port: int) -> None:
    """Exit with an actionable message if the port is already in use.

    Args:
        port: TCP port to check.

    Raises:
        SystemExit: If the port is occupied.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            console.print(
                f"[bold red]Error:[/bold red] Port {port} is already in use."
            )
            console.print(
                "Run [bold]bernstein stop[/bold] to free it, "
                f"or pass [bold]--port <n>[/bold] to use a different port."
            )
            raise SystemExit(1)


def preflight_checks(cli: str, port: int) -> None:
    """Run pre-flight checks before starting the server.

    Verifies that:
    1. The CLI binary is installed and in PATH.
    2. The required API key env var is present.
    3. The server port is not already occupied.

    Args:
        cli: Adapter name (e.g. "claude", "codex", "gemini", "qwen").
        port: TCP port the server will bind to.

    Raises:
        SystemExit: On any pre-flight failure, with an actionable message.
    """
    _check_binary(cli)
    _check_api_key(cli)
    _check_port_free(port)


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


def _build_codebase_index(workdir: Path) -> None:
    """Build or incrementally update the codebase search index.

    Uses SQLite FTS5 for BM25-ranked full-text search so agents can find
    relevant code without trial-and-error grepping.  On failure the error
    is logged and startup continues — the index is optional.

    Args:
        workdir: Project root directory.
    """
    from bernstein.core.rag import build_or_update_index

    try:
        indexer = build_or_update_index(workdir)
        console.print(
            f"[dim]Codebase index: {indexer.file_count()} file(s) indexed[/dim]"
        )
    except Exception:
        logger.warning("Codebase index build failed (non-fatal)", exc_info=True)


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


def _start_spawner(workdir: Path, port: int, cells: int = 1) -> int:
    """Launch the spawner process in the background.

    Args:
        workdir: Project root.
        port: Task server port.
        cells: Number of parallel orchestration cells (1 = single-cell).

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
            "--cells",
            str(cells),
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
    cells: int | None = None,
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
        cells: Number of parallel cells. If None, reads from seed config.

    Returns:
        BootstrapResult with PIDs and task ID.

    Raises:
        bernstein.core.seed.SeedError: If the seed file is invalid.
        RuntimeError: If the server fails to start or respond.
    """
    from rich.status import Status

    # 1. Parse seed
    with Status("[bold]Parsing seed file...[/bold]", console=console):
        seed = parse_seed(seed_path)
        # Pre-flight: verify binary, API key, and port before touching anything.
        preflight_checks(seed.cli, port)
    effective_cells = cells if cells is not None else seed.cells
    console.print(f"[green]→[/green] Parsed seed: [bold]{seed.goal[:80]}[/bold]")
    if seed.budget_usd is not None:
        console.print(f"  [bold]Budget:[/bold] ${seed.budget_usd:.2f}")
    if seed.team != "auto":
        console.print(f"  [bold]Team:[/bold] {', '.join(seed.team)}")
    if seed.constraints:
        console.print(f"  [bold]Constraints:[/bold] {len(seed.constraints)} rules")

    # 2. Init workspace + clean stale state
    with Status("[bold]Initialising workspace...[/bold]", console=console):
        created = _ensure_sdd(workdir)
        _clean_stale_runtime(workdir)
        _discover_catalog(workdir)
        _build_codebase_index(workdir)
        from bernstein.evolution.invariants import verify_invariants, write_lockfile
        ok, violations = verify_invariants(workdir)
        if not ok:
            console.print(f"[bold red]SAFETY: {len(violations)} locked file(s) modified[/bold red]")
            for v in violations:
                console.print(f"  [red]{v}[/red]")
        write_lockfile(workdir)
    if created:
        console.print("[green]→[/green] Created .sdd/ workspace")
    else:
        console.print("[green]→[/green] Workspace ready")

    # 3. Start server
    with Status(f"[bold]Starting task server on :{port}...[/bold]", console=console):
        server_pid = _start_server(workdir, port)
        if not _wait_for_server(port):
            console.print(
                f"[bold red]Error:[/bold red] Task server on port {port} did not respond within "
                f"{_SERVER_READY_TIMEOUT_S:.0f}s.\n"
                f"  [yellow]Reason:[/yellow] Server process may have crashed\n"
                f"  [green]Fix:[/green] Check [dim].sdd/runtime/server.log[/dim] for details"
            )
            raise SystemExit(1)
    console.print(f"[green]→[/green] Task server ready (PID {server_pid}, :{port})")

    # 4. Sync backlog / create manager task
    from bernstein.core.sync import sync_backlog_to_server

    server_url = f"http://127.0.0.1:{port}"
    with Status("[bold]Loading tasks...[/bold]", console=console):
        sync_result = sync_backlog_to_server(workdir, server_url=server_url)
    backlog_count = len(sync_result.created) + len(sync_result.skipped)

    manager_task_id = ""
    if backlog_count > 0:
        console.print(
            f"[green]→[/green] Planning tasks ({backlog_count} found in backlog"
            + (f", {len(sync_result.skipped)} already synced" if sync_result.skipped else "")
            + ")"
        )
    else:
        # No backlog — use the manager agent to plan from scratch
        with Status("[bold]Creating planning task...[/bold]", console=console):
            manager_task_id = _inject_manager_task(seed, workdir, port)
        console.print("[green]→[/green] Planning tasks (manager agent will decompose goal)")

    # 5. Start spawner + watchdog
    cell_label = f"{effective_cells} cells" if effective_cells > 1 else "single cell"
    with Status(f"[bold]Spawning agents ({cell_label})...[/bold]", console=console):
        spawner_pid = _start_spawner(workdir, port, cells=effective_cells)
        _start_watchdog(workdir, port)
    console.print(f"[green]→[/green] Spawning agents (PID {spawner_pid})")

    console.print("\n[bold green]Dashboard ready.[/bold green] Use [bold]bernstein stop[/bold] to stop.")

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
    cells: int = 1,
) -> BootstrapResult:
    """Bootstrap from an inline goal string (no YAML file needed).

    Creates a minimal SeedConfig from the goal and delegates to the
    standard bootstrap flow.

    Args:
        goal: Plain-text project goal.
        workdir: Project root directory.
        port: TCP port for the task server.
        cli: CLI backend to use.
        cells: Number of parallel orchestration cells.

    Returns:
        BootstrapResult with PIDs and task ID.
    """
    from rich.status import Status

    seed = SeedConfig(goal=goal, cli=cli)  # type: ignore[arg-type]

    console.print(f"[green]→[/green] Goal: [bold]{goal[:80]}[/bold]")

    # Pre-flight: verify binary, API key, and port before touching anything.
    with Status("[bold]Running pre-flight checks...[/bold]", console=console):
        preflight_checks(cli, port)

    # Initialise workspace
    with Status("[bold]Initialising workspace...[/bold]", console=console):
        created = _ensure_sdd(workdir)
        _clean_stale_runtime(workdir)
        _discover_catalog(workdir)
        _build_codebase_index(workdir)
        from bernstein.evolution.invariants import verify_invariants, write_lockfile
        ok, violations = verify_invariants(workdir)
        if not ok:
            console.print(f"[bold red]SAFETY: {len(violations)} locked file(s) modified[/bold red]")
            for v in violations:
                console.print(f"  [red]{v}[/red]")
        write_lockfile(workdir)
    if created:
        console.print("[green]→[/green] Created .sdd/ workspace")
    else:
        console.print("[green]→[/green] Workspace ready")

    with Status(f"[bold]Starting task server on :{port}...[/bold]", console=console):
        server_pid = _start_server(workdir, port)
        if not _wait_for_server(port):
            console.print(
                f"[bold red]Error:[/bold red] Task server on port {port} did not respond within "
                f"{_SERVER_READY_TIMEOUT_S:.0f}s.\n"
                f"  [yellow]Reason:[/yellow] Server process may have crashed\n"
                f"  [green]Fix:[/green] Check [dim].sdd/runtime/server.log[/dim] for details"
            )
            raise SystemExit(1)
    console.print(f"[green]→[/green] Task server ready (PID {server_pid}, :{port})")

    # Sync backlog first; only use manager if backlog is empty
    from bernstein.core.sync import sync_backlog_to_server

    server_url = f"http://127.0.0.1:{port}"
    with Status("[bold]Loading tasks...[/bold]", console=console):
        sync_result = sync_backlog_to_server(workdir, server_url=server_url)
    backlog_count = len(sync_result.created) + len(sync_result.skipped)

    manager_task_id = ""
    if backlog_count > 0:
        console.print(
            f"[green]→[/green] Planning tasks ({backlog_count} found in backlog"
            + (f", {len(sync_result.skipped)} already synced" if sync_result.skipped else "")
            + ")"
        )
    else:
        with Status("[bold]Creating planning task...[/bold]", console=console):
            manager_task_id = _inject_manager_task(seed, workdir, port)
        console.print("[green]→[/green] Planning tasks (manager agent will decompose goal)")

    cell_label = f"{cells} cells" if cells > 1 else "single cell"
    with Status(f"[bold]Spawning agents ({cell_label})...[/bold]", console=console):
        spawner_pid = _start_spawner(workdir, port, cells=cells)
        _start_watchdog(workdir, port)
    console.print(f"[green]→[/green] Spawning agents (PID {spawner_pid})")

    console.print("\n[bold green]Dashboard ready.[/bold green] Use [bold]bernstein stop[/bold] to stop.")

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
