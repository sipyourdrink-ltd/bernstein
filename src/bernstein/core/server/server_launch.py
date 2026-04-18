"""Server and spawner lifecycle: startup, health checks, task injection, cleanup.

Handles the mechanics of launching the task server, waiting for readiness,
injecting the initial manager task, and starting the spawner.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from bernstein.core.process_utils import is_process_alive
from bernstein.core.router import TierAwareRouter, load_providers_from_yaml
from bernstein.core.runtime_state import rotate_log_file
from bernstein.core.seed import SeedConfig, seed_to_initial_task

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_SERVER_READY_TIMEOUT_S = 30.0
_SERVER_POLL_INTERVAL_S = 0.25


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

    # Preserve logs across runs, but rotate oversized ones.
    for log_name in ("server.log", "spawner.log"):
        rotate_log_file(runtime_dir / log_name)

    # Clear stale tasks.jsonl to start fresh
    jsonl = runtime_dir / "tasks.jsonl"
    if jsonl.exists():
        jsonl.unlink(missing_ok=True)

    # Clear stale SQLite WAL/SHM locks from codebase index (can block re-index)
    index_dir = workdir / ".sdd" / "index"
    if index_dir.exists():
        for lock_file in (*index_dir.glob("*.db-wal"), *index_dir.glob("*.db-shm")):
            lock_file.unlink(missing_ok=True)


def ensure_sdd(workdir: Path) -> bool:
    """Create .sdd/ workspace structure if it does not exist.

    Args:
        workdir: Project root directory.

    Returns:
        True if the workspace was newly created, False if it already existed.
    """
    sdd_dirs = (
        ".sdd",
        ".sdd/backlog",
        ".sdd/backlog/open",
        ".sdd/backlog/done",
        ".sdd/agents",
        ".sdd/runtime",
        ".sdd/docs",
        ".sdd/decisions",
    )
    created = False
    for d in sdd_dirs:
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

    # .gitignore for runtime dir — ensure session.json is always listed.
    gi_path = workdir / ".sdd" / "runtime" / ".gitignore"
    if not gi_path.exists():
        gi_path.write_text("*.pid\n*.log\ntasks.jsonl\nsession.json\n")
    else:
        existing = gi_path.read_text()
        if "session.json" not in existing:
            gi_path.write_text(existing.rstrip("\n") + "\nsession.json\n")

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
    return is_process_alive(pid)


def _discover_catalog(workdir: Path) -> None:
    """Run CatalogRegistry.discover() and sync Agency catalog on startup.

    Loads the agent catalog from cache (if fresh) or re-fetches from providers.
    Also attempts to sync the Agency GitHub catalog (TTL-protected, 24h).
    On failure the error is logged and startup continues — catalog is optional.

    Args:
        workdir: Project root directory.
    """
    from bernstein.agents.catalog import CatalogRegistry

    cache_path = workdir / ".sdd" / "agents" / "catalog.json"
    try:
        registry = CatalogRegistry.default()
        registry._cache_path = cache_path  # type: ignore[reportPrivateUsage]
        registry.discover()
    except Exception:
        logger.warning("Catalog auto-discovery failed (non-fatal)", exc_info=True)

    # Auto-sync Agency catalog (TTL = 24h — skipped if synced recently)
    try:
        from bernstein.agents.agency_provider import AgencyProvider

        ok, msg = AgencyProvider.sync_catalog()
        if ok:
            logger.debug("Agency catalog sync: %s", msg)
        else:
            logger.debug("Agency catalog sync skipped or failed: %s", msg)
    except Exception:
        logger.debug("Agency catalog auto-sync failed (non-fatal)", exc_info=True)


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
        build_or_update_index(workdir)
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


def _start_server(
    workdir: Path,
    port: int,
    bind_host: str = "127.0.0.1",
    cluster_enabled: bool = False,
    auth_token: str | None = None,
    evolve_mode: bool = False,
) -> int:
    """Launch the task server as a background process.

    Args:
        workdir: Project root (server runs from here).
        port: TCP port for the uvicorn server.
        bind_host: Host to bind to. Use "0.0.0.0" for remote access.
        cluster_enabled: Enable cluster endpoints and node reaper.
        auth_token: Bearer token for API auth.
        evolve_mode: Retained for signature compatibility. Uvicorn
            ``--reload`` was removed 2026-04-17 per audit-115 because
            auto-reload is catastrophic in self-modifying runs
            (file writes → restart → dropped HTTP connections → WAL
            replay duplicate claims). The flag no longer affects the
            launched uvicorn argv.

    Returns:
        PID of the server process.

    Raises:
        RuntimeError: If a server is already running on the PID file.
        SystemExit: If ``BERNSTEIN_WORKERS`` / ``WEB_CONCURRENCY`` request
            more than one uvicorn worker. Bernstein's ``TaskStore`` is
            single-process (audit-025) and multi-worker mode corrupts
            JSONL and allows double-claims.
    """
    # audit-025: refuse to launch multi-worker in the parent process so the
    # operator sees the error on the bernstein CLI instead of in server.log
    # after a silent subprocess crash.
    from bernstein.core.server.server_app import preflight_multi_worker_guard

    preflight_multi_worker_guard()
    logger.info("Starting task server on %s:%d (single-worker mode, audit-025)", bind_host, port)

    pid_path = workdir / ".sdd" / "runtime" / "server.pid"
    existing = _read_pid(pid_path)
    if existing is not None and _is_alive(existing):
        raise RuntimeError(f"Server already running (PID {existing}). Run `bernstein stop` first.")

    # Build env for the server subprocess — inherit parent env and overlay
    # cluster-specific and storage vars so the server's module-level app
    # factory picks them up.
    env = dict(os.environ)
    if cluster_enabled:
        env["BERNSTEIN_CLUSTER_ENABLED"] = "1"
    env["BERNSTEIN_BIND_HOST"] = bind_host
    if auth_token:
        env["BERNSTEIN_AUTH_TOKEN"] = auth_token
    # audit-025: pin the child to single-worker even if the parent env had
    # WEB_CONCURRENCY set (the preflight above already rejected that case,
    # but we belt-and-braces here in case a future code path bypasses it).
    env["BERNSTEIN_WORKERS"] = "1"
    env.pop("WEB_CONCURRENCY", None)

    # Propagate storage backend config if set in the current process env.
    # The server reads these at import time via the store_factory module.
    for _storage_var in ("BERNSTEIN_STORAGE_BACKEND", "BERNSTEIN_DATABASE_URL", "BERNSTEIN_REDIS_URL"):
        if _storage_var in os.environ and _storage_var not in env:
            env[_storage_var] = os.environ[_storage_var]

    server_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "bernstein.core.server:app",
        "--host",
        bind_host,
        "--port",
        str(port),
    ]
    # ``--reload`` was removed 2026-04-17 per audit-115 / incident
    # 2026-04-11.  Bernstein agents continuously edit src/bernstein/*.py,
    # so auto-reload causes a uvicorn restart on every write — in-flight
    # requests drop, the bind port races, and WAL replay produces
    # duplicate task claims.  evolve_mode is preserved in the signature
    # for back-compat but no longer toggles reload.
    _ = evolve_mode  # intentional: parameter retained for compatibility

    log_path = workdir / ".sdd" / "runtime" / "server.log"
    rotate_log_file(log_path)
    # Keep the log file open — child inherits the fd via fork().
    # Closing it prematurely can cause the child's stdout to break.
    log_fh = log_path.open("a")
    proc = subprocess.Popen(
        server_cmd,
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(workdir),
    )
    # Safe to close in parent after Popen — child has its own fd copy
    log_fh.close()
    pid_path.write_text(str(proc.pid))
    return proc.pid


def _wait_for_server(port: int, server_url: str | None = None) -> bool:
    """Block until the server responds to /health, or timeout.

    Args:
        port: Server port (used to build URL if server_url is None).
        server_url: Explicit base URL to check (overrides port).

    Returns:
        True if the server is reachable, False on timeout.
    """
    deadline = time.monotonic() + _SERVER_READY_TIMEOUT_S
    base = server_url or f"http://127.0.0.1:{port}"
    url = f"{base}/health"
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=5.0)
            if resp.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.TimeoutException):
            pass
        time.sleep(_SERVER_POLL_INTERVAL_S)
    return False


def _inject_manager_task(
    seed: SeedConfig,
    workdir: Path,
    port: int,
    server_url: str | None = None,
    auth_token: str | None = None,
) -> str:
    """Create the initial manager task on the running server.

    Args:
        seed: Parsed seed configuration.
        workdir: Project root for resolving context files.
        port: Server port (used if server_url is None).
        server_url: Explicit base URL of the task server.
        auth_token: Bearer token for authenticated requests.

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
        "model": "opus",
        "effort": "max",
    }

    base = server_url or f"http://127.0.0.1:{port}"
    headers: dict[str, str] = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    resp = httpx.post(
        f"{base}/tasks",
        json=payload,
        headers=headers,
        timeout=5.0,
    )
    if resp.status_code != 201:
        raise RuntimeError(f"Failed to create manager task: {resp.status_code} {resp.text}")

    data: dict[str, Any] = resp.json()
    return str(data.get("id", "unknown"))


