"""Bootstrap orchestration: coordinate startup, task planning, and agent spawning.

This module orchestrates the full bootstrap flow: parsing seed, starting server,
and spawning agents. It imports lower-level startup logic from server_launch.py
and preflight checks from preflight.py.

Entry points:
- bootstrap_from_seed() — read bernstein.yaml and launch
- bootstrap_from_goal() — quick launch from inline goal string
"""

from __future__ import annotations

import asyncio as _asyncio
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

from bernstein.cli.display.icons import get_icons

if TYPE_CHECKING:
    from collections.abc import Awaitable as _Awaitable

    from bernstein.core.models import Task

# Import from sub-modules (facade re-exports)
from bernstein.core.config_path_validation import check_config_paths
from bernstein.core.log_redact import install_pii_filter
from bernstein.core.orchestration.preflight import (
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


# ---------------------------------------------------------------------------
# Singleton PID lock
# ---------------------------------------------------------------------------


def _acquire_pid_lock(workdir: Path) -> None:
    """Ensure only one Bernstein instance runs per working directory.

    Writes the current PID to ``.sdd/runtime/bernstein.pid``.  If the file
    already exists and the recorded PID is still alive, raises
    ``RuntimeError`` to prevent data corruption from concurrent instances.

    The PID file is removed on clean shutdown via :func:`_release_pid_lock`.

    Args:
        workdir: Project root directory.

    Raises:
        RuntimeError: If another live instance owns the PID file.
    """
    runtime_dir = workdir / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    pid_path = runtime_dir / "bernstein.pid"

    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            existing_pid = -1

        if existing_pid > 0:
            from bernstein.core.platform_compat import process_alive

            if process_alive(existing_pid):
                raise RuntimeError(
                    f"Another Bernstein instance is running (PID {existing_pid}). "
                    f"Stop it first with 'bernstein stop' or remove {pid_path}"
                )

    pid_path.write_text(str(os.getpid()))

    import atexit

    atexit.register(_release_pid_lock, workdir)


def _release_pid_lock(workdir: Path) -> None:
    """Remove the PID lock file on clean shutdown.

    Only removes the file if it still contains our PID (guards against a
    race where a new instance has already replaced the file).

    Args:
        workdir: Project root directory.
    """
    pid_path = workdir / ".sdd" / "runtime" / "bernstein.pid"
    try:
        if pid_path.exists() and int(pid_path.read_text().strip()) == os.getpid():
            pid_path.unlink(missing_ok=True)
    except (ValueError, OSError):
        pass


# ---------------------------------------------------------------------------
# MCP auto-discovery helpers
# ---------------------------------------------------------------------------


def _register_mcp_discovery(workdir: Path) -> None:
    """Write Bernstein into .claude/mcp.json so Claude Code auto-discovers it.

    Any Claude Code session opened in ``workdir`` will automatically have
    access to the Bernstein orchestration tools (bernstein_status, etc.)
    without manual configuration.

    Args:
        workdir: Project root directory.
    """
    import json as _json

    mcp_path = workdir / ".claude" / "mcp.json"
    mcp_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, object] = {}
    if mcp_path.exists():
        try:
            existing = _json.loads(mcp_path.read_text())
        except (ValueError, OSError):
            existing = {}

    servers = dict(existing.get("mcpServers", {}))  # type: ignore[arg-type]
    servers["bernstein"] = {
        "command": sys.executable,
        "args": ["-m", "bernstein.mcp.server"],
    }
    existing["mcpServers"] = servers
    mcp_path.write_text(_json.dumps(existing, indent=2) + "\n")
    logger.debug("Registered Bernstein MCP server in %s", mcp_path)


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


def _run_git_hygiene(workdir: Path) -> None:
    """Run pre-startup git hygiene, logging warnings on failure."""
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


