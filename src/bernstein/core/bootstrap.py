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
from typing import TYPE_CHECKING, Any

import httpx
from rich.console import Console
from rich.status import Status

if TYPE_CHECKING:
    from bernstein.core.models import Task

# Import from sub-modules (facade re-exports)
from bernstein.core.log_redact import install_pii_filter
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
from bernstein.core.server_supervisor import supervised_server

logger = logging.getLogger(__name__)

# Install PII redaction on the root logger so all handlers receive sanitised
# messages — no email, phone, SSN, or credit-card number reaches disk/stdout.
install_pii_filter()
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
    except httpx.RequestError as exc:
        logger.error(
            "Webhook POST to %s failed (%s: %s) — continuing without notification",
            config.webhook_url,
            type(exc).__name__,
            exc,
        )


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
    ab_test: bool = False,
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
    except ImportError as exc:
        logger.warning("Git hygiene module unavailable — skipping: %s", exc)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "Pre-startup git hygiene failed (%s: %s) — continuing",
            type(exc).__name__,
            exc,
        )

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

    _idx_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _idx_future = _idx_pool.submit(_build_codebase_index, workdir)
    with contextlib.suppress(concurrent.futures.TimeoutError):
        _idx_future.result(timeout=10)
    _idx_pool.shutdown(wait=False)

    # Safety invariants (silent unless violations)
    try:
        from bernstein.evolution.invariants import verify_invariants, write_lockfile

        ok, violations = verify_invariants(workdir)
        if not ok:
            console.print(f"[bold red]⚠ {len(violations)} locked file(s) modified[/bold red]")
        write_lockfile(workdir)
    except ImportError as exc:
        logger.warning("Invariants module unavailable — skipping check: %s", exc)
    except OSError as exc:
        logger.warning(
            "Invariant check failed (%s: %s) — continuing",
            type(exc).__name__,
            exc,
        )

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

    # 3. Load secrets provider if configured
    if seed.secrets:
        from bernstein.core.secrets import SecretsRefresher, load_secrets

        # Initial fetch to ensure we have keys before starting
        try:
            load_secrets(seed.secrets)
            # Start background refresher
            refresher = SecretsRefresher(seed.secrets)
            refresher.start()
            # Register for shutdown (best effort)
            import atexit

            atexit.register(refresher.stop)
            console.print(f"  [dim]secrets[/dim] load from {seed.secrets.provider} [green]ok[/green]")
        except Exception as sec_exc:
            console.print(f"  [red]✗[/red] [dim]secrets[/dim] load failed: {sec_exc}")
            raise SystemExit(1) from sec_exc

    # 4. Start server (compact output — single line)
    server_pid = supervised_server(
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
        ab_test=ab_test,
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
            except (OSError, subprocess.SubprocessError) as exc:
                logger.error(
                    "Failed to restart server (%s: %s) — will retry next cycle",
                    type(exc).__name__,
                    exc,
                )

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
                except (OSError, subprocess.SubprocessError) as exc:
                    logger.error(
                        "Failed to restart orchestrator (%s: %s) — will retry next cycle",
                        type(exc).__name__,
                        exc,
                    )


def bootstrap_from_goal(
    goal: str,
    workdir: Path,
    port: int = 8052,
    cli: str = "auto",
    cells: int = 1,
    force_fresh: bool = False,
    model: str | None = None,
    ab_test: bool = False,
    tasks: list[Task] | None = None,
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
        tasks: Pre-defined tasks to execute (skips LLM planning).

    Returns:
        BootstrapResult with PIDs and task ID.
    """
    seed = SeedConfig(goal=goal, cli=cli, model=model)  # type: ignore[arg-type]

    # Detect first run: no .sdd/ and no bernstein.yaml yet
    first_run = not (workdir / ".sdd").exists() and not (workdir / "bernstein.yaml").exists()
    if first_run and cli == "auto":
        from bernstein.core.agent_discovery import discover_agents_cached
        from bernstein.core.server_launch import _detect_project_type

        disc = discover_agents_cached()
        project_type = _detect_project_type(workdir)
        agent_names = [a.name for a in disc.agents if a.logged_in] or [a.name for a in disc.agents]

        type_note = f"[cyan]{project_type}[/cyan] project" if project_type != "generic" else "project"
        if agent_names:
            agents_note = f"  agents: [green]{', '.join(agent_names)}[/green]"
        else:
            agents_note = "  [yellow]No agents found — install claude, codex, or gemini[/yellow]"

        console.print(f"[bold]First run detected[/bold] — auto-configuring for {type_note}")
        console.print(agents_note)

    console.print(f"[green]→[/green] Goal: [bold]{goal[:80]}[/bold]")
    try:
        from bernstein.core.complexity_advisor import ComplexityMode, suggest_goal_execution_mode

        suggestion = suggest_goal_execution_mode(goal)
        if suggestion is not None and suggestion.mode == ComplexityMode.SINGLE_AGENT:
            console.print(
                "[yellow]Suggestion:[/yellow] this goal looks simple enough for a single-agent session "
                f"({suggestion.reason})."
            )
    except Exception:
        logger.debug("Failed to compute inline goal execution suggestion", exc_info=True)

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

    # Index codebase with a hard 10s deadline — don't block startup.
    # We must NOT use ThreadPoolExecutor as a context manager because its
    # __exit__ calls shutdown(wait=True), which blocks until the thread
    # finishes even after the timeout fires.
    _index_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _index_future = _index_pool.submit(_build_codebase_index, workdir)
    with Status("[bold]Indexing codebase...[/bold]", console=console):
        try:
            _index_future.result(timeout=10)
        except concurrent.futures.TimeoutError:
            console.print("[yellow]→[/yellow] Indexing taking too long — continuing in background")
    _index_pool.shutdown(wait=False)
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
        server_pid = supervised_server(workdir, port, bind_host=bind_host)
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
    elif tasks:
        # Pre-defined tasks from plan file
        with Status(f"[bold]Posting {len(tasks)} tasks to server...[/bold]", console=console):
            from bernstein.core.planner import _post_task_to_server

            async def _post_all():
                async with httpx.AsyncClient(timeout=10.0) as client:
                    # Map: temporary plan ID -> server-assigned ID
                    id_map: dict[str, str] = {}
                    for t in tasks:
                        # Update depends_on using the map
                        t.depends_on = [id_map.get(dep, dep) for dep in t.depends_on]

                        # Post to server
                        old_id = t.id
                        server_id = await _post_task_to_server(client, server_url, t)
                        t.id = server_id
                        id_map[old_id] = server_id

            import asyncio

            asyncio.run(_post_all())
        console.print(f"[green]→[/green] Posted {len(tasks)} tasks from plan file")
        backlog_count = len(tasks)
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
        spawner_pid = _start_spawner(workdir, port, cells=cells, ab_test=ab_test)
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
        from bernstein.core.json_logging import setup_json_logging
        setup_json_logging()
        
        if not any(isinstance(h, logging.StreamHandler) for h in logging.getLogger().handlers):
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            )
        run_watchdog(Path.cwd(), _args.port)
