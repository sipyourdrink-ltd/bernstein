"""Bootstrap orchestration: coordinate startup, task planning, and agent spawning.

This module orchestrates the full bootstrap flow: parsing seed, starting server,
and spawning agents. It imports lower-level startup logic from server_launch.py
and preflight checks from preflight.py.

Entry points:
- bootstrap_from_seed() — read bernstein.yaml and launch
- bootstrap_from_goal() — quick launch from inline goal string
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.status import Status

# Import from sub-modules (facade re-exports)
from bernstein.core.preflight import (
    _claude_has_oauth_session,
    _codex_has_auth,
    gemini_has_auth,
    preflight_checks,
)
from bernstein.core.seed import NotifyConfig, SeedConfig, parse_seed
from bernstein.core.server_launch import (
    BootstrapResult,
    _build_codebase_index,
    _clean_stale_runtime,
    _discover_catalog,
    _inject_manager_task,
    _is_alive,
    _read_pid,
    _resolve_auth_token,
    _resolve_bind_host,
    _resolve_server_url,
    _start_server,
    _start_spawner,
    _wait_for_server,
    auto_write_bernstein_yaml,
    create_router,
    ensure_sdd,
)

logger = logging.getLogger(__name__)
console = Console()

# Constants — re-export for backward compat
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

__all__ = [
    # This module
    "SDD_DIRS",
    # From server_launch (re-exported for backward compat)
    "BootstrapResult",
    # From preflight
    "_claude_has_oauth_session",
    "_codex_has_auth",
    "auto_write_bernstein_yaml",
    "bootstrap_from_goal",
    "bootstrap_from_seed",
    "console",
    "create_router",
    "ensure_sdd",
    "gemini_has_auth",
    "preflight_checks",
    "run_watchdog",
]


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


def bootstrap_from_seed(
    seed_path: Path,
    workdir: Path,
    port: int = 8052,
    cells: int | None = None,
    remote: bool = False,
    force_fresh: bool = False,
    evolve_mode: bool = False,
    cli: str | None = None,
    model: str | None = None,
) -> BootstrapResult:
    """Full bootstrap: parse seed -> init .sdd -> start server -> plan -> orchestrate.

    This is the main entry point for the "one command" UX. It:
    1. Parses the seed file (bernstein.yaml).
    2. Creates the .sdd/ workspace if needed.
    3. Starts the task server.
    4. Waits for the server to be ready.
    5. Injects the initial manager task with goal + constraints + context
       (skipped when a valid session exists, unless force_fresh=True).
    6. Starts the spawner (which launches the manager agent).

    Args:
        seed_path: Path to the bernstein.yaml seed file.
        workdir: Project root directory.
        port: TCP port for the task server.
        cells: Number of parallel cells. If None, reads from seed config.
        remote: If True, bind to 0.0.0.0 for remote access.
        force_fresh: Ignore any saved session and start from scratch.
        evolve_mode: When True, start the server with ``--reload`` so that
            source changes by agents are picked up without killing agents.
        cli: Optional CLI override (e.g. "claude", "codex"). Overrides seed config.
        model: Optional model override (e.g. "opus", "sonnet"). Overrides seed config.

    Returns:
        BootstrapResult with PIDs and task ID.

    Raises:
        bernstein.core.seed.SeedError: If the seed file is invalid.
        RuntimeError: If the server fails to start or respond.
    """
    # Resolve cluster-aware settings
    bind_host = "0.0.0.0" if remote else _resolve_bind_host()
    auth_token = _resolve_auth_token()
    server_url = _resolve_server_url(port)

    # ── Compact bootstrap: all steps on one screen ──

    # 0. Pre-startup git hygiene — clean stale worktrees/branches from prior runs
    try:
        from bernstein.core.git_hygiene import run_hygiene

        run_hygiene(workdir, full=True)
    except Exception:
        pass

    # 1. Parse seed
    seed = parse_seed(seed_path)
    if cli is not None:
        object.__setattr__(seed, "cli", cli)
    if model is not None:
        object.__setattr__(seed, "model", model)
    preflight_checks(seed.cli, port)
    effective_cells = cells if cells is not None else seed.cells

    # 2. Workspace + catalog + index (silent — errors logged, not printed)
    ensure_sdd(workdir)
    _clean_stale_runtime(workdir)
    _discover_catalog(workdir)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_build_codebase_index, workdir)
        with contextlib.suppress(concurrent.futures.TimeoutError):
            future.result(timeout=10)

    # Safety invariants (silent unless violations)
    try:
        from bernstein.evolution.invariants import verify_invariants, write_lockfile

        ok, violations = verify_invariants(workdir)
        if not ok:
            console.print(f"[bold red]⚠ {len(violations)} locked file(s) modified[/bold red]")
        write_lockfile(workdir)
    except Exception:
        pass

    # Storage + cluster config (env vars, no output)
    if seed.storage is not None:
        os.environ.setdefault("BERNSTEIN_STORAGE_BACKEND", seed.storage.backend)
        if seed.storage.database_url:
            os.environ.setdefault("BERNSTEIN_DATABASE_URL", seed.storage.database_url)
        if seed.storage.redis_url:
            os.environ.setdefault("BERNSTEIN_REDIS_URL", seed.storage.redis_url)

    cluster_enabled = (seed.cluster is not None and seed.cluster.enabled) or os.environ.get(
        "BERNSTEIN_CLUSTER_ENABLED", ""
    ).lower() in ("1", "true", "yes")

    # Compliance (env var override or seed)
    compliance_env = os.environ.get("BERNSTEIN_COMPLIANCE")
    if compliance_env:
        from bernstein.core.compliance import ComplianceConfig, CompliancePreset

        ComplianceConfig.from_preset(CompliancePreset(compliance_env.lower()))

    # 3. Start server (compact output — single line)
    server_pid = _start_server(
        workdir,
        port,
        bind_host=bind_host,
        cluster_enabled=cluster_enabled,
        auth_token=auth_token,
        evolve_mode=evolve_mode,
    )
    if not _wait_for_server(port, server_url=server_url):
        from bernstein.cli.errors import BernsteinError

        BernsteinError(
            what=f"Task server on port {port} did not respond within 10.0s",
            why="Server process may have crashed during startup",
            fix="Check .sdd/runtime/server.log for details",
        ).print()
        raise SystemExit(1)
    console.print(f"  [dim]server[/dim]  :{port} [green]ready[/green]")

    # 4. Sync backlog / create manager task
    from bernstein.core.session import check_resume_session
    from bernstein.core.sync import sync_backlog_to_server

    _resume = seed.session.resume
    _stale_minutes = seed.session.stale_after_minutes
    prior_session = check_resume_session(
        workdir,
        force_fresh=force_fresh or not _resume,
        stale_minutes=_stale_minutes,
    )

    sync_result = sync_backlog_to_server(workdir, server_url=server_url)
    backlog_count = len(sync_result.created) + len(sync_result.skipped)

    manager_task_id = ""
    if prior_session is not None:
        console.print(f"  [dim]resume[/dim]  {len(prior_session.completed_task_ids)} done previously")
    elif backlog_count > 0:
        console.print(f"  [dim]tasks[/dim]   {backlog_count} from backlog")
    else:
        manager_task_id = _inject_manager_task(
            seed,
            workdir,
            port,
            server_url=server_url,
            auth_token=auth_token,
        )
        console.print("  [dim]plan[/dim]    manager agent will decompose goal")

    # Cost estimate (single compact line)
    from bernstein.core.cost import estimate_run_cost

    est_count = backlog_count if backlog_count > 0 else 5
    est_model = seed.model or "sonnet"
    low, high = estimate_run_cost(est_count, est_model)
    console.print(f"  [dim]cost[/dim]    ~${low:.2f}-${high:.2f} ({est_count} tasks, {est_model})")

    # 5. Start spawner + watchdog
    spawner_pid = _start_spawner(
        workdir,
        port,
        cells=effective_cells,
        server_url=server_url,
        auth_token=auth_token,
        cluster_enabled=cluster_enabled,
    )
    _start_watchdog(workdir, port)
    console.print(f"  [dim]agents[/dim]  spawning (max {seed.max_agents})")

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
    cli: str = "auto",
    cells: int = 1,
    force_fresh: bool = False,
    model: str | None = None,
) -> BootstrapResult:
    """Bootstrap from an inline goal string (no YAML file needed).

    Creates a minimal SeedConfig from the goal and delegates to the
    standard bootstrap flow.  When ``cli="auto"`` (the default), the best
    available CLI agent is detected automatically — no configuration required.

    Args:
        goal: Plain-text project goal.
        workdir: Project root directory.
        port: TCP port for the task server.
        cli: CLI backend to use, or "auto" to detect automatically.
        cells: Number of parallel orchestration cells.
        force_fresh: Ignore any saved session and start from scratch.
        model: Optional model override (e.g. "opus", "sonnet").

    Returns:
        BootstrapResult with PIDs and task ID.
    """
    seed = SeedConfig(goal=goal, cli=cli, model=model)  # type: ignore[arg-type]

    # Detect first run: no .sdd/ and no bernstein.yaml yet
    first_run = not (workdir / ".sdd").exists() and not (workdir / "bernstein.yaml").exists()
    if first_run and cli == "auto":
        console.print("[dim]No project setup found. Auto-detecting...[/dim]")

    console.print(f"[green]→[/green] Goal: [bold]{goal[:80]}[/bold]")

    # Pre-flight: verify binary, API key, and port before touching anything.
    with Status("[bold]Running pre-flight checks...[/bold]", console=console):
        preflight_checks(cli, port)

    # Initialise workspace
    with Status("[bold]Creating workspace...[/bold]", console=console):
        created = ensure_sdd(workdir)
        if first_run and not (workdir / "bernstein.yaml").exists():
            auto_write_bernstein_yaml(workdir)
        _clean_stale_runtime(workdir)
    if created:
        console.print("[green]→[/green] Created .sdd/ workspace")
    else:
        console.print("[green]→[/green] Workspace ready")

    with Status("[bold]Loading agent catalog...[/bold]", console=console):
        _discover_catalog(workdir)
    console.print("[green]→[/green] Agent catalog loaded")

    with (
        Status("[bold]Indexing codebase...[/bold]", console=console),
        concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(_build_codebase_index, workdir)
        try:
            future.result(timeout=10)
        except concurrent.futures.TimeoutError:
            console.print("[yellow]→[/yellow] Indexing taking too long — continuing in background")
    console.print("[green]→[/green] Codebase indexed")

    with Status("[bold]Checking safety invariants...[/bold]", console=console):
        from bernstein.evolution.invariants import verify_invariants, write_lockfile

        ok, violations = verify_invariants(workdir)
        if not ok:
            console.print(f"[bold red]SAFETY: {len(violations)} locked file(s) modified[/bold red]")
            for v in violations:
                console.print(f"  [red]{v}[/red]")
        write_lockfile(workdir)

    bind_host = _resolve_bind_host()
    auth_token = _resolve_auth_token()
    server_url = _resolve_server_url(port)

    with Status(f"[bold]Starting task server on {bind_host}:{port}...[/bold]", console=console):
        server_pid = _start_server(workdir, port, bind_host=bind_host)
        if not _wait_for_server(port, server_url=server_url):
            from bernstein.cli.errors import BernsteinError

            BernsteinError(
                what=f"Task server on port {port} did not respond within 10.0s",
                why="Server process may have crashed during startup",
                fix="Check .sdd/runtime/server.log for details",
            ).print()
            raise SystemExit(1)
    console.print(f"[green]→[/green] Task server ready (PID {server_pid}, {bind_host}:{port})")

    # Sync backlog first; only use manager if backlog is empty and no prior session
    from bernstein.core.session import check_resume_session
    from bernstein.core.sync import sync_backlog_to_server

    prior_session = check_resume_session(workdir, force_fresh=force_fresh)

    with Status("[bold]Loading tasks...[/bold]", console=console):
        sync_result = sync_backlog_to_server(workdir, server_url=server_url)
    backlog_count = len(sync_result.created) + len(sync_result.skipped)

    manager_task_id = ""
    if prior_session is not None:
        completed_count = len(prior_session.completed_task_ids)
        console.print(
            f"[bold cyan]Resuming from previous session[/bold cyan] "
            f"({completed_count} task(s) already completed — skipping re-planning)"
        )
    elif backlog_count > 0:
        console.print(
            f"[green]→[/green] Planning tasks ({backlog_count} found in backlog"
            + (f", {len(sync_result.skipped)} already synced" if sync_result.skipped else "")
            + ")"
        )
    else:
        with Status("[bold]Creating planning task...[/bold]", console=console):
            manager_task_id = _inject_manager_task(
                seed,
                workdir,
                port,
                server_url=server_url,
                auth_token=auth_token,
            )
        console.print("[green]→[/green] Planning tasks (manager agent will decompose goal)")

    # Cost estimation — show before spawning agents
    from bernstein.core.cost import estimate_run_cost

    est_task_count = backlog_count if backlog_count > 0 else 5  # default estimate for manager-planned
    low, high = estimate_run_cost(est_task_count, "sonnet")
    console.print(
        f"[bold yellow]Cost estimate:[/bold yellow] ${low:.2f}-${high:.2f} ({est_task_count} task(s), sonnet model)"
    )

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