def _index_codebase_with_timeout(workdir: Path, timeout: float = 10) -> None:
    """Build codebase index with a hard timeout to avoid blocking startup."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(_build_codebase_index, workdir)
    with contextlib.suppress(concurrent.futures.TimeoutError):
        future.result(timeout=timeout)
    pool.shutdown(wait=False)


def _check_safety_invariants(workdir: Path) -> None:
    """Verify locked-file invariants, logging warnings on failure."""
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


def _apply_storage_env(seed: Any) -> None:
    """Set storage-related environment variables from seed config."""
    if seed.storage is None:
        return
    os.environ.setdefault("BERNSTEIN_STORAGE_BACKEND", seed.storage.backend)
    if seed.storage.database_url:
        os.environ.setdefault("BERNSTEIN_DATABASE_URL", seed.storage.database_url)
    if seed.storage.redis_url:
        os.environ.setdefault("BERNSTEIN_REDIS_URL", seed.storage.redis_url)


def _apply_compliance_env() -> None:
    """Apply compliance preset from BERNSTEIN_COMPLIANCE env var."""
    compliance_env = os.environ.get("BERNSTEIN_COMPLIANCE")
    if not compliance_env:
        return
    from bernstein.core.compliance import ComplianceConfig, CompliancePreset

    ComplianceConfig.from_preset(CompliancePreset(compliance_env.lower()))


def _register_ci_parsers() -> None:
    """Populate the CI log parser registry with all built-in adapters.

    Without this call the registry is empty at runtime, so
    ``bernstein ci fix --parser gitlab_ci`` and the self-healing CI
    pipeline silently no-op (see audit-031). The helper in
    :mod:`bernstein.adapters.ci` is idempotent, so calling it here on
    top of the import-time side-effect is safe.
    """
    try:
        from bernstein.adapters.ci import register_built_in_ci_parsers

        register_built_in_ci_parsers()
    except ImportError as exc:
        logger.warning("CI adapters unavailable — skipping parser registration: %s", exc)


def _load_secrets_provider(seed: Any) -> None:
    """Load secrets provider if configured in seed."""
    if not seed.secrets:
        return
    from bernstein.core.secrets import SecretsRefresher, load_secrets

    try:
        load_secrets(seed.secrets)
        refresher = SecretsRefresher(seed.secrets)
        refresher.start()
        import atexit

        atexit.register(refresher.stop)
        console.print(f"  [dim]secrets[/dim] load from {seed.secrets.provider} [green]ok[/green]")
    except Exception as sec_exc:
        console.print(f"  [red]✗[/red] [dim]secrets[/dim] load failed: {sec_exc}")
        raise SystemExit(1) from sec_exc


def _sync_and_plan_tasks(
    seed: Any,
    workdir: Path,
    port: int,
    server_url: str,
    auth_token: str | None,
    force_fresh: bool,
) -> tuple[int, str, Any]:
    """Sync backlog to server, import workflows, and determine planning mode.

    Returns:
        Tuple of (backlog_count, manager_task_id, prior_session).
    """
    from bernstein.core.session import check_resume_session
    from bernstein.core.sync import sync_backlog_to_server

    # Sync open GitHub Issues into .sdd/backlog/open/ before server sync.
    try:
        from bernstein.core.github import sync_github_issues_to_backlog

        gh_count = sync_github_issues_to_backlog(workdir)
        if gh_count > 0:
            console.print(f"  [dim]github[/dim]  synced {gh_count} issue(s) to backlog")
    except Exception as exc:
        logger.debug("GitHub issue sync skipped: %s", exc)

    _resume = seed.session.resume
    _stale_minutes = seed.session.stale_after_minutes
    prior_session = check_resume_session(
        workdir,
        force_fresh=force_fresh or not _resume,
        stale_minutes=_stale_minutes,
    )

    task_filter = os.environ.get("BERNSTEIN_TASK_FILTER")
    sync_result = sync_backlog_to_server(
        workdir,
        server_url=server_url,
        task_filter=task_filter,
        auth_token=auth_token,
    )
    backlog_count = len(sync_result.created) + len(sync_result.skipped)

    # Import unchecked items from TODO.md / TASKS.md / .plan if present.
    try:
        from bernstein.core.workflow_importer import import_workflow_tasks

        with httpx.Client(timeout=10.0) as _wf_client:
            _wf_imported = import_workflow_tasks(workdir, _wf_client, server_url)
        if _wf_imported:
            console.print(f"  [dim]workflow[/dim] {_wf_imported} task(s) from workflow file(s)")
            backlog_count += _wf_imported
    except Exception as _wf_exc:
        logger.debug("Workflow import skipped: %s", _wf_exc)

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

    return backlog_count, manager_task_id, prior_session


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
        evolve_mode: Retained for back-compat. Uvicorn ``--reload`` was
            removed 2026-04-17 (audit-115); this flag no longer alters
            the server launch. Agents pick up source changes only when
            the supervisor restarts the server for real (crash/health).
        cli: Optional CLI override (e.g. "claude", "codex"). Overrides seed config.
        model: Optional model override (e.g. "opus", "sonnet"). Overrides seed config.

    Returns:
        BootstrapResult with PIDs and task ID.

    Raises:
        bernstein.core.seed.SeedError: If the seed file is invalid.
        RuntimeError: If the server fails to start or respond, or if another
            Bernstein instance is already running in this directory.
    """
    # Singleton guard: prevent two instances on the same workdir
    _acquire_pid_lock(workdir)

    # Resolve cluster-aware settings
    bind_host = "0.0.0.0" if remote else _resolve_bind_host()
    auth_token = _resolve_auth_token()
    server_url = _resolve_server_url(port)

    # ── Compact bootstrap: all steps on one screen ──

    # 0. Pre-startup git hygiene — clean stale worktrees/branches from prior runs
    _run_git_hygiene(workdir)

    # 1. Parse seed
    seed = parse_seed(seed_path)
    if cli is not None:
        object.__setattr__(seed, "cli", cli)
    if model is not None:
        object.__setattr__(seed, "model", model)
    preflight_checks(seed.cli, port)
    check_config_paths(seed, workdir)
    effective_cells = cells if cells is not None else seed.cells

    # 2. Workspace + catalog + index (silent — errors logged, not printed)
    ensure_sdd(workdir)
    _clean_stale_runtime(workdir)
    _discover_catalog(workdir)
    _index_codebase_with_timeout(workdir)
    _check_safety_invariants(workdir)

    # Storage + cluster config (env vars, no output)
    _apply_storage_env(seed)

    cluster_enabled = (seed.cluster is not None and seed.cluster.enabled) or os.environ.get(
        "BERNSTEIN_CLUSTER_ENABLED", ""
    ).lower() in ("1", "true", "yes")

    _apply_compliance_env()

    # Populate CI log parser registry so `bernstein ci fix` and pipeline
    # self-healing can find GitHub Actions / GitLab CI parsers (audit-031).
    _register_ci_parsers()

    # 3. Load secrets provider if configured
    _load_secrets_provider(seed)

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

    # Register Bernstein as a discoverable MCP server for Claude Code sessions
    with contextlib.suppress(OSError):
        _register_mcp_discovery(workdir)

    # 4. Sync backlog / create manager task
    backlog_count, manager_task_id, _prior_session = _sync_and_plan_tasks(
        seed,
        workdir,
        port,
        server_url,
        auth_token,
        force_fresh,
    )

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