def _start_spawner(
    workdir: Path,
    port: int,
    cells: int = 1,
    server_url: str | None = None,
    auth_token: str | None = None,
    cluster_enabled: bool = False,
    ab_test: bool = False,
) -> int:
    """Launch the spawner process in the background."""
    pid_path = workdir / ".sdd" / "runtime" / "spawner.pid"
    log_path = workdir / ".sdd" / "runtime" / "spawner.log"
    rotate_log_file(log_path)

    # Pass cluster-related env vars to the spawner subprocess so the
    # orchestrator's __main__ block can build ClusterConfig from them.
    env = dict(os.environ)
    if server_url:
        env["BERNSTEIN_SERVER_URL"] = server_url
    if auth_token:
        env["BERNSTEIN_AUTH_TOKEN"] = auth_token
    if cluster_enabled:
        env["BERNSTEIN_CLUSTER_ENABLED"] = "1"
    if ab_test:
        env["BERNSTEIN_AB_TEST"] = "1"

    log_fh = log_path.open("a")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "bernstein.core.orchestration.orchestrator",
            "--port",
            str(port),
            "--cells",
            str(cells),
        ],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(workdir),
    )
    log_fh.close()
    pid_path.write_text(str(proc.pid))
    return proc.pid


def _resolve_server_url(port: int) -> str:
    """Build the effective server URL from env var or port.

    Priority: BERNSTEIN_SERVER_URL env var > constructed from port.
    """
    return os.environ.get("BERNSTEIN_SERVER_URL", f"http://127.0.0.1:{port}")