def _watchdog_check_process(
    *,
    name: str,
    pid: int | None,
    alive_since: float | None,
    restarts: int,
    give_up_logged: bool,
    max_restarts: int,
    reset_after_s: float,
    now: float,
    restart_fn: Any,
    post_restart_fn: Any | None = None,
) -> tuple[float | None, int, bool]:
    """Check a single watchdog-monitored process and restart if dead.

    Returns:
        Updated (alive_since, restarts, give_up_logged) tuple.
    """
    if pid is not None and _is_alive(pid):
        if alive_since is None:
            return now, restarts, give_up_logged
        if restarts > 0 and (now - alive_since) >= reset_after_s:
            logger.info(
                "%s has been healthy for %.0fs — resetting restart counter",
                name,
                now - alive_since,
            )
            return alive_since, 0, False
        return alive_since, restarts, give_up_logged

    # Process is dead
    if restarts >= max_restarts:
        if not give_up_logged:
            logger.error(
                "%s exceeded max restarts (%d), giving up; will resume monitoring once the process recovers",
                name,
                max_restarts,
            )
        return None, restarts, True

    logger.warning("%s (PID %s) is dead, restarting...", name, pid)
    try:
        new_pid = restart_fn()
        if new_pid == -1:
            return None, restarts, give_up_logged  # skip (e.g. server not alive)
        logger.info("%s restarted (PID %d)", name, new_pid)
        restarts += 1
        if post_restart_fn is not None:
            post_restart_fn()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error(
            "Failed to restart %s (%s: %s) — will retry next cycle",
            name,
            type(exc).__name__,
            exc,
        )
    return None, restarts, give_up_logged