def _resolve_bind_host() -> str:
    """Determine server bind host from env var.

    Priority: BERNSTEIN_BIND_HOST env var > default "127.0.0.1".
    """
    return os.environ.get("BERNSTEIN_BIND_HOST", "127.0.0.1")


def _resolve_auth_token() -> str | None:
    """Resolve the Bearer token used by bootstrap to talk to its own server.

    Precedence:
        1. Explicit ``BERNSTEIN_AUTH_TOKEN`` env var (operator-configured).
        2. Ephemeral auto-generated token when auth is enabled but no token is
           set and no opt-out is active. The generated token is written into
           ``os.environ`` so both the server subprocess (which inherits env in
           ``_start_server``) and the bootstrap client see the same value.
        3. ``None`` when ``BERNSTEIN_AUTH_DISABLED=1`` — the middleware
           short-circuits in that mode so no header is required.
    """
    existing = os.environ.get("BERNSTEIN_AUTH_TOKEN")
    if existing:
        return existing
    # Honour the explicit opt-out — the middleware will accept anonymous
    # requests, so we do not auto-generate a token that no one will check.
    if os.environ.get("BERNSTEIN_AUTH_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None
    import secrets

    token = secrets.token_urlsafe(32)
    os.environ["BERNSTEIN_AUTH_TOKEN"] = token
    logger.info(
        "Auto-generated BERNSTEIN_AUTH_TOKEN for this session (not persisted; "
        "set BERNSTEIN_AUTH_TOKEN to pin or BERNSTEIN_AUTH_DISABLED=1 to opt out).",
    )
    return token


def _detect_project_type(workdir: Path) -> str:
    """Detect project type from common config files.

    Args:
        workdir: Project root directory.

    Returns:
        One of: "python", "node", "go", "rust", "generic".
    """
    if (workdir / "pyproject.toml").exists() or (workdir / "setup.py").exists():
        return "python"
    if (workdir / "package.json").exists():
        return "node"
    if (workdir / "go.mod").exists():
        return "go"
    if (workdir / "Cargo.toml").exists():
        return "rust"
    return "generic"


def _constraints_for_project_type(project_type: str) -> list[str]:
    """Return default constraints for a detected project type.

    Args:
        project_type: One of the types returned by ``_detect_project_type``.

    Returns:
        List of constraint strings (empty for "generic").
    """
    mapping: dict[str, list[str]] = {
        "python": ["Python 3.12+", "pytest for tests", "ruff for linting"],
        "node": ["Node.js", "TypeScript preferred", "vitest or jest for tests"],
        "go": ["Go modules", "go test for tests"],
        "rust": ["Cargo for builds", "cargo test for tests"],
    }
    return mapping.get(project_type, [])


def auto_write_bernstein_yaml(workdir: Path) -> None:
    """Write a minimal bernstein.yaml with auto-routing to the project root.

    Called on first ``bernstein -g`` when no bernstein.yaml exists so users
    have a starting point they can customise later.  Detects project type
    automatically and includes appropriate constraints.

    Args:
        workdir: Project root directory.
    """
    from rich.console import Console

    from bernstein.core.agent_discovery import generate_auto_routing_yaml

    console = Console()
    routing_block = generate_auto_routing_yaml()
    if routing_block:
        # Extract just the "cli: auto" line + comment from the routing block
        cli_line = next((ln for ln in routing_block.splitlines() if ln.startswith("cli:")), "cli: auto")
    else:
        cli_line = "cli: auto"

    project_type = _detect_project_type(workdir)
    constraints = _constraints_for_project_type(project_type)

    lines = [
        "# Bernstein orchestration config — auto-generated",
        "# Uncomment 'goal' to run from this file: bernstein (without -g)",
        '# goal: "Describe what you want to build"',
        "",
        cli_line,
        "team: auto",
        'budget: "$10"',
    ]
    if constraints:
        lines.append("")
        lines.append("constraints:")
        for c in constraints:
            lines.append(f'  - "{c}"')
    lines.extend(
        [
            "",
            "visual:",
            "  splash: true",
            "  crt_effects: true",
            "  scanlines: false",
            "  splash_tier: auto",
        ]
    )
    lines.append("")

    (workdir / "bernstein.yaml").write_text("\n".join(lines))
    type_note = f" ({project_type})" if project_type != "generic" else ""
    console.print(f"[green]✓[/green] Created bernstein.yaml{type_note}")