def run_watchdog(workdir: Path, port: int, poll_s: float = 5.0) -> None:
    """Monitor the server and orchestrator, restarting them if they die.

    This blocks forever and should be run as a background daemon.

    The restart counter for each subprocess resets to 0 once the process has
    been observed alive continuously for ``RESTART_RESET_AFTER_S``. This
    prevents a single bad day (e.g. one buggy ``/status`` field flapping the
    server) from permanently disabling the watchdog: a healthy day earns the
    process its restart budget back. Without this, the watchdog gives up
    forever after 5 transient failures across the entire run (incident
    2026-04-11).

    Args:
        workdir: Project root directory.
        port: Task server port.
        poll_s: Seconds between health checks.
    """
    server_pid_path = workdir / ".sdd" / "runtime" / "server.pid"
    spawner_pid_path = workdir / ".sdd" / "runtime" / "spawner.pid"
    max_restarts = 5
    restart_reset_after_s = 120.0  # reset counter after this much continuous uptime
    server_restarts = 0
    spawner_restarts = 0
    server_alive_since: float | None = None
    spawner_alive_since: float | None = None
    server_give_up_logged = False
    spawner_give_up_logged = False

    while True:
        time.sleep(poll_s)
        now = time.monotonic()

        # Check server
        server_pid = _read_pid(server_pid_path)
        server_alive_since, server_restarts, server_give_up_logged = _watchdog_check_process(
            name="Server",
            pid=server_pid,
            alive_since=server_alive_since,
            restarts=server_restarts,
            give_up_logged=server_give_up_logged,
            max_restarts=max_restarts,
            reset_after_s=restart_reset_after_s,
            now=now,
            restart_fn=lambda: _start_server(workdir, port),
            post_restart_fn=lambda: _wait_for_server(port),
        )

        # Check orchestrator/spawner (only restart if server is alive)
        spawner_pid = _read_pid(spawner_pid_path)

        def _restart_spawner() -> int:
            cur_server_pid = _read_pid(server_pid_path)
            if cur_server_pid is None or not _is_alive(cur_server_pid):
                return -1  # signal: skip restart
            return _start_spawner(workdir, port)

        spawner_alive_since, spawner_restarts, spawner_give_up_logged = _watchdog_check_process(
            name="Orchestrator",
            pid=spawner_pid,
            alive_since=spawner_alive_since,
            restarts=spawner_restarts,
            give_up_logged=spawner_give_up_logged,
            max_restarts=max_restarts,
            reset_after_s=restart_reset_after_s,
            now=now,
            restart_fn=_restart_spawner,
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
    return _bootstrap_from_goal_impl(
        goal=goal,
        workdir=workdir,
        port=port,
        cli=cli,
        cells=cells,
        force_fresh=force_fresh,
        model=model,
        ab_test=ab_test,
        tasks=tasks,
    )


def _goal_sync_and_plan(
    *,
    seed: Any,
    workdir: Path,
    port: int,
    server_url: str,
    auth_token: str | None,
    force_fresh: bool,
    tasks: list[Task] | None,
    icons: Any,
) -> tuple[int, str, Any]:
    """Sync backlog, import workflows, post plan tasks for goal-based bootstrap.

    Returns:
        Tuple of (backlog_count, manager_task_id, sync_result).
    """
    from bernstein.core.session import check_resume_session
    from bernstein.core.sync import sync_backlog_to_server

    # Sync open GitHub Issues into .sdd/backlog/open/ before server sync.
    try:
        from bernstein.core.github import sync_github_issues_to_backlog

        gh_count = sync_github_issues_to_backlog(workdir)
        if gh_count > 0:
            console.print(f"[green]{icons.arrow_right}[/green] Synced {gh_count} GitHub issue(s) to backlog")
    except Exception as exc:
        logger.debug("GitHub issue sync skipped: %s", exc)

    prior_session = check_resume_session(workdir, force_fresh=force_fresh)

    task_filter = os.environ.get("BERNSTEIN_TASK_FILTER")
    with Status("[bold]Loading tasks...[/bold]", console=console):
        sync_result = sync_backlog_to_server(
            workdir,
            server_url=server_url,
            task_filter=task_filter,
            auth_token=auth_token,
        )
    backlog_count = len(sync_result.created) + len(sync_result.skipped)

    # Import unchecked items from TODO.md / TASKS.md / .plan if present.
    try:
        from bernstein.core.workflow_importer import import_workflow_tasks

        with httpx.Client(timeout=10.0) as _wf_client:
            _wf_imported = import_workflow_tasks(workdir, _wf_client, server_url)
        if _wf_imported:
            console.print(f"[green]{icons.arrow_right}[/green] Imported {_wf_imported} task(s) from workflow file(s)")
            backlog_count += _wf_imported
    except Exception as _wf_exc:
        logger.debug("Workflow import skipped: %s", _wf_exc)

    manager_task_id = ""
    if prior_session is not None:
        completed_count = len(prior_session.completed_task_ids)
        console.print(
            f"[bold cyan]Resuming from previous session[/bold cyan] "
            f"({completed_count} task(s) already completed — skipping re-planning)"
        )
    elif tasks:
        _post_plan_tasks(tasks, server_url, icons)
        backlog_count = len(tasks)
    elif backlog_count > 0:
        console.print(
            f"[green]{icons.arrow_right}[/green] Planning tasks ({backlog_count} found in backlog"
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
        console.print(f"[green]{icons.arrow_right}[/green] Planning tasks (manager agent will decompose goal)")

    return backlog_count, manager_task_id, sync_result


def _post_plan_tasks(tasks: list[Task], server_url: str, icons: Any) -> None:
    """Post pre-defined plan tasks to the server."""
    import asyncio

    from bernstein.core.planner import _post_task_to_server

    with Status(f"[bold]Posting {len(tasks)} tasks to server...[/bold]", console=console):

        async def _post_all() -> None:
            async with httpx.AsyncClient(timeout=10.0) as client:
                id_map: dict[str, str] = {}
                for t in tasks:
                    t.depends_on = [id_map.get(dep, dep) for dep in t.depends_on]
                    old_id = t.id
                    server_id = await _post_task_to_server(client, server_url, t)
                    t.id = server_id
                    id_map[old_id] = server_id

        asyncio.run(with_init_timeout(_post_all(), context="posting tasks from plan file"))
    console.print(f"[green]{icons.arrow_right}[/green] Posted {len(tasks)} tasks from plan file")


def _bootstrap_from_goal_impl(
    *,
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
    """Internal implementation of bootstrap_from_goal.

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
    # Singleton guard: prevent two instances on the same workdir
    _acquire_pid_lock(workdir)

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

    _icons = get_icons()
    console.print(f"[green]{_icons.arrow_right}[/green] Goal: [bold]{goal[:80]}[/bold]")
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
        console.print(f"[green]{_icons.arrow_right}[/green] Created .sdd/ workspace")
    else:
        console.print(f"[green]{_icons.arrow_right}[/green] Workspace ready")

    with Status("[bold]Loading agent catalog...[/bold]", console=console):
        _discover_catalog(workdir)
    console.print(f"[green]{_icons.arrow_right}[/green] Agent catalog loaded")

    # Index codebase with a hard 10s deadline - don't block startup.
    # We must NOT use ThreadPoolExecutor as a context manager because its
    # __exit__ calls shutdown(wait=True), which blocks until the thread
    # finishes even after the timeout fires.
    _index_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    _index_future = _index_pool.submit(_build_codebase_index, workdir)
    with Status("[bold]Indexing codebase...[/bold]", console=console):
        try:
            _index_future.result(timeout=10)
        except concurrent.futures.TimeoutError:
            console.print(f"[yellow]{_icons.arrow_right}[/yellow] Indexing taking too long - continuing in background")
    _index_pool.shutdown(wait=False)
    console.print(f"[green]{_icons.arrow_right}[/green] Codebase indexed")

    with Status("[bold]Checking safety invariants...[/bold]", console=console):
        from bernstein.evolution.invariants import verify_invariants, write_lockfile

        ok, violations = verify_invariants(workdir)
        if not ok:
            console.print(f"[bold red]SAFETY: {len(violations)} locked file(s) modified[/bold red]")
            for v in violations:
                console.print(f"  [red]{v}[/red]")
        write_lockfile(workdir)

    # Populate CI log parser registry (audit-031).
    _register_ci_parsers()

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
    console.print(f"[green]{_icons.arrow_right}[/green] Task server ready (PID {server_pid}, {bind_host}:{port})")

    # Register Bernstein as a discoverable MCP server for Claude Code sessions
    with contextlib.suppress(OSError):
        _register_mcp_discovery(workdir)

    # Sync backlog and determine planning mode
    backlog_count, manager_task_id, _sync_result = _goal_sync_and_plan(
        seed=seed,
        workdir=workdir,
        port=port,
        server_url=server_url,
        auth_token=auth_token,
        force_fresh=force_fresh,
        tasks=tasks,
        icons=_icons,
    )

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
    console.print(f"[green]{_icons.arrow_right}[/green] Spawning agents (PID {spawner_pid})")

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


# ---------------------------------------------------------------------------
# Initialization timeout guard (T583)
# ---------------------------------------------------------------------------

INIT_TIMEOUT_SECONDS: float = 30.0


async def with_init_timeout[T](
    coro: _Awaitable[T],
    *,
    timeout: float = INIT_TIMEOUT_SECONDS,
    context: str = "initialization",
) -> T:
    """Wrap an awaitable with a 30-second initialization timeout guard (T583).

    Prevents deadlocks during startup by raising :class:`asyncio.TimeoutError`
    if the awaitable does not complete within *timeout* seconds.

    Args:
        coro: Awaitable to wrap.
        timeout: Timeout in seconds (default: 30).
        context: Human-readable context for the timeout log message.

    Returns:
        Result of the awaitable.

    Raises:
        asyncio.TimeoutError: If the awaitable exceeds *timeout* seconds.
    """
    try:
        async with _asyncio.timeout(timeout):
            return await coro
    except TimeoutError:
        logger.error(
            "Initialization timeout after %.0fs during '%s' — possible deadlock",
            timeout,
            context,
        )
        raise
