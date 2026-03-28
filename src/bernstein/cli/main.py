"""CLI entry point for Bernstein -- multi-agent orchestration."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click
import httpx
from rich.console import Console

if TYPE_CHECKING:
    from rich.table import Table
    from rich.text import Text

    from bernstein.eval.golden import Tier

from bernstein.cli.cost import cost_cmd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVER_URL = os.environ.get("BERNSTEIN_SERVER_URL", "http://localhost:8052")
SDD_DIRS = [
    ".sdd",
    ".sdd/backlog",
    ".sdd/backlog/open",
    ".sdd/backlog/done",
    ".sdd/agents",
    ".sdd/runtime",
    ".sdd/docs",
    ".sdd/decisions",
]
SDD_PID_SERVER = ".sdd/runtime/server.pid"
SDD_PID_SPAWNER = ".sdd/runtime/spawner.pid"
SDD_PID_WATCHDOG = ".sdd/runtime/watchdog.pid"

BANNER = """\
╔══════════════════════════════════╗
║  🎼 Bernstein — Agent Orchestra  ║
╚══════════════════════════════════╝"""

# Task status → Rich color
STATUS_COLORS: dict[str, str] = {
    "open": "white",
    "claimed": "cyan",
    "in_progress": "yellow",
    "done": "green",
    "failed": "red",
    "blocked": "magenta",
    "cancelled": "red",
}

console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_banner() -> None:
    from rich.panel import Panel

    console.print(Panel(BANNER, border_style="blue", expand=False))


def _print_dry_run_table(workdir: Path) -> None:
    """Print a summary table of tasks that would be spawned in dry-run mode.

    Reads open backlog tasks directly from .sdd/backlog/open/ and renders
    a Rich table showing role, title, model, effort, priority, and scope.

    Args:
        workdir: Project root directory.
    """
    from rich.table import Table

    from bernstein.core.sync import BacklogTask, parse_backlog_file

    backlog_dir = workdir / ".sdd" / "backlog" / "open"
    tasks: list[BacklogTask] = []
    if backlog_dir.exists():
        for md_file in sorted(backlog_dir.glob("*.md")):
            bt = parse_backlog_file(md_file)
            if bt is not None:
                tasks.append(bt)

    console.print("\n[bold cyan][DRY RUN] Planned task spawns:[/bold cyan]")

    if not tasks:
        console.print("[dim]No open tasks found in backlog.[/dim]")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Role", style="cyan")
    table.add_column("Title")
    table.add_column("Priority", justify="center")
    table.add_column("Scope", justify="center")
    table.add_column("Complexity", justify="center")
    table.add_column("Model", style="dim", justify="center")
    table.add_column("Effort", style="dim", justify="center")

    for bt in sorted(tasks, key=lambda t: t.priority):
        table.add_row(
            bt.role,
            bt.title,
            str(bt.priority),
            bt.scope,
            bt.complexity,
            "auto",
            "auto",
        )

    console.print(table)
    console.print(f"\n[dim]Total: {len(tasks)} task(s) — no agents were spawned.[/dim]")


def _auth_headers() -> dict[str, str]:
    """Return Authorization header dict if BERNSTEIN_AUTH_TOKEN is set."""
    token = os.environ.get("BERNSTEIN_AUTH_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def _server_get(path: str) -> dict[str, Any] | None:
    """GET from the task server.  Returns None if server is unreachable."""
    try:
        resp = httpx.get(f"{SERVER_URL}{path}", timeout=5.0, headers=_auth_headers())
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    except httpx.ConnectError:
        return None
    except Exception as exc:
        from bernstein.cli.errors import server_error

        server_error(exc).print()
        return None


def _server_post(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST to the task server.  Returns None if server is unreachable."""
    try:
        resp = httpx.post(f"{SERVER_URL}{path}", json=payload, timeout=5.0, headers=_auth_headers())
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
    except httpx.ConnectError:
        return None
    except Exception as exc:
        from bernstein.cli.errors import server_error

        server_error(exc).print()
        return None


def _read_pid(path: str) -> int | None:
    p = Path(path)
    if p.exists():
        try:
            return int(p.read_text().strip())
        except ValueError:
            return None
    return None


def _write_pid(path: str, pid: int) -> None:  # type: ignore[reportUnusedFunction]
    Path(path).write_text(str(pid))


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_pid(path: str, label: str) -> None:
    pid = _read_pid(path)
    if pid is None:
        console.print(f"[dim]No PID file found for {label}.[/dim]")
        return
    if _is_alive(pid):
        try:
            # Kill the entire process group so child processes (pytest, uv,
            # agent subprocesses) don't survive and leak memory.
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                console.print(f"[green]Sent SIGTERM to {label} process group (PID {pid}, PGID {pgid}).[/green]")
            except (OSError, ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
                console.print(f"[green]Sent SIGTERM to {label} (PID {pid}).[/green]")
        except OSError as exc:
            console.print(f"[yellow]Could not terminate {label} (PID {pid}): {exc}[/yellow]")
    else:
        console.print(f"[dim]{label} (PID {pid}) was not running.[/dim]")
    Path(path).unlink(missing_ok=True)


def _kill_pid_hard(path: str, label: str) -> None:
    """Kill a process by PID file using SIGKILL (no grace period).

    Unlike :func:`_kill_pid` which sends SIGTERM, this sends SIGKILL to
    the entire process group for an immediate, non-catchable kill.

    Args:
        path: Path to the PID file.
        label: Human-readable label for log messages.
    """
    pid = _read_pid(path)
    if pid is None:
        return
    if _is_alive(pid):
        try:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            console.print(f"[red]Killed {label} (PID {pid}) with SIGKILL.[/red]")
        except OSError:
            pass
    Path(path).unlink(missing_ok=True)


def _write_shutdown_signals(reason: str = "User requested stop") -> list[str]:
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


def _return_claimed_to_open() -> int:
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
        closed_nums = {f.name.split("-")[0] for f in closed_dir.glob("*.md")}
    # Also check backlog/done/ which some codepaths use
    done_dir = Path(".sdd/backlog/done")
    if done_dir.exists():
        closed_nums |= {f.name.split("-")[0] for f in done_dir.glob("*.md")}

    count = 0
    for f in claimed_dir.glob("*.md"):
        num = f.name.split("-")[0]
        if num in closed_nums:
            f.unlink()  # already completed — remove duplicate
        else:
            f.rename(open_dir / f.name)
            count += 1
    return count


def _save_session_on_stop(workdir: Path) -> None:
    """Persist session state to disk so the next run can resume quickly.

    Queries the running task server for current task statuses and writes a
    proper ``session.json`` snapshot via the session module.  Falls back to
    a lightweight ``session_state.json`` diagnostic file if the server is
    unreachable.

    Args:
        workdir: Project root directory containing ``.sdd/``.
    """
    import contextlib as _cl

    # Try to save a rich session.json (used by bootstrap for fast resume)
    saved_proper = False
    with _cl.suppress(Exception):
        import httpx as _httpx

        from bernstein.core.session import SessionState, save_session

        resp = _httpx.get(f"{SERVER_URL}/tasks", timeout=3.0, headers=_auth_headers())
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
            "open_tasks": sum(1 for _ in (workdir / ".sdd" / "backlog" / "open").glob("*.md"))
            if (workdir / ".sdd" / "backlog" / "open").exists()
            else 0,
            "claimed_tasks": sum(1 for _ in (workdir / ".sdd" / "backlog" / "claimed").glob("*.md"))
            if (workdir / ".sdd" / "backlog" / "claimed").exists()
            else 0,
        }
        (runtime_dir / "session_state.json").write_text(json.dumps(fallback, indent=2))


def _recover_orphaned_claims() -> int:
    """On startup, return claimed tickets from dead sessions to open.

    Since we are starting a fresh run, any tickets still in
    ``backlog/claimed/`` are orphaned from a previous session and should
    be returned to ``backlog/open/`` so they can be picked up again.

    Returns:
        Number of tickets returned to open.
    """
    return _return_claimed_to_open()


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


_SEED_FILENAMES = ("bernstein.yaml", "bernstein.yml")


def _find_seed_file() -> Path | None:
    """Look for a bernstein.yaml in the current directory.

    Returns:
        Path to the seed file if found, None otherwise.
    """
    for name in _SEED_FILENAMES:
        p = Path(name)
        if p.is_file():
            return p
    return None


@click.group(invoke_without_command=True)
@click.version_option(package_name="bernstein")
@click.option("--goal", "-g", default=None, help="Inline goal (no seed file needed).")
@click.option("--evolve", "-e", is_flag=True, default=False, hidden=True, help="Continuous self-improvement mode.")
@click.option("--max-cycles", default=0, hidden=True, help="Stop after N evolve cycles (0=unlimited).")
@click.option("--budget", default=0.0, help="Cost cap in USD; stop spawning agents when reached (0=unlimited).")
@click.option("--interval", default=300, hidden=True, help="Seconds between evolve cycles (default 5min).")
@click.option(
    "--github", "github_sync", is_flag=True, default=False, hidden=True, help="Sync evolve proposals as GitHub Issues."
)
@click.option("--headless", is_flag=True, default=False, hidden=True, help="Run without dashboard (for overnight/CI).")
@click.option("--dry-run", is_flag=True, default=False, help="Preview task plan without spawning agents.")
@click.option("--yes", "-y", is_flag=True, default=False, hidden=True, help="Skip cost confirmation prompt.")
@click.option("--fresh", "force_fresh", is_flag=True, default=False, help="Ignore saved session; start from scratch.")
@click.option(
    "--approval",
    type=click.Choice(["auto", "review", "pr"]),
    default="auto",
    show_default=True,
    help="Approval gate: auto=merge immediately, review=pause for human review, pr=open GitHub PR.",
)
@click.option(
    "--merge",
    "merge_strategy",
    type=click.Choice(["pr", "direct"]),
    default="pr",
    show_default=True,
    help="Merge strategy: pr=create GitHub PR (default), direct=push directly to main branch.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    goal: str | None,
    evolve: bool,
    max_cycles: int,
    budget: float,
    interval: int,
    github_sync: bool,
    headless: bool,
    dry_run: bool,
    yes: bool,
    force_fresh: bool,
    approval: str,
    merge_strategy: str,
) -> None:
    """Bernstein — multi-agent orchestration for CLI coding agents.

    \b
    Usage:
      bernstein -g "Build auth with JWT"    Run with inline goal
      bernstein                             Run from bernstein.yaml
      bernstein status                      Check progress
      bernstein stop                        Stop everything
      bernstein doctor                      Run self-diagnostics
      bernstein recap                       Show post-run summary

    For full options: bernstein --help-all
    """
    if ctx.invoked_subcommand is not None:
        return

    _print_banner()

    seed_path = _find_seed_file()
    workdir = Path.cwd()
    port = 8052

    if dry_run:
        _print_dry_run_table(workdir)
        return

    # Recover orphaned claimed tickets from any prior crashed/stopped session
    recovered = _recover_orphaned_claims()
    if recovered:
        console.print(f"[yellow]Recovered {recovered} orphaned ticket(s) from a prior session.[/yellow]")

    # Evolve mode safety: require --budget or --max-cycles
    if evolve and budget <= 0 and max_cycles <= 0:
        from bernstein.cli.errors import BernsteinError

        BernsteinError(
            what="Evolve mode requires a safety limit",
            why="Evolve mode will autonomously modify code indefinitely",
            fix="Add --budget 5.00 or --max-cycles 10",
        ).print()
        raise SystemExit(1)

    # Evolve mode confirmation: show budget/cycles and require explicit approval
    if evolve and not yes:
        budget_str = f"${budget:.2f}" if budget > 0 else "unlimited"
        cycles_str = str(max_cycles) if max_cycles > 0 else "unlimited"
        console.print(
            f"[bold yellow]Evolve mode[/bold yellow] will autonomously modify code.\n"
            f"  Budget: [bold]{budget_str}[/bold], max cycles: [bold]{cycles_str}[/bold].\n"
            f"  Press [bold]Enter[/bold] to continue or Ctrl+C to cancel."
        )
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Aborted.[/yellow]")
            raise SystemExit(0) from None

    # Check if already running
    server_pid_path = Path(SDD_PID_SERVER)
    server_pid = _read_pid(str(server_pid_path))
    already_running = server_pid is not None and _is_alive(server_pid)

    if not already_running:
        # Write run_config.json so the orchestrator subprocess can read budget_usd, approval,
        # merge_strategy, and other per-run settings.
        if budget > 0 or approval != "auto" or merge_strategy != "pr":
            import json as _json

            runtime_dir = workdir / ".sdd" / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            run_cfg: dict[str, Any] = {}
            if budget > 0:
                run_cfg["budget_usd"] = budget
            if approval != "auto":
                run_cfg["approval"] = approval
            run_cfg["merge_strategy"] = merge_strategy
            (runtime_dir / "run_config.json").write_text(_json.dumps(run_cfg))

        if goal is not None:
            # Inline goal — no config files needed
            if not yes:
                console.print(
                    "[bold yellow]Cost estimate:[/bold yellow] ~$0.10-$1.00 per task with Sonnet. "
                    "Press [bold]Enter[/bold] to continue or Ctrl+C to cancel."
                )
                try:
                    input()
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[yellow]Aborted.[/yellow]")
                    raise SystemExit(0) from None
            from bernstein.core.bootstrap import bootstrap_from_goal

            try:
                bootstrap_from_goal(goal, workdir=workdir, port=port, force_fresh=force_fresh)
            except RuntimeError as exc:
                from bernstein.cli.errors import bootstrap_failed

                bootstrap_failed(exc).print()
                raise SystemExit(1) from exc
        elif seed_path is not None:
            console.print(f"Using: [bold]{seed_path.name}[/bold]")
            from bernstein.core.bootstrap import bootstrap_from_seed
            from bernstein.core.seed import SeedError

            # Cost estimate before spawning agents
            if not yes:
                from bernstein.core.cost import estimate_run_cost

                backlog_dir_est = workdir / ".sdd" / "backlog" / "open"
                est_count = sum(1 for _ in backlog_dir_est.glob("*.md")) if backlog_dir_est.exists() else 1
                low, high = estimate_run_cost(max(est_count, 1))
                console.print(
                    f"[bold yellow]Cost estimate:[/bold yellow] ~${low:.2f}-${high:.2f} "
                    f"for {est_count} task(s). "
                    "Press [bold]Enter[/bold] to continue or Ctrl+C to cancel."
                )
                try:
                    input()
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[yellow]Aborted.[/yellow]")
                    raise SystemExit(0) from None

            try:
                bootstrap_from_seed(seed_path, workdir=workdir, port=port, force_fresh=force_fresh)
            except SeedError as exc:
                from bernstein.cli.errors import seed_parse_error

                seed_parse_error(exc).print()
                raise SystemExit(1) from exc
            except RuntimeError as exc:
                from bernstein.cli.errors import bootstrap_failed

                bootstrap_failed(exc).print()
                raise SystemExit(1) from exc
        else:
            # No seed file, no goal — check if backlog has tasks
            backlog_dir = workdir / ".sdd" / "backlog" / "open"
            has_backlog = backlog_dir.exists() and any(backlog_dir.glob("*.md"))
            if has_backlog:
                task_count = sum(1 for _ in backlog_dir.glob("*.md"))
                console.print(f"[dim]No seed file — loading {task_count} tasks from backlog[/dim]")

                # Cost estimate before spawning agents
                if not yes:
                    from bernstein.core.cost import estimate_run_cost as _est

                    low, high = _est(task_count)
                    console.print(
                        f"[bold yellow]Cost estimate:[/bold yellow] ~${low:.2f}-${high:.2f} "
                        f"for {task_count} task(s). "
                        "Press [bold]Enter[/bold] to continue or Ctrl+C to cancel."
                    )
                    try:
                        input()
                    except (KeyboardInterrupt, EOFError):
                        console.print("\n[yellow]Aborted.[/yellow]")
                        raise SystemExit(0) from None

                from bernstein.core.bootstrap import bootstrap_from_goal

                try:
                    bootstrap_from_goal("Execute backlog tasks", workdir=workdir, port=port, force_fresh=force_fresh)
                except RuntimeError as exc:
                    from bernstein.cli.errors import bootstrap_failed

                    bootstrap_failed(exc).print()
                    raise SystemExit(1) from exc
            else:
                console.print(
                    "No bernstein.yaml or backlog tasks found.\n\n"
                    "[bold]Quick start:[/bold]\n"
                    '  bernstein -g "Build a REST API with auth"\n\n'
                    "Or create a bernstein.yaml / add .md tasks to .sdd/backlog/open/\n"
                )
                return
    else:
        console.print("[dim]Already running.[/dim]")

    # Write evolve config so the orchestrator can read it
    if evolve:
        import json as _json

        evolve_config = {
            "enabled": True,
            "max_cycles": max_cycles,
            "budget_usd": budget,
            "interval_s": interval,
            "github_sync": github_sync,
        }
        evolve_path = workdir / ".sdd" / "runtime" / "evolve.json"
        evolve_path.parent.mkdir(parents=True, exist_ok=True)
        evolve_path.write_text(_json.dumps(evolve_config))
        console.print(
            f"[bold cyan]Evolve mode ON[/bold cyan] "
            f"(interval={interval}s"
            f"{f', max_cycles={max_cycles}' if max_cycles else ''}"
            f"{f', budget=${budget:.2f}' if budget else ''}"
            f"{', github-sync=on' if github_sync else ''})"
        )

    if headless:
        console.print("[bold green]Running headless.[/bold green] Check .sdd/runtime/ for logs.")
        return

    # Register Ctrl+C handler so we save state before the dashboard exits
    _register_sigint_handler()

    # Show live dashboard (blocks until Ctrl+C / q)
    from bernstein.cli.dashboard import run_dashboard

    run_dashboard()


def _sigint_handler(signum: int, frame: Any) -> None:
    """Handle Ctrl+C: save state, return claimed tickets, then exit.

    This handler is installed while the dashboard is running so that an
    interactive Ctrl+C still persists session state and avoids orphaning
    claimed tickets.

    Args:
        signum: Signal number (always ``SIGINT``).
        frame: Current stack frame (unused).
    """
    import contextlib

    console.print("\n[yellow]Ctrl+C received — saving state…[/yellow]")
    with contextlib.suppress(OSError):
        _save_session_on_stop(Path.cwd())
    moved = _return_claimed_to_open()
    if moved:
        console.print(f"[dim]Returned {moved} claimed ticket(s) to open.[/dim]")
    console.print("[yellow]Use 'bernstein stop' for graceful shutdown.[/yellow]")
    raise SystemExit(130)


def _register_sigint_handler() -> None:
    """Install :func:`_sigint_handler` for ``SIGINT``."""
    signal.signal(signal.SIGINT, _sigint_handler)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def _detect_project_type(root: Path) -> str:
    """Auto-detect project type by checking for common config files.

    Args:
        root: Project root directory.

    Returns:
        Detected project type string (e.g. "python", "node", "go", "generic").
    """
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        return "python"
    if (root / "package.json").exists():
        return "node"
    if (root / "go.mod").exists():
        return "go"
    if (root / "Cargo.toml").exists():
        return "rust"
    return "generic"


def _default_constraints_for(project_type: str) -> list[str]:
    """Return sensible default constraints for a detected project type.

    Args:
        project_type: One of the types returned by ``_detect_project_type``.

    Returns:
        List of constraint strings.
    """
    mapping: dict[str, list[str]] = {
        "python": ["Python 3.12+", "pytest for tests", "ruff for linting"],
        "node": ["Node.js", "TypeScript preferred", "vitest or jest for tests"],
        "go": ["Go modules", "go test for tests"],
        "rust": ["Cargo for builds", "cargo test for tests"],
    }
    return mapping.get(project_type, [])


def _generate_default_yaml(project_type: str) -> str:
    """Generate a default bernstein.yaml with project-aware defaults.

    Args:
        project_type: Detected project type.

    Returns:
        YAML content string.
    """
    lines = [
        "# Bernstein orchestration config",
        "# Uncomment and edit the goal, then run: bernstein",
        "",
        '# goal: "Describe what you want the agents to build or improve"',
        "",
        "cli: claude  # or codex, gemini, qwen",
        "team: auto",
        'budget: "$10"',
    ]
    constraints = _default_constraints_for(project_type)
    if constraints:
        lines.append("")
        lines.append("constraints:")
        for c in constraints:
            lines.append(f'  - "{c}"')
    lines.append("")
    return "\n".join(lines)


@cli.command("overture", hidden=True)
@click.option(
    "--dir",
    "target_dir",
    default=".",
    show_default=True,
    help="Directory to initialise (default: current directory).",
)
def init(target_dir: str) -> None:
    """Init workspace — create .sdd/ structure."""
    _print_banner()
    root = Path(target_dir).resolve()
    console.print(f"Initialising Bernstein workspace in [bold]{root}[/bold]")

    # Auto-detect project type
    project_type = _detect_project_type(root)
    if project_type != "generic":
        console.print(f"[cyan]Detected[/cyan] {project_type} project")

    for d in SDD_DIRS:
        p = root / d
        p.mkdir(parents=True, exist_ok=True)

    # Write a minimal default config
    config_path = root / ".sdd" / "config.yaml"
    if not config_path.exists():
        config_path.write_text(
            "# Bernstein workspace config\n"
            "server_port: 8052\n"
            "max_workers: 6\n"
            "default_model: sonnet\n"
            "default_effort: high\n"
        )
        console.print(f"[green]Created[/green] {config_path.relative_to(root)}")

    # Write a .gitignore for the runtime dir
    gi_path = root / ".sdd" / "runtime" / ".gitignore"
    if not gi_path.exists():
        gi_path.write_text("*.pid\n*.log\n")

    # Create bernstein.yaml in project root if not present
    yaml_path = root / "bernstein.yaml"
    if not yaml_path.exists():
        yaml_path.write_text(_generate_default_yaml(project_type))
        console.print(f"[green]Created[/green] {yaml_path.relative_to(root)}")

    # Copy bundled default templates if the project doesn't have its own
    templates_dst = root / "templates"
    if not templates_dst.exists():
        import shutil

        from bernstein import _BUNDLED_TEMPLATES_DIR  # type: ignore[reportPrivateUsage]

        if _BUNDLED_TEMPLATES_DIR.is_dir():
            shutil.copytree(_BUNDLED_TEMPLATES_DIR, templates_dst)
            console.print("[green]Created[/green] templates/ (default roles & prompts)")

    # Append .sdd/runtime/ to root .gitignore if not already present
    root_gi_path = root / ".gitignore"
    gitignore_entry = ".sdd/runtime/"
    if root_gi_path.exists():
        existing = root_gi_path.read_text()
        if gitignore_entry not in existing:
            root_gi_path.write_text(existing.rstrip("\n") + f"\n{gitignore_entry}\n")
            console.print(f"[green]Updated[/green] .gitignore (added {gitignore_entry})")
    else:
        root_gi_path.write_text(f"{gitignore_entry}\n")
        console.print(f"[green]Created[/green] .gitignore (added {gitignore_entry})")

    # Print clear next steps
    console.print("")
    console.print("[green]Done.[/green] Next steps:")
    console.print("  1. Edit [bold]bernstein.yaml[/bold] — set a goal")
    console.print("  2. Run [bold]bernstein[/bold] to start the orchestra")
    console.print("")
    console.print(
        "  See [link=https://chernistry.github.io/bernstein/]docs[/link] "
        "or [bold]examples/quickstart/[/bold] for a working example."
    )


# ---------------------------------------------------------------------------
# run  (the "one command" Seed UX)
# ---------------------------------------------------------------------------


@cli.command("conduct", hidden=True)
@click.option(
    "--goal",
    default=None,
    help="Inline goal (skips bernstein.yaml).",
)
@click.option(
    "--seed",
    "seed_file",
    default=None,
    help="Path to a custom seed YAML file (default: bernstein.yaml).",
)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    help="Port for the task server.",
)
@click.option(
    "--cells",
    default=1,
    show_default=True,
    help="Number of parallel orchestration cells (1 = single-cell, >1 = MultiCellOrchestrator).",
)
@click.option(
    "--remote",
    is_flag=True,
    default=False,
    help="Bind server to 0.0.0.0 for remote/cluster access (default: 127.0.0.1).",
)
def run(goal: str | None, seed_file: str | None, port: int, cells: int, remote: bool) -> None:
    """Parse seed, init workspace, start server, launch agents.

    \b
      bernstein conduct                     # reads bernstein.yaml
      bernstein conduct --goal "Build X"    # inline goal
      bernstein conduct --seed custom.yaml  # custom seed file
      bernstein conduct --cells 3           # 3 parallel cells (multi-cell mode)
      bernstein conduct --remote            # bind to 0.0.0.0 for cluster access
    """
    _print_banner()

    # Set process title so orchestrator is visible in Activity Monitor / ps
    try:
        import setproctitle

        setproctitle.setproctitle("bernstein: orchestrator")
    except ImportError:
        pass

    from bernstein.core.bootstrap import bootstrap_from_goal, bootstrap_from_seed
    from bernstein.core.seed import SeedError

    workdir = Path.cwd()

    if goal is not None:
        # Inline goal mode -- no YAML needed
        try:
            bootstrap_from_goal(goal=goal, workdir=workdir, port=port, cells=cells)
        except RuntimeError as exc:
            from bernstein.cli.errors import bootstrap_failed

            bootstrap_failed(exc).print()
            raise SystemExit(1) from exc
        return

    # Seed file mode
    if seed_file is not None:
        path = Path(seed_file)
    else:
        found = _find_seed_file()
        if found is not None:
            path = found
        else:
            from bernstein.cli.errors import no_seed_or_goal

            no_seed_or_goal().print()
            raise SystemExit(1)

    console.print(f"[dim]Using seed file:[/dim] {path}")
    try:
        # CLI --cells overrides seed file value when explicitly set (cells > 1)
        cli_cells: int | None = cells if cells > 1 else None
        bootstrap_from_seed(seed_path=path, workdir=workdir, port=port, cells=cli_cells, remote=remote)
    except SeedError as exc:
        from bernstein.cli.errors import seed_parse_error

        seed_parse_error(exc).print()
        raise SystemExit(1) from exc
    except RuntimeError as exc:
        from bernstein.cli.errors import bootstrap_failed

        bootstrap_failed(exc).print()
        raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# start  (legacy, kept for backward compat)
# ---------------------------------------------------------------------------


@cli.command("downbeat", hidden=True)
@click.argument("goal", required=False, default=None)
@click.option(
    "--seed-file",
    default="bernstein.yaml",
    show_default=True,
    help="YAML seed file to read goal/tasks from (used when GOAL is not given).",
)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    help="Port for the task server.",
)
def start(goal: str | None, seed_file: str, port: int) -> None:
    """Start server and spawn manager (legacy, use 'conduct')."""
    _print_banner()

    try:
        import setproctitle

        setproctitle.setproctitle("bernstein: orchestrator")
    except ImportError:
        pass

    from bernstein.core.bootstrap import bootstrap_from_goal, bootstrap_from_seed
    from bernstein.core.seed import SeedError

    workdir = Path.cwd()

    if goal:
        try:
            bootstrap_from_goal(goal=goal, workdir=workdir, port=port)
        except RuntimeError as exc:
            from bernstein.cli.errors import bootstrap_failed

            bootstrap_failed(exc).print()
            raise SystemExit(1) from exc
    else:
        path = Path(seed_file)
        if not path.exists():
            from bernstein.cli.errors import no_seed_or_goal

            no_seed_or_goal().print()
            raise SystemExit(1)
        try:
            bootstrap_from_seed(seed_path=path, workdir=workdir, port=port)
        except SeedError as exc:
            from bernstein.cli.errors import seed_parse_error

            seed_parse_error(exc).print()
            raise SystemExit(1) from exc
        except RuntimeError as exc:
            from bernstein.cli.errors import bootstrap_failed

            bootstrap_failed(exc).print()
            raise SystemExit(1) from exc


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command("score", hidden=True)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def status(as_json: bool) -> None:
    """Task summary, active agents, cost estimate.

    \b
      bernstein status          # Rich table output
      bernstein status --json   # machine-readable JSON
    """
    data = _server_get("/status")
    if data is None:
        if as_json:
            click.echo(json.dumps({"error": "Cannot reach task server"}))
        else:
            console.print(
                "[red]Cannot reach task server.[/red] Is Bernstein running? Run [bold]bernstein[/bold] to start."
            )
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps(data, indent=2))
        return

    _print_banner()

    # ---- Task table ----
    from rich.table import Table

    tasks: list[dict[str, Any]] = data.get("tasks", [])
    task_table = Table(title="Tasks", show_lines=False, header_style="bold cyan")
    task_table.add_column("ID", style="dim", min_width=10)
    task_table.add_column("Title", min_width=30)
    task_table.add_column("Role", min_width=10)
    task_table.add_column("Status", min_width=14)
    task_table.add_column("Priority", justify="right")
    task_table.add_column("Agent", min_width=12)

    for t in tasks:
        raw_status = t.get("status", "open")
        color = STATUS_COLORS.get(raw_status, "white")
        task_table.add_row(
            t.get("id", "—"),
            t.get("title", "—"),
            t.get("role", "—"),
            f"[{color}]{raw_status}[/{color}]",
            str(t.get("priority", 2)),
            t.get("assigned_agent") or "[dim]—[/dim]",
        )

    console.print(task_table)

    # ---- Agent table ----
    agents: list[dict[str, Any]] = data.get("agents", [])
    if agents:
        agent_table = Table(title="Active Agents", show_lines=False, header_style="bold cyan")
        agent_table.add_column("ID", style="dim", min_width=12)
        agent_table.add_column("Role", min_width=10)
        agent_table.add_column("Status", min_width=10)
        agent_table.add_column("Model", min_width=10)
        agent_table.add_column("Tasks")

        for a in agents:
            raw_astatus = a.get("status", "idle")
            acolor = "yellow" if raw_astatus == "working" else "dim"
            agent_table.add_row(
                a.get("id", "—"),
                a.get("role", "—"),
                f"[{acolor}]{raw_astatus}[/{acolor}]",
                a.get("model", "—"),
                str(len(a.get("task_ids", []))),
            )
        console.print(agent_table)
    else:
        console.print("[dim]No active agents.[/dim]")

    # ---- Summary stats ----
    summary: dict[str, Any] = data.get("summary", {})
    total = summary.get("total", len(tasks))
    done = summary.get("done", sum(1 for t in tasks if t.get("status") == "done"))
    in_prog = summary.get("in_progress", sum(1 for t in tasks if t.get("status") == "in_progress"))
    failed = summary.get("failed", sum(1 for t in tasks if t.get("status") == "failed"))

    console.print(
        f"\n[bold]Tasks:[/bold] {total} total  "
        f"[green]{done} done[/green]  "
        f"[yellow]{in_prog} in progress[/yellow]  "
        f"[red]{failed} failed[/red]"
    )

    elapsed_s: int | None = data.get("elapsed_seconds")
    if elapsed_s is not None:
        minutes, secs = divmod(elapsed_s, 60)
        console.print(f"[bold]Elapsed:[/bold] {minutes}m {secs}s")

    # ---- Cost section ----
    total_cost_usd: float = data.get("total_cost_usd", 0.0)
    per_role: list[dict[str, Any]] = data.get("per_role", [])
    roles_with_cost = [r for r in per_role if r.get("cost_usd", 0.0) > 0.0]
    if total_cost_usd > 0.0 or roles_with_cost:
        console.print(f"\n[bold]Total spend:[/bold] [green]${total_cost_usd:.4f}[/green]")
        if roles_with_cost:
            cost_table = Table(title="Cost by Role", show_lines=False, header_style="bold cyan")
            cost_table.add_column("Role", min_width=12)
            cost_table.add_column("Tasks", justify="right")
            cost_table.add_column("Cost", justify="right")
            for r in sorted(roles_with_cost, key=lambda x: x.get("cost_usd", 0.0), reverse=True):
                role_tasks = r.get("done", 0) + r.get("failed", 0) + r.get("claimed", 0) + r.get("open", 0)
                cost_table.add_row(
                    r.get("role", "—"),
                    str(role_tasks),
                    f"${r.get('cost_usd', 0.0):.4f}",
                )
            console.print(cost_table)

    # ---- Cluster section (only shown when nodes are registered) ----
    cluster = _server_get("/cluster/status")
    if cluster and cluster.get("total_nodes", 0) > 0:
        node_table = Table(title="Cluster Nodes", show_lines=False, header_style="bold cyan")
        node_table.add_column("ID", style="dim", min_width=12)
        node_table.add_column("Name", min_width=12)
        node_table.add_column("Status", min_width=10)
        node_table.add_column("Slots", justify="right")
        node_table.add_column("Active", justify="right")
        node_table.add_column("URL", min_width=20)

        for n in cluster.get("nodes", []):
            raw_nstatus = n.get("status", "offline")
            ncolor = "green" if raw_nstatus == "online" else ("yellow" if raw_nstatus == "degraded" else "red")
            cap = n.get("capacity", {})
            node_table.add_row(
                n.get("id", "—")[:12],
                n.get("name", "—"),
                f"[{ncolor}]{raw_nstatus}[/{ncolor}]",
                str(cap.get("available_slots", "—")),
                str(cap.get("active_agents", "—")),
                n.get("url", "—") or "[dim]—[/dim]",
            )

        console.print(node_table)
        console.print(
            f"[bold]Cluster:[/bold] {cluster.get('topology', '?')}  "
            f"[green]{cluster.get('online_nodes', 0)} online[/green]  "
            f"{cluster.get('offline_nodes', 0)} offline  "
            f"[bold]{cluster.get('available_slots', 0)} slots available[/bold]"
        )


# ---------------------------------------------------------------------------
# add-task
# ---------------------------------------------------------------------------


@cli.command("compose", hidden=True)
@click.argument("title")
@click.option("--role", default="backend", show_default=True, help="Agent role for this task.")
@click.option("--description", "-d", default="", help="Task description.")
@click.option(
    "--priority",
    type=click.IntRange(1, 3),
    default=2,
    show_default=True,
    help="1=critical, 2=normal, 3=nice-to-have.",
)
@click.option(
    "--scope",
    type=click.Choice(["small", "medium", "large"]),
    default="medium",
    show_default=True,
)
@click.option(
    "--complexity",
    type=click.Choice(["low", "medium", "high"]),
    default="medium",
    show_default=True,
)
@click.option("--depends-on", multiple=True, metavar="TASK_ID", help="Task IDs this depends on.")
def add_task(
    title: str,
    role: str,
    description: str,
    priority: int,
    scope: str,
    complexity: str,
    depends_on: tuple[str, ...],
) -> None:
    """Add a task to the running server.

    TITLE is the short task name.
    """
    payload: dict[str, Any] = {
        "title": title,
        "role": role,
        "description": description,
        "priority": priority,
        "scope": scope,
        "complexity": complexity,
        "depends_on": list(depends_on),
    }

    result = _server_post("/task", payload)
    if result is None:
        console.print("[red]Cannot reach task server.[/red] Is Bernstein running? Run [bold]bernstein[/bold] to start.")
        raise SystemExit(1)

    task_id = result.get("id", "?")
    console.print(
        f"[green]Task added:[/green] [bold]{task_id}[/bold] — {title} ([dim]role={role}, priority={priority}[/dim])"
    )


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@cli.command("sync", hidden=True)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    help="Task server port.",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def sync(port: int, workdir: str) -> None:
    """Sync .sdd/backlog/open/*.md files with the task server.

    \b
    Creates server tasks for new backlog files not yet on the server.
    Moves .md files to backlog/done/ when their task is completed.
    """
    from bernstein.core.sync import sync_backlog_to_server

    root = Path(workdir).resolve()
    result = sync_backlog_to_server(root, server_url=f"http://127.0.0.1:{port}")

    if result.created:
        console.print(f"[green]Created {len(result.created)} task(s):[/green] " + ", ".join(result.created))
    if result.skipped:
        console.print(f"[dim]Skipped {len(result.skipped)} file(s) already on server[/dim]")
    if result.moved:
        console.print(
            f"[green]Moved {len(result.moved)} completed file(s) to backlog/done/:[/green] " + ", ".join(result.moved)
        )
    for err in result.errors:
        console.print(f"[red]Error:[/red] {err}")

    if not result.created and not result.moved and not result.errors:
        if result.skipped:
            console.print("[dim]All backlog files already synced.[/dim]")
        else:
            console.print("[dim]Nothing to sync — backlog/open/ is empty.[/dim]")


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@cli.command("stop")
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
    _print_banner()

    if force:
        console.print("[bold red]Hard stop — killing everything immediately…[/bold red]\n")
        _hard_stop()
    else:
        console.print("[bold]Soft stop — giving agents time to save…[/bold]\n")
        _soft_stop(timeout)


def _soft_stop(timeout: int) -> None:
    """Soft stop: signal agents, wait, save state, return tickets, kill.

    Args:
        timeout: Maximum seconds to wait for agents to exit gracefully.
    """
    # 1. Write SHUTDOWN signal files for all active agents
    signaled = _write_shutdown_signals(reason="User requested stop")
    if signaled:
        console.print(f"[dim]Wrote SHUTDOWN signals for {len(signaled)} agent(s).[/dim]")

    # 2. Ask the server to initiate graceful shutdown
    data = _server_post("/shutdown", {})
    if data is not None:
        console.print("[dim]Shutdown signal sent to task server.[/dim]")
    else:
        console.print("[dim]Task server not reachable — skipping server shutdown.[/dim]")

    # 3. Wait for agents to wind down
    if timeout > 0:
        console.print(f"[dim]Waiting up to {timeout}s for agents…[/dim]")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status_data = _server_get("/status")
            if status_data is None:
                break
            active = [a for a in status_data.get("agents", []) if a.get("status") in {"working", "starting"}]
            if not active:
                break
            time.sleep(1)

    # 4. Save session state
    _save_session_on_stop(Path.cwd())
    console.print("[dim]Session state saved.[/dim]")

    # 5. Return claimed tickets to open
    moved = _return_claimed_to_open()
    if moved:
        console.print(f"[dim]Returned {moved} claimed ticket(s) to open.[/dim]")

    # 6. Kill watchdog first so it doesn't restart things we're stopping
    _kill_pid(SDD_PID_WATCHDOG, "Watchdog")

    # 7. Kill spawner
    _kill_pid(SDD_PID_SPAWNER, "Spawner")

    # 8. Kill all spawned agents (they run in separate process groups)
    agents_json = Path(".sdd/runtime/agents.json")
    if agents_json.exists():
        try:
            agent_data = json.loads(agents_json.read_text())
            for agent in agent_data.get("agents", []):
                pid = agent.get("pid")
                if pid and _is_alive(pid):
                    try:
                        os.killpg(os.getpgid(pid), signal.SIGTERM)
                        console.print(f"[dim]Sent SIGTERM to agent {agent.get('id', '?')} (PID {pid})[/dim]")
                    except OSError:
                        pass
        except (OSError, ValueError):
            pass

    # 9. Kill server
    _kill_pid(SDD_PID_SERVER, "Task server")

    console.print("\n[green]Bernstein stopped (soft).[/green]")


def _hard_stop() -> None:
    """Hard stop: SIGKILL everything, best-effort save, return tickets."""
    # 1. Kill watchdog immediately
    _kill_pid_hard(SDD_PID_WATCHDOG, "Watchdog")

    # 2. Kill spawner immediately
    _kill_pid_hard(SDD_PID_SPAWNER, "Spawner")

    # 3. Kill all spawned agents with SIGKILL
    agents_json = Path(".sdd/runtime/agents.json")
    if agents_json.exists():
        try:
            agent_data = json.loads(agents_json.read_text())
            for agent in agent_data.get("agents", []):
                pid = agent.get("pid")
                if pid and _is_alive(pid):
                    import contextlib

                    try:
                        pgid = os.getpgid(pid)
                        os.killpg(pgid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        with contextlib.suppress(OSError):
                            os.kill(pid, signal.SIGKILL)
                    console.print(f"[red]Killed agent {agent.get('id', '?')} (PID {pid}) with SIGKILL[/red]")
        except (OSError, ValueError):
            pass

    # 4. Kill server immediately
    _kill_pid_hard(SDD_PID_SERVER, "Task server")

    # 5. Best-effort session save
    try:
        _save_session_on_stop(Path.cwd())
        console.print("[dim]Session state saved (best-effort).[/dim]")
    except OSError:
        console.print("[yellow]Could not save session state.[/yellow]")

    # 6. Return claimed tickets to open
    try:
        moved = _return_claimed_to_open()
        if moved:
            console.print(f"[dim]Returned {moved} claimed ticket(s) to open.[/dim]")
    except OSError:
        console.print("[yellow]Could not return claimed tickets.[/yellow]")

    console.print("\n[red]Bernstein stopped (hard).[/red]")


# ---------------------------------------------------------------------------
# ps — process visibility
# ---------------------------------------------------------------------------


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@cli.command("ps")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON instead of table.")
@click.option("--pid-dir", default=".sdd/runtime/pids", help="PID metadata directory.")
def ps_cmd(as_json: bool, pid_dir: str) -> None:
    """Show running Bernstein agent processes."""
    from rich.table import Table

    pid_path = Path(pid_dir)
    if not pid_path.exists():
        if as_json:
            console.print("[]")
        else:
            console.print("[dim]No agent processes found.[/dim]")
        return

    agents: list[dict[str, Any]] = []
    stale_files: list[Path] = []

    for pid_file in sorted(pid_path.glob("*.json")):
        try:
            info = json.loads(pid_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        worker_pid = info.get("worker_pid", 0)
        child_pid = info.get("child_pid")
        alive = _is_process_alive(worker_pid) if worker_pid else False

        if not alive:
            stale_files.append(pid_file)
            continue

        started_at = info.get("started_at", 0)
        runtime_s = time.time() - started_at if started_at else 0
        minutes, secs = divmod(int(runtime_s), 60)
        hours, minutes = divmod(minutes, 60)
        runtime_str = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s"

        agents.append(
            {
                "session": info.get("session", "?"),
                "role": info.get("role", "?"),
                "command": info.get("command", "?"),
                "model": info.get("model", "?"),
                "worker_pid": worker_pid,
                "child_pid": child_pid,
                "runtime": runtime_str,
                "started_at": started_at,
            }
        )

    # Clean up stale PID files
    for f in stale_files:
        f.unlink(missing_ok=True)

    if as_json:
        console.print(json.dumps(agents, indent=2))
        return

    if not agents:
        console.print("[dim]No running agents.[/dim]")
        return

    table = Table(title="Bernstein Agents", show_lines=False, header_style="bold cyan")
    table.add_column("Session", style="dim", min_width=18)
    table.add_column("Role", min_width=10)
    table.add_column("CLI", min_width=8)
    table.add_column("Model", min_width=16)
    table.add_column("Worker PID", justify="right")
    table.add_column("Agent PID", justify="right")
    table.add_column("Runtime", justify="right")

    for a in agents:
        table.add_row(
            a["session"],
            f"[bold]{a['role']}[/bold]",
            a["command"],
            a["model"],
            str(a["worker_pid"]),
            str(a["child_pid"] or "—"),
            a["runtime"],
        )

    console.print(table)
    console.print(f"\n[dim]{len(agents)} agent(s) running[/dim]")


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------

_DEMO_PORT = 8055

_ADAPTER_COMMANDS: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "gemini": "gemini",
    "qwen": "qwen",
}

_DEMO_TASKS: list[dict[str, str]] = [
    {
        "filename": "1-health-check.md",
        "content": (
            "# Add health check endpoint\n\n"
            "**Role:** backend\n"
            "**Priority:** 1\n"
            "**Scope:** small\n"
            "**Complexity:** low\n\n"
            "Add a `/health` endpoint to `app.py` that returns "
            '`{"status": "healthy", "version": "1.0.0"}` with HTTP 200.\n'
        ),
    },
    {
        "filename": "2-add-tests.md",
        "content": (
            "# Add tests for app.py\n\n"
            "**Role:** qa\n"
            "**Priority:** 2\n"
            "**Scope:** small\n"
            "**Complexity:** low\n\n"
            "Add pytest tests in `tests/test_app.py` covering all routes in "
            "`app.py`, including the `/health` endpoint.\n"
        ),
    },
    {
        "filename": "3-error-handling.md",
        "content": (
            "# Add error handling middleware\n\n"
            "**Role:** backend\n"
            "**Priority:** 2\n"
            "**Scope:** small\n"
            "**Complexity:** low\n\n"
            "Add 404 and 500 JSON error handlers to `app.py`. "
            'Return `{"error": "Not found", "status": 404}` for missing routes.\n'
        ),
    },
]


def _detect_available_adapter() -> str | None:
    """Return the name of the first available CLI adapter found in PATH.

    Returns:
        Adapter name (e.g. ``'claude'``) or None if none found.
    """
    import shutil as _shutil

    for name, cmd in _ADAPTER_COMMANDS.items():
        if _shutil.which(cmd) is not None:
            return name
    return None


def _setup_demo_project(project_dir: Path, adapter: str) -> None:
    """Copy demo template files and seed three backlog tasks.

    Args:
        project_dir: Destination directory (should be empty / temp dir).
        adapter: CLI adapter name — written into the workspace config.
    """
    import shutil as _shutil

    # Copy template files from templates/demo/
    template_dir = Path(__file__).parent.parent.parent.parent / "templates" / "demo"
    if template_dir.exists():
        _shutil.copytree(str(template_dir), str(project_dir), dirs_exist_ok=True)
    else:
        # Fallback: write minimal files inline so the command works even without
        # the templates/ directory being present on PYTHONPATH.
        (project_dir / "app.py").write_text(
            '"""Simple Flask web application."""\n'
            "from flask import Flask, jsonify\n\n"
            "app = Flask(__name__)\n\n\n"
            '@app.route("/")\n'
            "def hello() -> object:\n"
            '    """Return a greeting."""\n'
            '    return jsonify({"message": "Hello, World!", "status": "ok"})\n\n\n'
            'if __name__ == "__main__":\n'
            "    app.run(debug=True)\n"
        )
        (project_dir / "requirements.txt").write_text("flask>=3.0.0\npytest>=8.0.0\n")
        tests_dir = project_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "__init__.py").write_text("")
        (tests_dir / "test_app.py").write_text(
            '"""Basic tests."""\nimport pytest\nfrom app import app\n\n\n'
            "@pytest.fixture\ndef client():\n"
            '    app.config["TESTING"] = True\n'
            "    with app.test_client() as c:\n        yield c\n\n\n"
            "def test_hello(client):\n"
            '    resp = client.get("/")\n'
            "    assert resp.status_code == 200\n"
        )

    # Create .sdd/ structure
    for d in SDD_DIRS:
        (project_dir / d).mkdir(parents=True, exist_ok=True)

    config_path = project_dir / ".sdd" / "config.yaml"
    config_path.write_text(
        "# Bernstein demo workspace\n"
        f"server_port: {_DEMO_PORT}\n"
        "max_workers: 2\n"
        "default_model: sonnet\n"
        "default_effort: normal\n"
        f"cli: {adapter}\n"
    )
    (project_dir / ".sdd" / "runtime" / ".gitignore").write_text("*.pid\n*.log\ntasks.jsonl\n")

    # Seed the three backlog tasks
    backlog_open = project_dir / ".sdd" / "backlog" / "open"
    for task in _DEMO_TASKS:
        (backlog_open / task["filename"]).write_text(task["content"])


def _stop_demo_processes(project_dir: Path) -> None:
    """Terminate server, spawner and watchdog started in project_dir.

    Args:
        project_dir: Demo project root whose .sdd/runtime/ holds PID files.
    """
    runtime_dir = project_dir / ".sdd" / "runtime"
    for pid_filename, _label in [
        ("watchdog.pid", "Watchdog"),
        ("spawner.pid", "Spawner"),
        ("server.pid", "Task server"),
    ]:
        pid_file = runtime_dir / pid_filename
        if not pid_file.exists():
            continue
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            continue
        if _is_alive(pid):
            try:
                import signal as _signal

                os.kill(pid, _signal.SIGTERM)
            except OSError:
                pass
        pid_file.unlink(missing_ok=True)


def _print_demo_summary(project_dir: Path, server_url: str) -> None:
    """Print final demo summary: tasks done, files changed, cost.

    Args:
        project_dir: Demo project root.
        server_url: Base URL of the demo task server.
    """
    from rich.table import Table

    tasks_data: list[dict[str, Any]] = []
    total_cost: float = 0.0
    try:
        resp = httpx.get(f"{server_url}/status", timeout=3.0)
        if resp.status_code == 200:
            payload = resp.json()
            tasks_data = payload.get("tasks", [])
            total_cost = payload.get("total_cost_usd", 0.0)
    except Exception:
        pass

    done = sum(1 for t in tasks_data if t.get("status") == "done")
    failed = sum(1 for t in tasks_data if t.get("status") == "failed")
    total = len(tasks_data)

    console.print("\n[bold cyan]── Demo Summary ──────────────────────────[/bold cyan]")

    table = Table(show_header=True, header_style="bold magenta", show_lines=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Tasks completed", f"[green]{done}[/green] / {total}")
    if failed:
        table.add_row("Tasks failed", f"[red]{failed}[/red]")

    # Count Python files in the project dir (excluding .sdd/)
    py_files = [p for p in project_dir.glob("**/*.py") if ".sdd" not in p.parts]
    table.add_row("Python files in project", str(len(py_files)))
    table.add_row("API cost", f"${total_cost:.4f}")
    console.print(table)

    console.print(f"\n[dim]Project directory:[/dim] {project_dir}")
    console.print("[dim]Inspect it to see what the agents changed.[/dim]")
    console.print("\n[bold green]Try it yourself:[/bold green]")
    console.print(f"  cd {project_dir}")
    console.print("  pip install -r requirements.txt")
    console.print("  pytest tests/ -q")


@cli.command("demo")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show the demo plan without spawning any agents.",
)
@click.option(
    "--adapter",
    default=None,
    metavar="NAME",
    help="CLI adapter to use (auto-detected by default).  Choices: claude, codex, gemini, qwen.",
)
@click.option(
    "--timeout",
    default=120,
    show_default=True,
    help="Maximum seconds to wait for tasks to complete.",
)
def demo(dry_run: bool, adapter: str | None, timeout: int) -> None:
    """Zero-to-running demo: spin up a Flask app and ship 3 tasks.

    \b
    Creates a temporary project directory with a Flask hello-world starter,
    seeds 3 tasks into the backlog (health check, tests, error handling),
    then runs agents to complete them while showing live progress.

    \b
      bernstein demo              # run the full demo
      bernstein demo --dry-run    # preview the plan without spawning agents
      bernstein demo --timeout 60 # cap run time at 60 seconds
    """
    import tempfile

    _print_banner()

    # Resolve adapter
    detected = adapter or _detect_available_adapter()
    if detected is None:
        console.print(
            "[red]No supported CLI agent found in PATH.[/red]\n\n"
            "Install one of:\n"
            "  Claude Code  https://claude.ai/code\n"
            "  Codex CLI    https://github.com/openai/codex-cli\n"
            "  Gemini CLI   https://github.com/google-gemini/gemini-cli\n"
        )
        raise SystemExit(1)

    # Always print cost estimate before doing anything
    console.print(
        "\n[bold yellow]Cost estimate:[/bold yellow] "
        "~$0.15 in API credits (3 small tasks, sonnet model)\n"
        f"[dim]Adapter: {detected}  |  Tasks: 3  |  Timeout: {timeout}s[/dim]"
    )

    if dry_run:
        console.print("\n[bold cyan][DRY RUN] What would happen:[/bold cyan]\n")
        from rich.table import Table

        plan_table = Table(show_header=True, header_style="bold magenta")
        plan_table.add_column("Step")
        plan_table.add_column("Action")
        plan_table.add_column("Detail")
        plan_table.add_row("1", "Create project", "Temp dir with Flask hello-world (5 files)")
        plan_table.add_row("2", "Seed backlog", "3 tasks in .sdd/backlog/open/")
        for i, t in enumerate(_DEMO_TASKS, start=3):
            # Parse task inline to get title/role
            parts = t["content"].split("\n")
            title = parts[0].lstrip("# ").strip()
            role = next(
                (ln.split("**Role:**")[-1].strip() for ln in parts if "**Role:**" in ln),
                "backend",
            )
            plan_table.add_row(str(i), f"Run {role} agent", title)
        plan_table.add_row(str(len(_DEMO_TASKS) + 3), "Print summary", "tasks done, cost, files changed")
        console.print(plan_table)
        console.print("\n[dim]No agents were spawned. Run [bold]bernstein demo[/bold] to execute.[/dim]")
        return

    # Create temp project dir
    project_dir = Path(tempfile.mkdtemp(prefix="bernstein-demo-"))
    console.print(f"\n[dim]Creating demo project in {project_dir}…[/dim]")

    _setup_demo_project(project_dir, detected)
    console.print("[green]✓[/green] Flask starter project created (5 files)")
    console.print("[green]✓[/green] 3 tasks seeded: health check, tests, error handling")

    server_url = f"http://127.0.0.1:{_DEMO_PORT}"

    try:
        # Bootstrap: start server + spawner in the demo project dir
        console.print("\n[bold]Starting orchestration…[/bold]")
        from bernstein.core.bootstrap import bootstrap_from_goal

        bootstrap_from_goal(
            goal="Complete the seeded backlog tasks for the demo Flask app.",
            workdir=project_dir,
            port=_DEMO_PORT,
            cli=detected,
        )

        # Poll for completion with a live progress indicator
        from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

        start = time.monotonic()
        deadline = start + timeout

        console.print()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            poll_task = progress.add_task("Agents working…", total=None)

            while time.monotonic() < deadline:
                try:
                    resp = httpx.get(f"{server_url}/status", timeout=3.0)
                    if resp.status_code == 200:
                        payload = resp.json()
                        tasks_list: list[dict[str, Any]] = payload.get("tasks", [])
                        done = sum(1 for t in tasks_list if t.get("status") == "done")
                        failed = sum(1 for t in tasks_list if t.get("status") == "failed")
                        total_tasks = len(tasks_list)
                        progress.update(
                            poll_task,
                            description=(
                                f"Agents working… "
                                f"[green]{done}[/green]/{total_tasks} done"
                                + (f"  [red]{failed} failed[/red]" if failed else "")
                            ),
                        )
                        if total_tasks > 0 and done + failed >= total_tasks:
                            break
                except Exception:
                    pass
                time.sleep(2)

        console.print("[green]✓[/green] Orchestration finished")

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
    except RuntimeError as exc:
        from bernstein.cli.errors import bootstrap_failed

        bootstrap_failed(exc).print()
    finally:
        _stop_demo_processes(project_dir)

    _print_demo_summary(project_dir, server_url)


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("task_id")
@click.option("--reason", "-r", default="Cancelled by user", help="Cancellation reason")
def cancel(task_id: str, reason: str) -> None:
    """Cancel a running or queued task."""
    data = _server_post(f"/tasks/{task_id}/cancel", {"reason": reason})
    if data is None:
        console.print("[red]Server not reachable.[/red]")
        raise SystemExit(1)
    console.print(f"[green]Cancelled:[/green] {data['title']}")
    console.print(f"[dim]Status: {data['status']}[/dim]")


# ---------------------------------------------------------------------------
# approve / reject
# ---------------------------------------------------------------------------


@cli.command("approve")
@click.argument("task_id")
@click.option("--workdir", default=".", help="Project root directory.", type=click.Path())
def approve(task_id: str, workdir: str) -> None:
    """Approve a pending task review so Bernstein merges the work.

    When running with ``--approval review``, Bernstein pauses after each
    verified task and writes a pending approval file.  Run this command
    to signal approval so the orchestrator continues with the merge.

    \b
    Example:
      bernstein approve T-abc123
    """
    approvals_dir = Path(workdir) / ".sdd" / "runtime" / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    decision_file = approvals_dir / f"{task_id}.approved"
    decision_file.write_text("approved")
    console.print(f"[green]Approved:[/green] task [bold]{task_id}[/bold] — Bernstein will merge the work.")


@cli.command("reject")
@click.argument("task_id")
@click.option("--workdir", default=".", help="Project root directory.", type=click.Path())
def reject(task_id: str, workdir: str) -> None:
    """Reject a pending task review so Bernstein discards the work.

    When running with ``--approval review``, Bernstein pauses after each
    verified task and writes a pending approval file.  Run this command
    to reject the work — the worktree will be cleaned up without merging.

    \b
    Example:
      bernstein reject T-abc123
    """
    approvals_dir = Path(workdir) / ".sdd" / "runtime" / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    decision_file = approvals_dir / f"{task_id}.rejected"
    decision_file.write_text("rejected")
    console.print(f"[red]Rejected:[/red] task [bold]{task_id}[/bold] — work will be discarded.")


@cli.command("pending")
@click.option("--workdir", default=".", help="Project root directory.", type=click.Path())
def pending(workdir: str) -> None:
    """List tasks waiting for approval review.

    Shows all tasks that have been verified and are waiting for a human
    decision (``bernstein approve <id>`` or ``bernstein reject <id>``).
    """
    from rich.table import Table

    pending_dir = Path(workdir) / ".sdd" / "runtime" / "pending_approvals"
    if not pending_dir.exists() or not any(pending_dir.glob("*.json")):
        console.print("[dim]No tasks pending approval.[/dim]")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Task ID", style="cyan")
    table.add_column("Title")
    table.add_column("Tests")

    for f in sorted(pending_dir.glob("*.json")):
        try:
            import json as _json

            data = _json.loads(f.read_text())
            table.add_row(
                data.get("task_id", f.stem),
                data.get("task_title", ""),
                data.get("test_summary", ""),
            )
        except Exception:
            table.add_row(f.stem, "[dim]unreadable[/dim]", "")

    console.print(table)
    console.print("\n[dim]Approve with:[/dim] bernstein approve <task_id>")
    console.print("[dim]Reject with:[/dim]  bernstein reject <task_id>")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@cli.command("plan")
@click.option(
    "--export",
    "export_file",
    default=None,
    metavar="FILE",
    help="Write full task list as formatted JSON to FILE.",
)
@click.option(
    "--status",
    "status_filter",
    default=None,
    type=click.Choice(["open", "claimed", "in_progress", "done", "failed", "blocked", "cancelled"]),
    help="Filter tasks by status.",
)
def plan(export_file: str | None, status_filter: str | None) -> None:
    """Show task backlog as a table, or export to JSON.

    \b
      bernstein plan                          # show all tasks
      bernstein plan --status open            # show only open tasks
      bernstein plan --export plan.json       # export full backlog to JSON
    """
    from rich.table import Table

    path = "/tasks"
    if status_filter:
        path = f"/tasks?status={status_filter}"

    raw = _server_get(path)
    if raw is None:
        console.print("[red]Cannot reach task server.[/red] Is Bernstein running? Run [bold]bernstein[/bold] to start.")
        raise SystemExit(1)

    tasks: list[dict[str, Any]] = cast("list[dict[str, Any]]", raw) if isinstance(raw, list) else []

    if export_file:
        out = Path(export_file)
        out.write_text(json.dumps(tasks, indent=2))
        console.print(f"Exported {len(tasks)} tasks to {export_file}")
        return

    if not tasks:
        console.print("[dim]No tasks found.[/dim]")
        return

    table = Table(title="Task Backlog", show_lines=False, header_style="bold cyan")
    table.add_column("ID", style="dim", min_width=10)
    table.add_column("Status", min_width=12)
    table.add_column("Role", min_width=10)
    table.add_column("Title", min_width=30)
    table.add_column("Depends On", min_width=12)
    table.add_column("Model", min_width=8)
    table.add_column("Effort", min_width=8)

    for t in tasks:
        raw_status: str = t.get("status", "open")
        color = STATUS_COLORS.get(raw_status, "white")
        depends = ", ".join(d[:8] for d in cast("list[str]", t.get("depends_on", []))) or "—"
        table.add_row(
            str(t.get("id", "—"))[:8],
            f"[{color}]{raw_status}[/{color}]",
            str(t.get("role", "—")),
            str(t.get("title", "—")),
            depends,
            str(t.get("model") or "—"),
            str(t.get("effort") or "—"),
        )

    console.print(table)


# ---------------------------------------------------------------------------
# logs — tail agent output in real-time
# ---------------------------------------------------------------------------


def _find_agent_logs(runtime_dir: Path, agent_id: str | None) -> list[Path]:
    """Return agent log files from runtime_dir sorted by mtime, optionally filtered by agent_id."""
    if not runtime_dir.exists():
        return []
    log_list = [p for p in runtime_dir.glob("*.log") if p.name != "watchdog.log"]
    if agent_id:
        log_list = [p for p in log_list if agent_id in p.stem]
    return sorted(log_list, key=lambda p: p.stat().st_mtime)


@cli.command("logs")
@click.option("--follow", "-f", is_flag=True, default=False, help="Stream log output in real-time (like tail -f).")
@click.option("--agent", "-a", default=None, help="Filter by agent session ID (partial match).")
@click.option("--lines", "-n", default=50, show_default=True, help="Number of lines to show without --follow.")
@click.option(
    "--runtime-dir",
    default=".sdd/runtime",
    show_default=True,
    hidden=True,
    help="Directory containing agent log files.",
)
def logs_cmd(follow: bool, agent: str | None, lines: int, runtime_dir: str) -> None:
    """Tail agent log output.

    Without --follow, prints the last N lines of the most recent agent log.
    With --follow (-f), streams new output in real-time until Ctrl+C.
    """
    rdir = Path(runtime_dir)
    log_files = _find_agent_logs(rdir, agent)

    if not log_files:
        suffix = f" matching '{agent}'" if agent else ""
        console.print(f"[yellow]No agent logs found{suffix} in {rdir}[/yellow]")
        raise SystemExit(1)

    log_path = log_files[-1]  # most recent
    console.print(f"[dim]Watching:[/dim] [bold]{log_path.name}[/bold]")

    if not follow:
        text = log_path.read_text(errors="replace")
        tail_lines = text.splitlines()[-lines:]
        console.print("\n".join(tail_lines) or "[dim](empty)[/dim]")
        return

    # --follow: print last N lines as context then stream new bytes
    try:
        existing = log_path.read_text(errors="replace")
        context = existing.splitlines()[-lines:]
        if context:
            console.print("\n".join(context))
        offset = log_path.stat().st_size
    except FileNotFoundError:
        offset = 0

    console.print("[dim]--- following (Ctrl+C to stop) ---[/dim]")
    try:
        while True:
            try:
                size = log_path.stat().st_size
            except FileNotFoundError:
                time.sleep(0.2)
                continue

            if size > offset:
                with log_path.open("rb") as fh:
                    fh.seek(offset)
                    new_bytes = fh.read(size - offset)
                offset = size
                console.print(new_bytes.decode(errors="replace"), end="")

            time.sleep(0.2)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped.[/dim]")


@cli.command("notes", hidden=True)
@click.option("--lines", "-n", default=40, show_default=True, help="Number of tail lines to display.")
@click.option("--component", type=click.Choice(["server", "spawner"]), default="server", show_default=True)
def _notes_legacy(lines: int, component: str) -> None:
    """Tail server or spawner logs (legacy alias)."""
    log_path = Path(f".sdd/runtime/{component}.log")
    if not log_path.exists():
        console.print(f"[red]Log file not found:[/red] {log_path}")
        raise SystemExit(1)

    from rich.panel import Panel

    all_lines = log_path.read_text(errors="replace").splitlines()
    tail = all_lines[-lines:]
    console.print(
        Panel(
            "\n".join(tail) or "[dim](empty)[/dim]",
            title=f"[bold]{component}.log[/bold] (last {lines} lines)",
            border_style="dim",
        )
    )


# ---------------------------------------------------------------------------
# tasks  (alias for `bernstein status --tasks-only`)
# ---------------------------------------------------------------------------


@cli.command("parts", hidden=True)
@click.option(
    "--status-filter",
    "status_filter",
    default=None,
    type=click.Choice(["open", "claimed", "in_progress", "done", "failed", "blocked"]),
    help="Filter by task status.",
)
@click.option("--role", default=None, help="Filter by role.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def list_tasks(status_filter: str | None, role: str | None, as_json: bool) -> None:
    """List tasks with optional filters."""
    data = _server_get("/status")
    if data is None:
        console.print("[red]Cannot reach task server.[/red] Is Bernstein running? Run [bold]bernstein[/bold] to start.")
        raise SystemExit(1)

    tasks: list[dict[str, Any]] = data.get("tasks", [])

    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    if role:
        tasks = [t for t in tasks if t.get("role") == role]

    if as_json:
        console.print_json(json.dumps(tasks))
        return

    if not tasks:
        console.print("[dim]No tasks matching filters.[/dim]")
        return

    from rich.table import Table

    table = Table(show_lines=False, header_style="bold cyan")
    table.add_column("ID", style="dim", min_width=10)
    table.add_column("Title", min_width=30)
    table.add_column("Role", min_width=10)
    table.add_column("Status", min_width=14)
    table.add_column("Priority", justify="right")

    for t in tasks:
        raw_status = t.get("status", "open")
        color = STATUS_COLORS.get(raw_status, "white")
        table.add_row(
            t.get("id", "—"),
            t.get("title", "—"),
            t.get("role", "—"),
            f"[{color}]{raw_status}[/{color}]",
            str(t.get("priority", 2)),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# live — module-level display helpers (extracted for testability)
# ---------------------------------------------------------------------------


def _build_agents_table(agents: list[dict[str, Any]]) -> Table:
    """Build a Rich Table showing active agents."""
    from rich.table import Table

    table = Table(
        title="Agents",
        show_lines=False,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Agent", min_width=18)
    table.add_column("Model", min_width=8)
    table.add_column("Status", min_width=10)
    table.add_column("Runtime", justify="right", min_width=7)
    table.add_column("Tasks", justify="right", min_width=5)

    for a in agents:
        raw = a.get("status", "idle")
        color = {"working": "yellow", "starting": "cyan", "dead": "red"}.get(raw, "green" if raw == "done" else "dim")
        runtime_s = a.get("runtime_s", 0)
        mins, secs = divmod(int(runtime_s), 60)
        runtime_str = f"{mins}:{secs:02d}" if runtime_s > 0 else "—"
        model = a.get("model") or "—"
        task_count = len(a.get("task_ids", []))
        table.add_row(
            f"[bold]{a.get('role', '?')}[/bold] [dim]{a.get('id', '—')[-8:]}[/dim]",
            model,
            f"[{color}]{raw}[/{color}]",
            runtime_str,
            str(task_count),
        )
    return table


def _build_events_table(tasks: list[dict[str, Any]]) -> Table:
    """Build a Rich Table showing tasks."""
    from rich.table import Table

    table = Table(
        title="Tasks",
        show_lines=False,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Status", min_width=9)
    table.add_column("Role", min_width=8)
    table.add_column("Title")

    for t in tasks:
        raw_status = t.get("status", "open")
        color = STATUS_COLORS.get(raw_status, "white")
        icon = {"done": "+", "failed": "x", "claimed": ">", "open": " "}.get(raw_status, " ")
        table.add_row(
            f"[{color}]{icon} {raw_status}[/{color}]",
            t.get("role", "—"),
            t.get("title", "—"),
        )
    return table


def _build_stats_bar(summary: dict[str, Any]) -> Text:
    """Build a Rich Text stats bar from a summary dict."""
    from rich.text import Text

    total = summary.get("total", 0)
    done = summary.get("done", 0)
    in_prog = summary.get("in_progress", 0)
    failed = summary.get("failed", 0)
    elapsed_s = int(summary.get("elapsed_seconds", 0))
    mins, secs = divmod(elapsed_s, 60)

    bar = Text()
    bar.append(f"Tasks: {total}  ", style="bold")
    bar.append(f"done={done} ", style="green")
    bar.append(f"working={in_prog} ", style="yellow")
    bar.append(f"failed={failed}  ", style="red")
    # Progress bar
    if total > 0:
        pct = int(done / total * 100)
        filled = pct // 5
        bar.append(f"[{'=' * filled}{' ' * (20 - filled)}] {pct}%  ", style="bold green")
    bar.append(f"{mins}m{secs:02d}s", style="dim")
    return bar


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


@cli.command("live")
@click.option(
    "--interval",
    default=2.0,
    show_default=True,
    help="Polling interval in seconds.",
)
@click.option(
    "--classic",
    is_flag=True,
    default=False,
    help="Use the classic Rich Live display instead of the Textual TUI.",
)
def live(interval: float, classic: bool) -> None:
    """Live dashboard: active agents, task events, and stats (Ctrl+C to exit).

    Launches the Textual TUI session manager by default.
    Pass --classic for the original Rich Live display.
    """
    if not classic:
        from bernstein.tui.app import BernsteinApp

        app = BernsteinApp(poll_interval=interval)
        app.run()
        return

    # -- classic Rich Live display (kept for fallback) --
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel

    _print_banner()

    def _fetch_dashboard() -> dict[str, Any]:
        """Fetch all data needed for the live dashboard."""
        status = _server_get("/status")
        if status is None:
            return {}
        tasks_raw = _server_get("/tasks")
        tasks: list[dict[str, Any]] = cast("list[dict[str, Any]]", tasks_raw) if isinstance(tasks_raw, list) else []

        # Read agent state from orchestrator's agents.json (written each tick)
        agents: list[dict[str, Any]] = []
        agents_json = Path(".sdd/runtime/agents.json")
        if agents_json.exists():
            try:
                import json as _json

                data = _json.loads(agents_json.read_text())
                agents = [a for a in data.get("agents", []) if a.get("status") != "dead"]
            except (OSError, ValueError):
                pass

        elapsed = time.time() - _live_start_ts

        return {
            "agents": agents,
            "tasks": tasks,
            "summary": {
                "total": status.get("total", 0),
                "done": status.get("done", 0),
                "in_progress": status.get("claimed", 0),
                "failed": status.get("failed", 0),
                "elapsed_seconds": elapsed,
            },
        }

    _live_start_ts = time.time()
    data: dict[str, Any] = {}

    def _render() -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="agents", ratio=3),
            Layout(name="events", ratio=4),
            Layout(name="stats", size=3),
        )
        agents = data.get("agents", [])
        tasks = data.get("tasks", [])
        summary = data.get("summary", {})
        layout["agents"].update(_build_agents_table(agents))
        layout["events"].update(_build_events_table(tasks))
        layout["stats"].update(Panel(_build_stats_bar(summary), border_style="blue"))
        return layout

    try:
        with Live(_render(), refresh_per_second=1, screen=True) as live_display:
            while True:
                data = _fetch_dashboard()
                tasks_list: list[dict[str, Any]] = data.get("tasks", [])
                new_map: dict[str, str] = {}
                for t in tasks_list:
                    new_map[t.get("id", "")] = t.get("status", "open")
                live_display.update(_render())
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    console.print("\n[dim]Live display stopped.[/dim]")


# ---------------------------------------------------------------------------
# web dashboard
# ---------------------------------------------------------------------------


@cli.command("dashboard")
@click.option("--port", default=8052, show_default=True, help="Server port.")
@click.option("--no-open", is_flag=True, default=False, help="Do not open browser.")
def dashboard(port: int, no_open: bool) -> None:
    """Open the web dashboard in your browser.

    Requires the Bernstein server to be running. If it is not,
    prints instructions on how to start it.
    """
    import webbrowser

    url = f"http://localhost:{port}/dashboard"
    # Check if server is alive
    try:
        resp = httpx.get(f"http://localhost:{port}/health", timeout=2.0)
        if resp.status_code != 200:
            console.print(
                f"[red]Server returned {resp.status_code}.[/red]\nStart the server first: [cyan]bernstein run[/cyan]"
            )
            sys.exit(1)
    except httpx.ConnectError:
        console.print(
            "[red]Cannot connect to Bernstein server.[/red]\n"
            f"Start the server first: [cyan]bernstein run[/cyan]\n"
            f"Then open: [link={url}]{url}[/link]"
        )
        sys.exit(1)

    console.print(f"[green]Dashboard:[/green] [link={url}]{url}[/link]")
    if not no_open:
        webbrowser.open(url)


# ---------------------------------------------------------------------------
# benchmark
# ---------------------------------------------------------------------------


@cli.group("benchmark")
def benchmark_group() -> None:
    """Run the tiered golden benchmark suite."""


@benchmark_group.command("swe-bench")
@click.option("--lite", "mode", flag_value="lite", default=True, help="Run SWE-Bench Lite (300 instances).")
@click.option("--sample", "sample", type=int, default=None, help="Evaluate a random sample of N instances.")
@click.option("--instance", "instance_id", default=None, help="Evaluate a single instance by ID.")
@click.option("--dataset", "dataset_path", default=None, help="Path to local JSONL dataset file.")
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Persist results to .sdd/benchmark/swe_bench_results.json.",
)
def benchmark_swe_bench(
    mode: str,
    sample: int | None,
    instance_id: str | None,
    dataset_path: str | None,
    save: bool,
) -> None:
    """Run Bernstein against SWE-Bench instances and report resolve rate.

    \b
      bernstein benchmark swe-bench --lite              # all 300 Lite instances
      bernstein benchmark swe-bench --sample 20         # random 20-instance eval
      bernstein benchmark swe-bench --instance django__django-11905
    """
    from rich.table import Table

    from bernstein.benchmark.swe_bench import InstanceResult, SWEBenchRunner, compute_report, save_results

    workdir = Path(".")
    runner = SWEBenchRunner(workdir=workdir, sample=sample, instance_id=instance_id)

    dpath = Path(dataset_path) if dataset_path else None
    instances = runner.load_dataset(dpath)

    if not instances:
        console.print(
            "[yellow]No instances found. Pass --dataset <path.jsonl> or install the 'datasets' package.[/yellow]"
        )
        raise SystemExit(1)

    console.print(f"[bold]SWE-Bench evaluation[/bold] — {len(instances)} instance(s)")

    table = Table(title="SWE-Bench Results", header_style="bold cyan", show_lines=False)
    table.add_column("Instance", style="dim", min_width=30)
    table.add_column("Resolved", min_width=10)
    table.add_column("Cost (USD)", justify="right", min_width=12)
    table.add_column("Time (s)", justify="right", min_width=10)
    table.add_column("Agents", justify="right", min_width=8)

    results: list[InstanceResult] = []
    for inst in instances:
        console.print(f"  Running [cyan]{inst.instance_id}[/cyan]…", end="")
        result = runner.run_instance(inst)
        results.append(result)
        status = "[green]✓[/green]" if result.resolved else "[red]✗[/red]"
        console.print(f" {status}")
        table.add_row(
            inst.instance_id,
            "[green]YES[/green]" if result.resolved else "[red]NO[/red]",
            f"${result.cost_usd:.4f}",
            f"{result.duration_seconds:.1f}",
            str(result.agent_count),
        )

    report = compute_report(results)
    console.print(table)
    console.print(
        f"\n[bold]Resolve rate:[/bold] {report.resolve_rate:.1%} "
        f"({report.resolved}/{report.total})  "
        f"[dim]median cost ${report.median_cost_usd:.4f}  "
        f"median time {report.median_duration_seconds:.0f}s[/dim]"
    )

    if save:
        sdd_dir = Path(".sdd")
        out = save_results(report, sdd_dir)
        console.print(f"[dim]Results saved → {out}[/dim]")


@benchmark_group.command("run")
@click.option(
    "--tier",
    type=click.Choice(["smoke", "capability", "stretch", "all"]),
    default="all",
    show_default=True,
    help="Which benchmark tier to run.",
)
@click.option(
    "--benchmarks-dir",
    default="tests/benchmarks",
    show_default=True,
    help="Root directory containing smoke/capability/stretch sub-dirs.",
)
@click.option(
    "--save/--no-save",
    default=True,
    show_default=True,
    help="Persist results to .sdd/benchmarks/YYYY-MM-DD.jsonl.",
)
def benchmark_run(tier: str, benchmarks_dir: str, save: bool) -> None:
    """Run benchmark suite and report pass/fail per benchmark.

    \b
      bernstein benchmark run                  # run all tiers
      bernstein benchmark run --tier smoke     # smoke only
      bernstein benchmark run --tier stretch   # stretch only
    """
    from rich.table import Table

    from bernstein.evolution.benchmark import (
        run_all,
        run_selected,
        save_results,
    )

    bdir = Path(benchmarks_dir)
    if not bdir.exists():
        console.print(f"[red]Benchmarks directory not found:[/red] {bdir}")
        raise SystemExit(1)

    summary = run_all(bdir) if tier == "all" else run_selected(bdir, tier)  # type: ignore[arg-type]

    # ---- Results table ----
    table = Table(title=f"Benchmarks — tier={tier}", header_style="bold cyan", show_lines=False)
    table.add_column("ID", style="dim", min_width=14)
    table.add_column("Tier", min_width=12)
    table.add_column("Goal", min_width=40)
    table.add_column("Result", min_width=8)
    table.add_column("Duration", justify="right", min_width=10)

    for result in summary.results:
        status_str = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
        table.add_row(
            result.benchmark_id,
            result.tier,
            result.goal,
            status_str,
            f"{result.duration_seconds:.2f}s",
        )
        if not result.passed:
            for sig in result.signal_results:
                if not sig.passed:
                    table.add_row(
                        "",
                        "",
                        f"  [dim]↳ {sig.signal_type}: {sig.message}[/dim]",
                        "",
                        "",
                    )

    console.print(table)
    console.print(
        f"\n[bold]Total:[/bold] {summary.total}  "
        f"[green]{summary.passed} passed[/green]  "
        f"[red]{summary.failed} failed[/red]"
    )

    if save and summary.total > 0:
        sdd_dir = Path(".sdd")
        out = save_results(summary, sdd_dir)
        console.print(f"[dim]Results saved → {out}[/dim]")

    if summary.failed > 0:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# eval — multiplicative scoring harness
# ---------------------------------------------------------------------------


@cli.group("eval")
def eval_group() -> None:
    """Evaluation harness with multiplicative scoring."""


@eval_group.command("run")
@click.option(
    "--tier",
    type=click.Choice(["smoke", "standard", "stretch", "adversarial"]),
    default=None,
    help="Run only tasks from this tier.",
)
@click.option("--compare", "compare_prev", is_flag=True, default=False, help="Compare vs previous run.")
@click.option("--save/--no-save", default=True, show_default=True, help="Persist results to disk.")
def eval_run(tier: str | None, compare_prev: bool, save: bool) -> None:
    """Run the golden benchmark suite with multiplicative scoring.

    \b
      bernstein eval run                    # run full golden suite
      bernstein eval run --tier smoke       # smoke tier only
      bernstein eval run --compare          # compare vs previous run
    """
    from rich.table import Table

    from bernstein.eval.harness import EvalHarness, TaskEvalResult

    workdir = Path(".")
    state_dir = workdir / ".sdd"
    harness = EvalHarness(state_dir=state_dir, repo_root=workdir)

    tier_filter: Tier | None = tier  # type: ignore[assignment]
    tasks = harness.load_golden_tasks(tier_filter=tier_filter)

    if not tasks:
        console.print("[yellow]No golden tasks found.[/yellow]")
        console.print(f"[dim]Expected at: {state_dir / 'eval' / 'golden'}/<tier>/*.md[/dim]")
        raise SystemExit(1)

    console.print(f"[bold]Eval harness[/bold] — {len(tasks)} golden task(s)")

    # Evaluate each task (with empty telemetry for now — real runs
    # would collect telemetry from actual agent execution)
    task_results: list[TaskEvalResult] = []
    for task in tasks:
        result = harness.evaluate_task(task)
        task_results.append(result)

    run_result = harness.compute_multiplicative_score(task_results)

    # Display results
    table = Table(title="Eval Results", header_style="bold cyan", show_lines=False)
    table.add_column("Component", min_width=15)
    table.add_column("Score", justify="right", min_width=10)

    mc = run_result.multiplicative_components
    if mc:
        table.add_row("Task Success", f"{mc.task_success:.2%}")
        table.add_row("Code Quality", f"{mc.code_quality:.2%}")
        table.add_row("Efficiency", f"{mc.efficiency:.2%}")
        table.add_row("Reliability", f"{mc.reliability:.2%}")
        table.add_row("Safety", f"{mc.safety:.2%}")
        table.add_row("", "")
        table.add_row("[bold]Final Score[/bold]", f"[bold]{mc.final_score:.4f}[/bold]")

    console.print(table)

    # Per-tier breakdown
    pt = run_result.per_tier
    if pt:
        tier_table = Table(title="Per-Tier Scores", header_style="bold cyan")
        tier_table.add_column("Tier", min_width=15)
        tier_table.add_column("Score", justify="right", min_width=10)
        tier_table.add_row("Smoke", f"{pt.smoke:.2%}")
        tier_table.add_row("Standard", f"{pt.standard:.2%}")
        tier_table.add_row("Stretch", f"{pt.stretch:.2%}")
        tier_table.add_row("Adversarial", f"{pt.adversarial:.2%}")
        console.print(tier_table)

    # Compare with previous run
    if compare_prev:
        prev = harness.load_previous_run()
        if prev:
            delta = run_result.score - prev.score
            color = "green" if delta >= 0 else "red"
            console.print(f"\n[bold]vs previous:[/bold] [{color}]{delta:+.4f}[/{color}]")
            console.print(f"[dim]Previous score: {prev.score:.4f}[/dim]")
        else:
            console.print("[dim]No previous run found for comparison.[/dim]")

    # Save results
    if save:
        path = harness.save_run(run_result)
        console.print(f"[dim]Results saved → {path}[/dim]")


@eval_group.command("report")
def eval_report() -> None:
    """Generate a markdown report from the most recent eval run."""
    from bernstein.eval.harness import EvalHarness

    workdir = Path(".")
    state_dir = workdir / ".sdd"
    harness = EvalHarness(state_dir=state_dir, repo_root=workdir)

    prev = harness.load_previous_run()
    if not prev:
        console.print("[yellow]No eval runs found.[/yellow]")
        raise SystemExit(1)

    console.print(f"[bold]Eval Report[/bold] — score: {prev.score:.4f}")

    mc = prev.multiplicative_components
    if mc:
        console.print(f"  Task Success:  {mc.task_success:.2%}")
        console.print(f"  Code Quality:  {mc.code_quality:.2%}")
        console.print(f"  Efficiency:    {mc.efficiency:.2%}")
        console.print(f"  Reliability:   {mc.reliability:.2%}")
        console.print(f"  Safety:        {mc.safety:.2%}")

    pt = prev.per_tier
    if pt:
        console.print(f"\n  Smoke:       {pt.smoke:.2%}")
        console.print(f"  Standard:    {pt.standard:.2%}")
        console.print(f"  Stretch:     {pt.stretch:.2%}")
        console.print(f"  Adversarial: {pt.adversarial:.2%}")

    if prev.cost_total > 0:
        console.print(f"\n  Total cost: ${prev.cost_total:.2f}")

    console.print(f"  Tasks evaluated: {prev.tasks_evaluated}")


@eval_group.command("failures")
def eval_failures() -> None:
    """Show failure taxonomy breakdown from the most recent eval run."""
    import json as json_mod

    from rich.table import Table

    workdir = Path(".")
    runs_dir = workdir / ".sdd" / "eval" / "runs"

    if not runs_dir.is_dir():
        console.print("[yellow]No eval runs found.[/yellow]")
        raise SystemExit(1)

    run_files = sorted(runs_dir.glob("eval_run_*.json"), reverse=True)
    if not run_files:
        console.print("[yellow]No eval runs found.[/yellow]")
        raise SystemExit(1)

    data = json_mod.loads(run_files[0].read_text(encoding="utf-8"))
    failures = data.get("failures", [])

    if not failures:
        console.print("[green]No failures in the most recent run.[/green]")
        return

    table = Table(title="Failure Taxonomy", header_style="bold red", show_lines=True)
    table.add_column("Task", min_width=20)
    table.add_column("Category", min_width=18)
    table.add_column("Details", min_width=40)

    for f in failures:
        table.add_row(
            str(f.get("task", "")),
            str(f.get("taxonomy", "")),
            str(f.get("details", "")),
        )

    console.print(table)

    # Category counts
    counts: dict[str, int] = {}
    for f in failures:
        cat = str(f.get("taxonomy", "unknown"))
        counts[cat] = counts.get(cat, 0) + 1

    console.print(f"\n[bold]Total failures:[/bold] {len(failures)}")
    for cat, count in sorted(counts.items(), key=lambda x: -x[1]):
        console.print(f"  {cat}: {count}")


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------

cli.add_command(cost_cmd, "cost")


# ---------------------------------------------------------------------------
# workspace — multi-repo workspace management
# ---------------------------------------------------------------------------


@cli.group("workspace", invoke_without_command=True)
@click.pass_context
def workspace_group(ctx: click.Context) -> None:
    """Multi-repo workspace management.

    Without a subcommand, shows repo status table.
    """
    if ctx.invoked_subcommand is not None:
        return

    from rich.table import Table

    data = _server_get("/workspace")
    if data is None:
        # No server running — try to parse workspace from seed file
        seed_path = _find_seed_file()
        if seed_path is None:
            console.print("[dim]No workspace configured (no bernstein.yaml found).[/dim]")
            return

        from bernstein.core.seed import SeedError, parse_seed

        try:
            cfg = parse_seed(seed_path)
        except SeedError as exc:
            from bernstein.cli.errors import seed_parse_error

            seed_parse_error(exc).print()
            return

        if cfg.workspace is None:
            console.print("[dim]No workspace section in bernstein.yaml.[/dim]")
            return

        ws = cfg.workspace
        repo_statuses = ws.status()

        table = Table(title="Workspace repos", show_header=True, header_style="bold magenta")
        table.add_column("Repo", style="cyan")
        table.add_column("Path")
        table.add_column("Branch", justify="center")
        table.add_column("Clean", justify="center")
        table.add_column("Ahead", justify="right")
        table.add_column("Behind", justify="right")

        for repo in ws.repos:
            rs = repo_statuses.get(repo.name)
            if rs:
                clean_icon = "[green]yes[/green]" if rs.clean else "[red]no[/red]"
                table.add_row(repo.name, str(repo.path), rs.branch, clean_icon, str(rs.ahead), str(rs.behind))
            else:
                table.add_row(repo.name, str(repo.path), "[dim]n/a[/dim]", "[dim]n/a[/dim]", "-", "-")

        console.print(table)
        return

    # Server is running — render from API response
    from rich.table import Table as RichTable

    table = RichTable(title="Workspace repos", show_header=True, header_style="bold magenta")
    table.add_column("Repo", style="cyan")
    table.add_column("Path")
    table.add_column("Branch", justify="center")
    table.add_column("Clean", justify="center")
    table.add_column("Ahead", justify="right")
    table.add_column("Behind", justify="right")

    for repo in data.get("repos", []):
        clean_icon = "[green]yes[/green]" if repo.get("clean") else "[red]no[/red]"
        table.add_row(
            repo["name"],
            repo["path"],
            repo.get("branch", "unknown"),
            clean_icon,
            str(repo.get("ahead", 0)),
            str(repo.get("behind", 0)),
        )

    console.print(table)


@workspace_group.command("clone")
def workspace_clone() -> None:
    """Clone all missing repos defined in the workspace."""
    seed_path = _find_seed_file()
    if seed_path is None:
        console.print("[red]No bernstein.yaml found.[/red]")
        return

    from bernstein.core.seed import SeedError, parse_seed

    try:
        cfg = parse_seed(seed_path)
    except SeedError as exc:
        console.print(f"[red]Error parsing seed file:[/red] {exc}")
        return

    if cfg.workspace is None:
        console.print("[dim]No workspace section in bernstein.yaml.[/dim]")
        return

    cloned = cfg.workspace.clone_missing()
    if cloned:
        for name in cloned:
            console.print(f"[green]Cloned[/green] {name}")
    else:
        console.print("[dim]All repos already present (or no clone URLs configured).[/dim]")


@workspace_group.command("validate")
def workspace_validate() -> None:
    """Check workspace health — all repos exist and are valid git repos."""
    seed_path = _find_seed_file()
    if seed_path is None:
        console.print("[red]No bernstein.yaml found.[/red]")
        return

    from bernstein.core.seed import SeedError, parse_seed

    try:
        cfg = parse_seed(seed_path)
    except SeedError as exc:
        console.print(f"[red]Error parsing seed file:[/red] {exc}")
        return

    if cfg.workspace is None:
        console.print("[dim]No workspace section in bernstein.yaml.[/dim]")
        return

    issues = cfg.workspace.validate()
    if issues:
        for issue in issues:
            console.print(f"[red]Issue:[/red] {issue}")
    else:
        console.print(f"[green]All {len(cfg.workspace.repos)} repos are healthy.[/green]")


# ---------------------------------------------------------------------------
# config — global ~/.bernstein config management
# ---------------------------------------------------------------------------


@cli.group("config")
def config_group() -> None:
    """Manage global Bernstein configuration (~/.bernstein/config.yaml)."""


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a global config value.

    Example: bernstein config set cli codex
    """
    from bernstein.core.home import BernsteinHome

    home = BernsteinHome.default()
    # Coerce numeric strings
    parsed_value: Any
    try:
        parsed_value = float(value) if "." in value else int(value)
    except ValueError:
        parsed_value = value if value.lower() not in ("null", "none") else None
    home.set(key, parsed_value)
    console.print(f"[green]✓[/green] {key} = {parsed_value!r}  [dim](~/.bernstein/config.yaml)[/dim]")


@config_group.command("get")
@click.argument("key")
@click.option("--project-dir", default=".", show_default=True, help="Project directory for precedence check.")
def config_get(key: str, project_dir: str) -> None:
    """Show the effective value for KEY and its source.

    Example: bernstein config get cli
    """
    from bernstein.core.home import BernsteinHome, resolve_config

    home = BernsteinHome.default()
    result = resolve_config(key, home=home, project_dir=Path(project_dir))
    source_style = {"project": "cyan", "global": "yellow", "default": "dim"}.get(result["source"], "white")
    console.print(
        f"[bold]{key}[/bold] = {result['value']!r}  [{source_style}](source: {result['source']})[/{source_style}]"
    )


@config_group.command("list")
@click.option("--project-dir", default=".", show_default=True, help="Project directory for precedence check.")
def config_list(project_dir: str) -> None:
    """List all config keys with their effective values and sources."""
    from rich.table import Table

    from bernstein.core.home import _DEFAULTS, BernsteinHome, resolve_config  # type: ignore[reportPrivateUsage]

    home = BernsteinHome.default()
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Key")
    table.add_column("Value")
    table.add_column("Source")

    source_styles = {"project": "cyan", "global": "yellow", "default": "dim"}

    for key in sorted(_DEFAULTS.keys()):
        result = resolve_config(key, home=home, project_dir=Path(project_dir))
        style = source_styles.get(result["source"], "white")
        table.add_row(
            key,
            str(result["value"]),
            f"[{style}]{result['source']}[/{style}]",
        )

    console.print(table)


# ---------------------------------------------------------------------------
# retro — on-demand retrospective report
# ---------------------------------------------------------------------------


def _load_archive_tasks(
    archive_path: Path,
    since_ts: float | None,
) -> tuple[list[Any], list[Any]]:
    """Load done/failed Task objects from the archive JSONL file.

    Args:
        archive_path: Path to .sdd/archive/tasks.jsonl.
        since_ts: If set, only include tasks completed after this timestamp.

    Returns:
        Tuple of (done_tasks, failed_tasks) as Task objects.
    """
    from bernstein.core.models import Complexity, Scope, Task, TaskStatus

    done_tasks: list[Task] = []
    failed_tasks: list[Task] = []

    if not archive_path.exists():
        return done_tasks, failed_tasks

    for line in archive_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        status = record.get("status", "")
        if status not in ("done", "failed"):
            continue

        completed_at = record.get("completed_at")
        if since_ts is not None and (completed_at is None or completed_at < since_ts):
            continue

        task = Task(
            id=record.get("task_id", ""),
            title=record.get("title", ""),
            description=record.get("result_summary", "") or "",
            role=record.get("role", "unknown"),
            complexity=Complexity(record.get("complexity", "medium")),
            scope=Scope(record.get("scope", "medium")),
            status=TaskStatus.DONE if status == "done" else TaskStatus.FAILED,
            created_at=record.get("created_at", 0.0),
        )
        if status == "done":
            done_tasks.append(task)
        else:
            failed_tasks.append(task)

    return done_tasks, failed_tasks


def _build_collector_from_archive(
    archive_path: Path,
    since_ts: float | None,
) -> Any:
    """Build a MetricsCollector populated from the archive JSONL file.

    Args:
        archive_path: Path to .sdd/archive/tasks.jsonl.
        since_ts: If set, only include tasks completed after this timestamp.

    Returns:
        MetricsCollector with task metrics pre-populated.
    """
    from bernstein.core.metrics import MetricsCollector, TaskMetrics

    collector = MetricsCollector(metrics_dir=None)

    if not archive_path.exists():
        return collector

    for line in archive_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        status = record.get("status", "")
        if status not in ("done", "failed"):
            continue

        completed_at: float | None = record.get("completed_at")
        if since_ts is not None and (completed_at is None or completed_at < since_ts):
            continue

        task_id = record.get("task_id", "")
        role = record.get("role", "unknown")
        model = record.get("model") or "unknown"
        provider = record.get("provider") or "unknown"
        start_time: float = record.get("created_at") or 0.0
        cost_usd: float = record.get("cost_usd") or 0.0

        tm = TaskMetrics(
            task_id=task_id,
            role=role,
            model=model,
            provider=provider,
            start_time=start_time,
            end_time=completed_at,
            success=(status == "done"),
            cost_usd=cost_usd,
        )
        collector._task_metrics[task_id] = tm  # type: ignore[reportPrivateUsage]

    # Enrich with token data from .sdd/metrics/tasks.jsonl if available
    metrics_path = archive_path.parent.parent / "metrics" / "tasks.jsonl"
    if metrics_path.exists():
        metrics_by_task: dict[str, dict[str, Any]] = {}
        for line in metrics_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = rec.get("task_id", "")
            if tid:
                metrics_by_task[str(tid)] = rec

        for task_id, tm in collector._task_metrics.items():  # type: ignore[reportPrivateUsage]
            if task_id in metrics_by_task:
                rec = metrics_by_task[task_id]
                tm.tokens_prompt = int(rec.get("tokens_prompt", 0) or 0)
                tm.tokens_completion = int(rec.get("tokens_completion", 0) or 0)
                tm.tokens_used = tm.tokens_prompt + tm.tokens_completion
                # Use metrics cost if archive cost is missing
                if not tm.cost_usd and rec.get("cost_usd"):
                    tm.cost_usd = float(rec.get("cost_usd") or 0.0)

    return collector


@cli.command("retro")
@click.option(
    "--since",
    default=None,
    metavar="HOURS",
    type=float,
    help="Only include tasks completed in the last N hours.",
)
@click.option(
    "--output",
    "-o",
    default=None,
    metavar="FILE",
    help="Write report to FILE instead of stdout (default: .sdd/runtime/retrospective.md).",
)
@click.option(
    "--print",
    "print_output",
    is_flag=True,
    default=False,
    help="Print report to stdout even when writing to file.",
)
@click.option(
    "--archive",
    default=".sdd/archive/tasks.jsonl",
    show_default=True,
    hidden=True,
    help="Path to the task archive JSONL file.",
)
def retro(
    since: float | None,
    output: str | None,
    print_output: bool,
    archive: str,
) -> None:
    """Generate a retrospective report from completed and failed tasks.

    \b
    Reads task history from .sdd/archive/tasks.jsonl and writes a markdown
    report to .sdd/runtime/retrospective.md.

    \b
      bernstein retro                    # report on all recorded tasks
      bernstein retro --since 24         # last 24 hours only
      bernstein retro --print            # print to stdout as well
      bernstein retro -o report.md       # write to custom file
    """
    import time as _time

    from bernstein.core.retrospective import generate_retrospective

    workdir = Path.cwd()
    archive_path = Path(archive)
    runtime_dir = workdir / ".sdd" / "runtime"

    since_ts: float | None = None
    if since is not None:
        since_ts = _time.time() - since * 3600

    done_tasks, failed_tasks = _load_archive_tasks(archive_path, since_ts)

    if not done_tasks and not failed_tasks:
        label = f"in the last {since}h" if since is not None else "in the archive"
        console.print(f"[yellow]No completed or failed tasks found {label}.[/yellow]")
        console.print(f"[dim]Archive: {archive_path}[/dim]")
        raise SystemExit(0)

    collector = _build_collector_from_archive(archive_path, since_ts)

    all_ts = [t.created_at for t in done_tasks + failed_tasks if t.created_at]
    run_start_ts = min(all_ts) if all_ts else _time.time()

    # Redirect output if custom file given
    out_dir = runtime_dir
    retro_filename = "retrospective.md"
    if output:
        out_path = Path(output).resolve()
        out_dir = out_path.parent
        retro_filename = out_path.name

    generate_retrospective(
        done_tasks=done_tasks,
        failed_tasks=failed_tasks,
        collector=collector,
        runtime_dir=out_dir,
        run_start_ts=run_start_ts,
    )

    # generate_retrospective always writes to runtime_dir/retrospective.md
    actual_path = out_dir / "retrospective.md"
    if output and Path(output).name != "retrospective.md":
        # Rename to the requested filename
        actual_path.rename(out_dir / retro_filename)
        actual_path = out_dir / retro_filename

    if print_output:
        console.print(actual_path.read_text())
    else:
        console.print(f"[green]Retrospective written to[/green] {actual_path}")
        console.print(
            f"[dim]{len(done_tasks)} done, {len(failed_tasks)} failed"
            + (f", since {since}h ago" if since is not None else "")
            + "[/dim]"
        )


# ---------------------------------------------------------------------------
# agents — catalog management
# ---------------------------------------------------------------------------


@cli.group("agents")
def agents_group() -> None:
    """Manage agent catalogs: sync, list, and validate.

    \b
      bernstein agents sync               # refresh all catalogs
      bernstein agents list               # show all available agents
      bernstein agents list --source local  # filter by source
      bernstein agents validate           # check catalog health
    """


@agents_group.command("sync")
@click.option(
    "--dir",
    "definitions_dir",
    default=".sdd/agents/definitions",
    show_default=True,
    help="Agent definitions directory.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-sync even if within the 24-hour TTL.",
)
def agents_sync(definitions_dir: str, force: bool) -> None:
    """Force-refresh all agent catalogs and update cache."""
    import asyncio

    from bernstein.agents.agency_provider import AgencyProvider
    from bernstein.agents.registry import AgentRegistry

    definitions_path = Path(definitions_dir)

    # Provider: local YAML definitions
    console.print("[bold]Syncing agent catalogs…[/bold]\n")
    console.print(f"[cyan]→ local[/cyan]  {definitions_path}")

    if not definitions_path.exists():
        console.print(f"  [yellow]Directory does not exist:[/yellow] {definitions_path}")
        console.print(f"  [dim]Create it with: mkdir -p {definitions_path}[/dim]")
    else:
        registry = AgentRegistry(definitions_dir=definitions_path)
        loaded = registry.load_definitions()
        console.print(f"  [green]✓[/green] Loaded {len(loaded)} agent definition(s)")
        for defn in loaded:
            console.print(f"    [dim]{defn.name}[/dim] v{defn.version} ({defn.role})")

    # Provider: agency catalog (legacy YAML format — .sdd/agents/agency/)
    agency_dir = Path(".sdd/agents/agency")
    console.print(f"\n[cyan]→ agency (local YAML)[/cyan] {agency_dir}")
    if not agency_dir.exists():
        console.print(f"  [dim]Directory not found — skipping (place Agency YAML files in {agency_dir})[/dim]")
    else:
        from bernstein.core.agency_loader import load_agency_catalog

        catalog = load_agency_catalog(agency_dir)
        console.print(f"  [green]✓[/green] Loaded {len(catalog)} agency agent(s)")
        for name in list(catalog)[:5]:
            agent = catalog[name]
            console.print(f"    [dim]{name}[/dim] ({agent.role})")
        if len(catalog) > 5:
            console.print(f"    [dim]… and {len(catalog) - 5} more[/dim]")

    # Provider: Agency GitHub repo (msitarzewski/agency-agents markdown format)
    default_agency_path = AgencyProvider.default_cache_path()
    console.print(f"\n[cyan]→ agency (GitHub)[/cyan] {default_agency_path}")
    ok, msg = AgencyProvider.sync_catalog(force=force)
    if ok:
        console.print(f"  [green]✓[/green] {msg}")
        provider = AgencyProvider(local_path=default_agency_path)
        agency_agents = asyncio.run(provider.fetch_agents())
        console.print(f"  [green]✓[/green] {len(agency_agents)} specialist agent(s) available")
        for a in agency_agents[:5]:
            caps = ", ".join(a.capabilities[:3]) if a.capabilities else "—"
            console.print(f"    [dim]{a.name}[/dim] ({a.role})  {caps}")
        if len(agency_agents) > 5:
            console.print(f"    [dim]… and {len(agency_agents) - 5} more[/dim]")
    else:
        console.print(f"  [yellow]![/yellow] {msg}")
        console.print(
            f"  [dim]Manual clone: git clone https://github.com/msitarzewski/agency-agents {default_agency_path}[/dim]"
        )

    console.print("\n[green]Sync complete.[/green]")


@agents_group.command("list")
@click.option(
    "--source",
    type=click.Choice(["local", "agency", "all"]),
    default="all",
    show_default=True,
    help="Filter agents by catalog source.",
)
@click.option(
    "--dir",
    "definitions_dir",
    default=".sdd/agents/definitions",
    show_default=True,
    help="Local agent definitions directory.",
)
def agents_list(source: str, definitions_dir: str) -> None:
    """List all available agents from loaded catalogs."""
    import asyncio

    from bernstein.agents.agency_provider import AgencyProvider
    from bernstein.agents.registry import AgentRegistry

    # rows: (id, name, role, capabilities, source)
    rows: list[tuple[str, str, str, str, str]] = []

    # Local definitions
    if source in ("local", "all"):
        definitions_path = Path(definitions_dir)
        if definitions_path.exists():
            registry = AgentRegistry(definitions_dir=definitions_path)
            registry.load_definitions()
            for defn in registry.definitions.values():
                rows.append((defn.name, defn.name, defn.role, "", "local"))

    # Agency catalog — legacy YAML format (.sdd/agents/agency/)
    if source in ("agency", "all"):
        agency_dir = Path(".sdd/agents/agency")
        if agency_dir.exists():
            from bernstein.core.agency_loader import load_agency_catalog

            catalog = load_agency_catalog(agency_dir)
            for name, agent in catalog.items():
                rows.append((name, agent.name, agent.role, "", "agency"))

    # Agency catalog — GitHub markdown format (~/.bernstein/catalogs/agency/)
    if source in ("agency", "all"):
        default_agency_path = AgencyProvider.default_cache_path()
        if default_agency_path.exists():
            provider = AgencyProvider(local_path=default_agency_path)
            agency_agents = asyncio.run(provider.fetch_agents())
            for a in agency_agents:
                caps = ", ".join(a.capabilities[:4]) if a.capabilities else ""
                rows.append((a.id or a.name, a.name, a.role, caps, "agency"))

    if not rows:
        console.print("[dim]No agents found. Run [bold]bernstein agents sync[/bold] first.[/dim]")
        return

    from rich.table import Table

    table = Table(
        title="Available Agents",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("NAME", style="dim", min_width=22)
    table.add_column("ROLE", min_width=12)
    table.add_column("CAPABILITIES", min_width=32)
    table.add_column("SOURCE", min_width=8)

    source_order = {"agency": 0, "local": 1}
    for _agent_id, name, role, caps, src in sorted(rows, key=lambda r: (source_order.get(r[4], 9), r[1])):
        src_color = "cyan" if src == "local" else "magenta"
        table.add_row(
            name,
            role,
            caps or "[dim]—[/dim]",
            f"[{src_color}]{src}[/{src_color}]",
        )

    console.print(table)
    console.print(f"\n[dim]{len(rows)} agent(s) total[/dim]")


@agents_group.command("validate")
@click.option(
    "--dir",
    "definitions_dir",
    default=".sdd/agents/definitions",
    show_default=True,
    help="Local agent definitions directory.",
)
def agents_validate(definitions_dir: str) -> None:
    """Validate all agent catalogs and report issues.

    Exits with code 1 if any provider is unreachable or has invalid agents.
    """
    import yaml

    from bernstein.agents.registry import SchemaValidationError

    definitions_path = Path(definitions_dir)
    issues: list[str] = []

    console.print("[bold]Validating agent catalogs…[/bold]\n")

    # --- Local definitions ---
    console.print(f"[cyan]→ local[/cyan]  {definitions_path}")
    if not definitions_path.exists():
        issues.append(f"local: definitions directory not found: {definitions_path}")
        console.print(f"  [red]✗[/red] Directory not found: {definitions_path}")
    else:
        yaml_files = list(definitions_path.glob("*.yaml")) + list(definitions_path.glob("*.yml"))
        if not yaml_files:
            console.print("  [dim]No YAML files found — catalog is empty[/dim]")
        for yaml_file in sorted(yaml_files):
            try:
                content = yaml_file.read_text(encoding="utf-8")
                data = yaml.safe_load(content)
                if not isinstance(data, dict):
                    raise ValueError("YAML must be a mapping")
                from bernstein.agents.registry import AgentRegistry

                registry = AgentRegistry(definitions_dir=definitions_path)
                registry._validate_schema(cast("dict[str, Any]", data), yaml_file)  # type: ignore[reportPrivateUsage]
                console.print(f"  [green]✓[/green] {yaml_file.name}")
            except SchemaValidationError as exc:
                issues.append(f"local/{yaml_file.name}: {exc}")
                console.print(f"  [red]✗[/red] {yaml_file.name}: {exc}")
            except Exception as exc:
                issues.append(f"local/{yaml_file.name}: {exc}")
                console.print(f"  [red]✗[/red] {yaml_file.name}: {exc}")

    # --- Agency catalog ---
    agency_dir = Path(".sdd/agents/agency")
    console.print(f"\n[cyan]→ agency[/cyan] {agency_dir}")
    if not agency_dir.exists():
        console.print("  [dim]Not configured — skipping[/dim]")
    else:
        from bernstein.core.agency_loader import parse_agency_agent

        agency_files = [p for p in sorted(agency_dir.iterdir()) if p.suffix in (".yaml", ".yml")]
        if not agency_files:
            console.print("  [dim]No YAML files found — catalog is empty[/dim]")
        for p in agency_files:
            try:
                parse_agency_agent(p)
                console.print(f"  [green]✓[/green] {p.name}")
            except ValueError as exc:
                issues.append(f"agency/{p.name}: {exc}")
                console.print(f"  [red]✗[/red] {p.name}: {exc}")

    # --- Summary ---
    console.print()
    if issues:
        console.print(f"[red]Validation failed: {len(issues)} issue(s)[/red]")
        for issue in issues:
            console.print(f"  [red]•[/red] {issue}")
        raise SystemExit(1)
    else:
        console.print("[green]All catalogs valid.[/green]")


@agents_group.command("showcase")
@click.option(
    "--dir",
    "definitions_dir",
    default=".sdd/agents/definitions",
    show_default=True,
    help="Local agent definitions directory.",
)
def agents_showcase(definitions_dir: str) -> None:
    """Rich display of available agents grouped by role, with success rates.

    \b
    Shows:
      - All agents from loaded catalogs, grouped by role / division
      - Per-agent match count and success rate (from .sdd/agents/registry.json)
      - Featured agents with the highest success rates
    """
    from rich.table import Table

    from bernstein.agents.discovery import AgentDiscovery

    # Load success metrics from registry
    discovery = AgentDiscovery.load()
    metrics = discovery.metrics

    rows: list[tuple[str, str, str, str, str, str]] = []

    # Local definitions
    definitions_path = Path(definitions_dir)
    if definitions_path.exists():
        from bernstein.agents.registry import AgentRegistry

        registry = AgentRegistry(definitions_dir=definitions_path)
        registry.load_definitions()
        for defn in registry.definitions.values():
            m = metrics.get("local")
            rate = f"{m.success_rate * 100:.0f}%" if m and m.tasks_assigned else "—"
            assigned = str(m.tasks_assigned) if m else "0"
            rows.append((defn.name, defn.role, defn.description[:60], "local", assigned, rate))

    # Agency catalog
    agency_dir = Path(".sdd/agents/agency")
    if agency_dir.exists():
        from bernstein.core.agency_loader import load_agency_catalog

        catalog = load_agency_catalog(agency_dir)
        for name, agent in catalog.items():
            m = metrics.get("agency")
            rate = f"{m.success_rate * 100:.0f}%" if m and m.tasks_assigned else "—"
            assigned = str(m.tasks_assigned) if m else "0"
            rows.append((name, agent.role, agent.description[:60], "agency", assigned, rate))

    # Built-in roles (fallback)
    from bernstein.agents.catalog import _BUILTIN_AGENT_ENTRIES  # type: ignore[reportPrivateUsage]

    builtin_names = {r[0] for r in rows}
    for entry in _BUILTIN_AGENT_ENTRIES:
        if entry["role"] not in builtin_names:
            m = metrics.get("builtin")
            rate = f"{m.success_rate * 100:.0f}%" if m and m.tasks_assigned else "—"
            assigned = str(m.tasks_assigned) if m else "0"
            rows.append(
                (
                    entry["role"],
                    entry["role"],
                    entry.get("description", ""),
                    "builtin",
                    assigned,
                    rate,
                )
            )

    if not rows:
        console.print("[dim]No agents found. Run [bold]bernstein agents sync[/bold] first.[/dim]")
        return

    # Sort by source priority then role
    source_order = {"agency": 0, "local": 1, "builtin": 2}
    rows.sort(key=lambda r: (source_order.get(r[3], 9), r[1], r[0]))

    # Identify "featured" agents — top success rates with ≥3 tasks
    top_sources = {m.source for m in discovery.top_sources(min_tasks=3)}

    table = Table(
        title="Agent Showcase",
        show_lines=False,
        header_style="bold cyan",
        expand=False,
    )
    table.add_column("Name", min_width=22)
    table.add_column("Role", min_width=14)
    table.add_column("Description", min_width=40)
    table.add_column("Source", min_width=8)
    table.add_column("Tasks", min_width=6, justify="right")
    table.add_column("Success", min_width=8, justify="right")

    for name, role, desc, src, assigned, rate in rows:
        src_color = {"agency": "magenta", "local": "cyan", "builtin": "dim"}.get(src, "white")
        star = " ★" if src in top_sources else ""
        name_text = f"[bold]{name}[/bold]{star}" if star else name
        table.add_row(
            name_text,
            role,
            desc or "[dim]—[/dim]",
            f"[{src_color}]{src}[/{src_color}]",
            assigned,
            rate,
        )

    console.print(table)

    # Summary line
    total = discovery.total_agents or len(rows)
    console.print(f"\n[dim]{len(rows)} agent(s) shown · {total} total across all directories[/dim]")
    if top_sources:
        console.print(f"[dim]★ Featured sources (≥3 tasks, highest success): {', '.join(sorted(top_sources))}[/dim]")

    # Discovery hints
    console.print()
    console.print("[dim]Discover more agents:[/dim]")
    console.print("[dim]  bernstein agents discover         # scan local + project dirs[/dim]")
    console.print("[dim]  bernstein agents discover --net   # also search GitHub & npm[/dim]")


@agents_group.command("match")
@click.option("--role", required=True, help="Agent role to match (e.g. security, backend, qa).")
@click.option("--task", "task_description", default="", help="Task description for fuzzy matching.")
def agents_match(role: str, task_description: str) -> None:
    """Show which agent would be selected for a given role.

    \b
    Example:
      bernstein agents match --role security
      bernstein agents match --role backend --task "add rate limiting middleware"
    """
    from bernstein.agents.catalog import CatalogRegistry

    # Load from agency catalog if available
    registry = CatalogRegistry.default()
    agency_dir = Path(".sdd/agents/agency")
    if agency_dir.exists():
        from bernstein.core.agency_loader import load_agency_catalog

        catalog = load_agency_catalog(agency_dir)
        registry.load_from_agency(catalog)

    match = registry.match(role, task_description)
    if match is None:
        console.print(f"[yellow]No catalog agent found for role '[bold]{role}[/bold]'.[/yellow]")
        console.print("[dim]Built-in role template will be used.[/dim]")
        return

    from rich.panel import Panel
    from rich.text import Text as RichText

    t = RichText()
    t.append("  Role      ", style="dim")
    t.append(f"{match.role}\n", style="bold")
    t.append("  Name      ", style="dim")
    t.append(f"{match.name}\n", style="bold cyan")
    t.append("  ID        ", style="dim")
    t.append(f"{match.id or '—'}\n")
    t.append("  Source    ", style="dim")
    t.append(f"{match.source}\n")
    t.append("  Priority  ", style="dim")
    t.append(f"{match.priority}\n")
    t.append("  Tools     ", style="dim")
    t.append(", ".join(match.tools) if match.tools else "—")
    t.append("\n\n")
    t.append("  Description\n", style="dim")
    t.append(f"    {match.description[:120]}\n")

    console.print(Panel(t, title=f"[bold]Agent match: {role}[/bold]", border_style="cyan"))


@agents_group.command("discover")
@click.option("--net", "include_network", is_flag=True, default=False, help="Also search GitHub and npm.")
def agents_discover(include_network: bool) -> None:
    """Scan known sources for agent directories and update the registry.

    \b
    Scans:
      ~/.bernstein/agents/     user-level definitions
      .sdd/agents/local/       project-level definitions
      GitHub (--net)           repos tagged bernstein-agents
      npm (--net)              packages with bernstein-agent keyword
    """
    from bernstein.agents.discovery import AgentDiscovery

    discovery = AgentDiscovery.load()

    console.print("[bold]Discovering agent directories…[/bold]\n")
    results = discovery.full_sync(include_network=include_network)

    for source, count in results.items():
        icon = "[green]✓[/green]" if count >= 0 else "[yellow]![/yellow]"
        console.print(f"  {icon} [cyan]{source}[/cyan]  {count} agent(s)")

    if include_network:
        gh_entries = [d for d in discovery.directories if d.source_type == "github"]
        npm_entries = [d for d in discovery.directories if d.source_type == "npm"]
        if gh_entries:
            console.print(f"\n  [magenta]GitHub[/magenta] ({len(gh_entries)} repos)")
            for e in gh_entries[:5]:
                console.print(f"    [dim]{e.name}[/dim]  {e.url}")
        if npm_entries:
            console.print(f"\n  [magenta]npm[/magenta] ({len(npm_entries)} packages)")
            for e in npm_entries[:5]:
                console.print(f"    [dim]{e.name}[/dim]  {e.url}")

    console.print(f"\n[green]Done.[/green] Registry: [dim]{discovery.registry_path}[/dim]")
    console.print(f"[dim]Total agents tracked: {discovery.total_agents}[/dim]")


# ---------------------------------------------------------------------------
# help-all — progressive disclosure: full flag list
# ---------------------------------------------------------------------------


@cli.command("help-all", hidden=False)
@click.pass_context
def help_all(ctx: click.Context) -> None:
    """Show all options including advanced flags.

    \b
    Advanced flags (hidden from default --help):
      --evolve / -e             Continuous self-improvement mode
      --max-cycles N            Stop after N evolve cycles (default: unlimited)
      --budget N                Stop after $N spent (default: unlimited)
      --interval N              Seconds between evolve cycles (default: 300)
      --github                  Sync evolve proposals as GitHub Issues
      --headless                Run without TUI dashboard (for CI/overnight)
      --yes / -y                Skip cost confirmation prompt

    \b
    All subcommands:
      bernstein status          Task summary and active agents
      bernstein stop            Stop all agents and server
      bernstein doctor          Run self-diagnostics
      bernstein recap           Post-run summary (tasks, cost, duration)
      bernstein cost            Detailed spend report by model
      bernstein plan            Show full task backlog
      bernstein logs [-f]       Tail agent log output
      bernstein cancel TASK_ID  Cancel a task
      bernstein demo            Zero-to-running demo project
      bernstein retro           Generate retrospective report
      bernstein evolve          Manage self-evolution proposals
      bernstein agents          Manage agent catalogs
      bernstein benchmark       Run the golden benchmark suite
    """
    console.print(ctx.get_help())


# ---------------------------------------------------------------------------
# Backward-compatible aliases (old names still work)
# ---------------------------------------------------------------------------

# Backward-compat aliases — register the decorated Click Command objects directly
# so all options and parameters are preserved.
cli.add_command(init, "init")
cli.add_command(run, "run")
cli.add_command(start, "start")
cli.add_command(status, "status")
cli.add_command(stop, "rest")
cli.add_command(add_task, "add-task")
cli.add_command(_notes_legacy, "logs-legacy")
cli.add_command(list_tasks, "list-tasks")


# ---------------------------------------------------------------------------
# evolve  — manage evolution proposals
# ---------------------------------------------------------------------------


@cli.group("evolve")
def evolve() -> None:
    """Manage self-evolution proposals.

    \b
      bernstein evolve review           # list proposals pending human review
      bernstein evolve approve <id>     # approve a specific proposal
      bernstein evolve run              # run the autoresearch evolution loop
      bernstein evolve status           # show evolution history table
      bernstein evolve export [path]    # export HTML/Markdown report
    """


@evolve.command("run")
@click.option(
    "--window",
    default="2h",
    show_default=True,
    help="Evolution window duration (e.g. 2h, 30m, 1h30m).",
)
@click.option(
    "--max-proposals",
    default=24,
    show_default=True,
    help="Maximum proposals to evaluate per session.",
)
@click.option(
    "--cycle",
    default=300,
    show_default=True,
    help="Seconds per experiment cycle (default 300 = 5 min).",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
@click.option(
    "--github",
    "github_sync",
    is_flag=True,
    default=False,
    help="Sync proposals as GitHub Issues for distributed coordination.",
)
@click.option(
    "--github-repo",
    default=None,
    help="GitHub repo slug (owner/repo). Inferred from git remote if omitted.",
)
def evolve_run(
    window: str,
    max_proposals: int,
    cycle: int,
    workdir: str,
    github_sync: bool,
    github_repo: str | None,
) -> None:
    """Run the autoresearch evolution loop.

    \b
    Runs time-boxed experiment cycles that:
    1. Analyze metrics and detect improvement opportunities
    2. Generate low-risk proposals (L0/L1 only)
    3. Sandbox validate each proposal
    4. Auto-apply improvements that pass validation
    5. Log all results to .sdd/evolution/experiments.jsonl

    L2+ proposals are saved to .sdd/evolution/deferred.jsonl for human review.

    When --github is set, each proposal is published as a GitHub Issue with
    label ``bernstein-evolve``.  Multiple instances running concurrently will
    claim different issues, preventing duplicate work.

    \b
      bernstein evolve run                         # default: 2h window, 24 proposals
      bernstein evolve run --window 30m            # short session
      bernstein evolve run --max-proposals 48      # more experiments
      bernstein evolve run --github                # sync proposals to GitHub Issues
      bernstein evolve run --github --github-repo owner/myrepo
    """
    from bernstein.evolution.loop import EvolutionLoop

    root = Path(workdir).resolve()
    state_dir = root / ".sdd"

    if not state_dir.is_dir():
        console.print(
            "[red].sdd directory not found.[/red] Run [bold]bernstein[/bold] first to initialise the workspace."
        )
        raise SystemExit(1)

    # Read evolve.github_sync / evolve.github_repo from bernstein.yaml if present
    # and the flags were not set on the CLI.
    for _seed_name in ("bernstein.yaml", "bernstein.yml"):
        _seed_path = root / _seed_name
        if _seed_path.exists():
            try:
                import yaml as _yaml

                _seed_raw = _yaml.safe_load(_seed_path.read_text(encoding="utf-8"))
                if isinstance(_seed_raw, dict):
                    _seed_dict = cast("dict[str, Any]", _seed_raw)
                    _evolve_cfg = _seed_dict.get("evolve", {})
                    if isinstance(_evolve_cfg, dict):
                        _evolve_dict = cast("dict[str, Any]", _evolve_cfg)
                        if not github_sync and _evolve_dict.get("github_sync"):
                            github_sync = True
                        if github_repo is None and _evolve_dict.get("github_repo"):
                            github_repo = str(_evolve_dict["github_repo"])
            except Exception:
                pass  # YAML parse errors are non-fatal here
            break

    # Parse window duration string (e.g. "2h", "30m", "1h30m").
    window_seconds = _parse_duration(window)
    if window_seconds <= 0:
        console.print(f"[red]Invalid window duration:[/red] {window}")
        raise SystemExit(1)

    # Check GitHub availability early so we can warn before the loop starts.
    if github_sync:
        from bernstein.core.github import GitHubClient

        _gh_check = GitHubClient(repo=github_repo)
        if not _gh_check.available:
            console.print(
                "[yellow]Warning:[/yellow] --github requested but [bold]gh[/bold] CLI "
                "is not available or not authenticated.\n"
                "GitHub sync will be skipped. Run [bold]gh auth login[/bold] to enable it."
            )
            github_sync = False

    console.print(
        f"[bold]Evolution loop starting[/bold]\n"
        f"  Window:     {window} ({window_seconds}s)\n"
        f"  Max props:  {max_proposals}\n"
        f"  Cycle:      {cycle}s\n"
        f"  State dir:  {state_dir}\n"
        + (f"  GitHub:     {'enabled' if github_sync else 'disabled'}\n" if github_sync else "")
    )

    loop = EvolutionLoop(
        state_dir=state_dir,
        repo_root=root,
        cycle_seconds=cycle,
        max_proposals=max_proposals,
        window_seconds=window_seconds,
        github_sync=github_sync,
    )
    if github_sync and github_repo:
        # Pass the explicit repo slug to the lazily-created GitHubClient.
        from bernstein.core.github import GitHubClient

        loop._github = GitHubClient(repo=github_repo)  # type: ignore[reportPrivateUsage]

    try:
        results = loop.run(
            window_seconds=window_seconds,
            max_proposals=max_proposals,
        )
    except KeyboardInterrupt:
        loop.stop()
        results = loop._experiments  # type: ignore[reportPrivateUsage]
        console.print("\n[dim]Evolution loop interrupted.[/dim]")

    # Print summary.
    summary = loop.get_summary()
    console.print(
        f"\n[bold]Evolution complete[/bold]\n"
        f"  Experiments:  {summary['experiments_run']}\n"
        f"  Accepted:     {summary['proposals_accepted']}\n"
        f"  Rate:         {summary['acceptance_rate']:.0%}\n"
        f"  Cost:         ${summary['total_cost_usd']:.4f}\n"
        f"  Elapsed:      {summary['elapsed_seconds']:.0f}s\n"
    )

    if results:
        from rich.table import Table

        result_table = Table(
            title="Experiment Results",
            show_lines=False,
            header_style="bold cyan",
        )
        result_table.add_column("Proposal", min_width=12)
        result_table.add_column("Title", min_width=30)
        result_table.add_column("Risk", min_width=8)
        result_table.add_column("Delta", justify="right", min_width=8)
        result_table.add_column("Result", min_width=10)

        for r in results:
            color = "green" if r.accepted else "red"
            delta_str = f"{r.delta:+.3f}" if r.delta != 0 else "—"
            result_table.add_row(
                r.proposal_id,
                r.title,
                r.risk_level,
                delta_str,
                f"[{color}]{'accepted' if r.accepted else 'rejected'}[/{color}]",
            )
        console.print(result_table)


def _parse_duration(s: str) -> int:
    """Parse a duration string like '2h', '30m', '1h30m' into seconds."""
    import re as _re

    total = 0
    for match in _re.finditer(r"(\d+)\s*(h|m|s)", s.lower()):
        value = int(match.group(1))
        unit = match.group(2)
        if unit == "h":
            total += value * 3600
        elif unit == "m":
            total += value * 60
        elif unit == "s":
            total += value

    if total == 0:
        try:
            total = int(s)
        except ValueError:
            return 0
    return total


@evolve.command("review")
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def evolve_review(workdir: str) -> None:
    """Show upgrade proposals pending human review."""
    from bernstein.evolution.gate import ApprovalGate

    root = Path(workdir).resolve()
    decisions_dir = root / ".sdd" / "evolution"
    gate = ApprovalGate(decisions_dir=decisions_dir)
    pending = gate.get_pending_decisions()

    if not pending:
        console.print("[dim]No proposals pending review.[/dim]")
        return

    from rich.table import Table

    review_table = Table(title="Proposals Pending Review", show_lines=True, header_style="bold cyan")
    review_table.add_column("ID", style="dim", min_width=12)
    review_table.add_column("Risk", min_width=12)
    review_table.add_column("Confidence", justify="right", min_width=10)
    review_table.add_column("Outcome", min_width=22)
    review_table.add_column("Reason")

    for d in sorted(pending, key=lambda x: x.decided_at):
        outcome_color = "red" if "immediate" in d.outcome.value else "yellow"
        review_table.add_row(
            d.proposal_id,
            d.risk_level.value,
            f"{d.confidence:.0%}",
            f"[{outcome_color}]{d.outcome.value}[/{outcome_color}]",
            d.reason,
        )

    console.print(review_table)
    console.print("\n[dim]Approve with:[/dim] [bold]bernstein evolve approve <id>[/bold]")


@evolve.command("approve")
@click.argument("proposal_id")
@click.option(
    "--reviewer",
    default="human",
    show_default=True,
    help="Name of the approver.",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def evolve_approve(proposal_id: str, reviewer: str, workdir: str) -> None:
    """Approve an upgrade proposal by ID."""
    from bernstein.evolution.gate import ApprovalGate

    root = Path(workdir).resolve()
    decisions_dir = root / ".sdd" / "evolution"
    gate = ApprovalGate(decisions_dir=decisions_dir)
    decision = gate.approve(proposal_id, reviewer=reviewer)

    if decision is None:
        console.print(
            f"[red]No pending proposal found:[/red] {proposal_id}\n"
            "Run [bold]bernstein evolve review[/bold] to list pending proposals."
        )
        raise SystemExit(1)

    console.print(f"[green]Approved:[/green] [bold]{proposal_id}[/bold] (reviewer={reviewer})")


@evolve.command("status")
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def evolve_status(workdir: str) -> None:
    """Show evolution history as a rich table.

    Reads .sdd/metrics/evolve_cycles.jsonl and .sdd/evolution/experiments.jsonl
    and displays a per-cycle breakdown with cumulative improvement metrics.

    \b
      bernstein evolve status           # history from current directory
      bernstein evolve status --dir /path/to/project
    """
    from bernstein.evolution.report import EvolutionReport

    root = Path(workdir).resolve()
    state_dir = root / ".sdd"

    if not state_dir.is_dir():
        console.print(
            "[red].sdd directory not found.[/red] Run [bold]bernstein[/bold] first to initialise the workspace."
        )
        raise SystemExit(1)

    report = EvolutionReport(state_dir=state_dir)
    report.load()
    report.print_status()


@evolve.command("export")
@click.argument("output", default="evolution_report", required=False)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["html", "md", "markdown"], case_sensitive=False),
    default="html",
    show_default=True,
    help="Output format: html or md/markdown.",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def evolve_export(output: str, fmt: str, workdir: str) -> None:
    """Export a static evolution report (HTML or Markdown).

    OUTPUT is the output file path (without extension). Defaults to
    'evolution_report' in the current directory.

    \b
      bernstein evolve export                        # evolution_report.html
      bernstein evolve export --format md            # evolution_report.md
      bernstein evolve export docs/evolution         # docs/evolution.html
    """
    from bernstein.evolution.report import EvolutionReport

    root = Path(workdir).resolve()
    state_dir = root / ".sdd"

    if not state_dir.is_dir():
        console.print(
            "[red].sdd directory not found.[/red] Run [bold]bernstein[/bold] first to initialise the workspace."
        )
        raise SystemExit(1)

    report = EvolutionReport(state_dir=state_dir)
    report.load()

    if not report.cycles:
        console.print("[dim]No evolution data found to export.[/dim]")
        raise SystemExit(1)

    is_markdown = fmt.lower() in ("md", "markdown")
    ext = ".md" if is_markdown else ".html"
    out_path = Path(output)
    if out_path.suffix.lower() not in (".html", ".md"):
        out_path = out_path.with_suffix(ext)

    if is_markdown:
        report.export_markdown(out_path)
    else:
        report.export_html(out_path)

    console.print(
        f"[green]Report written:[/green] {out_path} "
        f"({report.total_cycles} cycles, {report.total_tasks_completed} tasks completed)"
    )


# ---------------------------------------------------------------------------
# ideate — creative evolution pipeline
# ---------------------------------------------------------------------------


@cli.command("ideate")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show proposals without creating backlog tasks.",
)
@click.option(
    "--proposals",
    type=click.Path(exists=True),
    default=None,
    help="JSON file with pre-written visionary proposals (skip agent stage).",
)
@click.option(
    "--verdicts",
    type=click.Path(exists=True),
    default=None,
    help="JSON file with pre-written analyst verdicts (skip agent stage).",
)
@click.option(
    "--threshold",
    type=float,
    default=7.0,
    show_default=True,
    help="Minimum composite score for approval.",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def ideate(
    dry_run: bool,
    proposals: str | None,
    verdicts: str | None,
    threshold: float,
    workdir: str,
) -> None:
    """Run the creative evolution pipeline (visionary -> analyst -> tasks).

    \b
    Generates bold feature ideas, evaluates them ruthlessly, and converts
    approved proposals into backlog tasks. Requires pre-written proposal
    and verdict JSON files (agent-driven generation is a future feature).

    \b
      bernstein ideate --proposals ideas.json --verdicts evals.json
      bernstein ideate --proposals ideas.json --verdicts evals.json --dry-run
      bernstein ideate --proposals ideas.json --verdicts evals.json --threshold 8
    """
    from bernstein.evolution.creative import (
        AnalystVerdict,
        CreativePipeline,
        VisionaryProposal,
    )

    root = Path(workdir).resolve()
    state_dir = root / ".sdd"

    if not state_dir.is_dir():
        console.print(
            "[red].sdd directory not found.[/red] Run [bold]bernstein[/bold] first to initialise the workspace."
        )
        raise SystemExit(1)

    # Load proposals.
    proposal_list: list[VisionaryProposal] = []
    if proposals:
        try:
            raw = json.loads(Path(proposals).read_text(encoding="utf-8"))
            proposal_list = [VisionaryProposal.from_dict(p) for p in raw]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            console.print(f"[red]Failed to load proposals:[/red] {exc}")
            raise SystemExit(1) from exc
        console.print(f"Loaded [bold]{len(proposal_list)}[/bold] proposal(s)")

    # Load verdicts.
    verdict_list: list[AnalystVerdict] = []
    if verdicts:
        try:
            raw = json.loads(Path(verdicts).read_text(encoding="utf-8"))
            verdict_list = [AnalystVerdict.from_dict(v) for v in raw]
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            console.print(f"[red]Failed to load verdicts:[/red] {exc}")
            raise SystemExit(1) from exc
        console.print(f"Loaded [bold]{len(verdict_list)}[/bold] verdict(s)")

    if not proposal_list or not verdict_list:
        console.print(
            "[yellow]Both --proposals and --verdicts are required.[/yellow]\n\n"
            "Provide JSON files with visionary proposals and analyst verdicts.\n"
            "See templates/roles/visionary/ and templates/roles/analyst/ for output formats."
        )
        raise SystemExit(1)

    pipeline = CreativePipeline(
        state_dir=state_dir,
        repo_root=root,
        approval_threshold=threshold,
    )

    result = pipeline.run(proposal_list, verdict_list, dry_run=dry_run)

    # Print results.
    if dry_run:
        console.print("\n[bold cyan][DRY RUN][/bold cyan] No backlog tasks created.\n")

    from rich.table import Table

    table = Table(
        title="Creative Pipeline Results",
        show_lines=True,
        header_style="bold cyan",
    )
    table.add_column("Proposal", min_width=25)
    table.add_column("Verdict", min_width=8)
    table.add_column("Feas.", justify="right", min_width=5)
    table.add_column("Impact", justify="right", min_width=6)
    table.add_column("Risk", justify="right", min_width=5)
    table.add_column("Score", justify="right", min_width=6)

    for v in result.verdicts:
        verdict_color = {
            "APPROVE": "green",
            "REVISE": "yellow",
            "REJECT": "red",
        }.get(v.verdict, "white")
        table.add_row(
            v.proposal_title,
            f"[{verdict_color}]{v.verdict}[/{verdict_color}]",
            f"{v.feasibility_score:.0f}",
            f"{v.impact_score:.0f}",
            f"{v.risk_score:.0f}",
            f"{v.composite_score:.1f}",
        )

    console.print(table)
    console.print(
        f"\n  Proposals: {len(result.proposals)}"
        f"  |  Approved: {len(result.approved)}"
        f"  |  Tasks created: {len(result.tasks_created)}"
    )


# ---------------------------------------------------------------------------
# doctor — self-diagnostic
# ---------------------------------------------------------------------------


@cli.command("doctor")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def doctor(as_json: bool) -> None:
    """Run self-diagnostics: check Python, adapters, API keys, port, and workspace.

    \b
      bernstein doctor          # print diagnostic report
      bernstein doctor --json   # machine-readable output
    """
    import shutil
    import socket

    checks: list[dict[str, Any]] = []

    def _check(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    # 1. Python version
    major, minor = sys.version_info.major, sys.version_info.minor
    py_ok = (major, minor) >= (3, 12)
    _check(
        "Python version",
        py_ok,
        f"Python {major}.{minor} (need 3.12+)",
        "Install Python 3.12 or newer" if not py_ok else "",
    )

    # 2. CLI adapters
    adapters = {
        "claude": "ANTHROPIC_API_KEY",
        "codex": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    any_adapter = False
    for adapter_name, _env_var in adapters.items():
        found = shutil.which(adapter_name) is not None
        if found:
            any_adapter = True
        _check(
            f"Adapter: {adapter_name}",
            found,
            "found in PATH" if found else "not in PATH",
            f"Install {adapter_name} CLI — see docs" if not found else "",
        )

    # 3. API keys (Claude Code supports OAuth — API key optional)
    key_vars = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"]
    any_key = False
    for var in key_vars:
        set_val = bool(os.environ.get(var))
        if set_val:
            any_key = True
        hint = ""
        status = "set" if set_val else "not set"
        if var == "ANTHROPIC_API_KEY" and not set_val:
            # Check for OAuth session
            from bernstein.core.bootstrap import _claude_has_oauth_session  # type: ignore[reportPrivateUsage]

            if _claude_has_oauth_session():
                status = "not set (OAuth active — OK)"
                any_key = True
                set_val = True
            else:
                hint = "export ANTHROPIC_API_KEY=key or: claude login"
        elif not set_val:
            hint = f"export {var}=your-key"
        _check(f"Env: {var}", set_val, status, hint)

    # 4. Port 8052 availability
    port = 8052
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            result = s.connect_ex(("127.0.0.1", port))
            port_in_use = result == 0
    except Exception:
        port_in_use = False
    _check(
        f"Port {port}",
        not port_in_use,
        "in use — server may already be running" if port_in_use else "available",
        "Run 'bernstein stop' to free the port" if port_in_use else "",
    )

    # 5. .sdd/ structure
    workdir = Path.cwd()
    required_dirs = [".sdd", ".sdd/backlog", ".sdd/runtime"]
    sdd_ok = all((workdir / d).exists() for d in required_dirs)
    _check(
        ".sdd workspace",
        sdd_ok,
        "present" if sdd_ok else "missing or incomplete",
        "Run 'bernstein' or 'bernstein -g \"goal\"' to initialise" if not sdd_ok else "",
    )

    # 6. Stale PID files
    stale_pids: list[str] = []
    for pid_name in ("server.pid", "spawner.pid", "watchdog.pid"):
        pid_path = workdir / ".sdd" / "runtime" / pid_name
        if pid_path.exists():
            try:
                pid_val = int(pid_path.read_text().strip())
                try:
                    os.kill(pid_val, 0)
                except OSError:
                    stale_pids.append(pid_name)
            except ValueError:
                stale_pids.append(pid_name)
    _check(
        "Stale PID files",
        len(stale_pids) == 0,
        f"found: {', '.join(stale_pids)}" if stale_pids else "none",
        "Run 'bernstein stop' to clean up" if stale_pids else "",
    )

    # 7. Guardrail stats
    from bernstein.core.guardrails import get_guardrail_stats

    guardrail_stats = get_guardrail_stats(workdir)
    g_total = guardrail_stats["total"]
    g_blocked = guardrail_stats["blocked"]
    g_flagged = guardrail_stats["flagged"]
    if g_total > 0:
        g_detail = f"{g_total} checked, {g_blocked} blocked, {g_flagged} flagged"
    else:
        g_detail = "no events recorded yet"
    _check("Guardrails", True, g_detail)

    # 8. CI tool dependencies (ruff, pytest, pyright)
    from bernstein.core.ci_fix import check_test_dependencies

    ci_dep_results = check_test_dependencies()
    for dep in ci_dep_results:
        _check(
            f"CI tool: {dep['name']}",
            dep["ok"] == "True",
            dep["detail"],
            dep["fix"],
        )

    # 8. Storage backend connectivity
    storage_backend = os.environ.get("BERNSTEIN_STORAGE_BACKEND", "memory")
    if storage_backend == "memory":
        _check("Storage backend", True, "memory (default, no external dependencies)", "")
    elif storage_backend == "postgres":
        db_url = os.environ.get("BERNSTEIN_DATABASE_URL")
        if db_url:
            try:
                import asyncpg  # type: ignore[import-untyped]

                async def _check_pg() -> bool:
                    conn = await asyncpg.connect(db_url)  # type: ignore[reportUnknownVariableType,reportUnknownMemberType]
                    await conn.close()  # type: ignore[reportUnknownMemberType]
                    return True

                import asyncio

                asyncio.run(_check_pg())
                _check("Storage backend", True, f"postgres — connected ({db_url[:40]}...)", "")
            except ImportError:
                _check(
                    "Storage backend",
                    False,
                    "postgres — asyncpg not installed",
                    "pip install bernstein[postgres]",
                )
            except Exception as exc:
                _check(
                    "Storage backend",
                    False,
                    f"postgres — connection failed: {exc}",
                    "Check BERNSTEIN_DATABASE_URL and ensure PostgreSQL is running",
                )
        else:
            _check(
                "Storage backend",
                False,
                "postgres — BERNSTEIN_DATABASE_URL not set",
                "export BERNSTEIN_DATABASE_URL=postgresql://user:pass@localhost/bernstein",
            )
    elif storage_backend == "redis":
        db_url = os.environ.get("BERNSTEIN_DATABASE_URL")
        redis_url = os.environ.get("BERNSTEIN_REDIS_URL")
        storage_ok = True
        if not db_url:
            _check(
                "Storage backend (postgres)",
                False,
                "redis mode — BERNSTEIN_DATABASE_URL not set",
                "export BERNSTEIN_DATABASE_URL=postgresql://user:pass@localhost/bernstein",
            )
            storage_ok = False
        if not redis_url:
            _check(
                "Storage backend (redis)",
                False,
                "redis mode — BERNSTEIN_REDIS_URL not set",
                "export BERNSTEIN_REDIS_URL=redis://localhost:6379",
            )
            storage_ok = False
        if storage_ok:
            _check("Storage backend", True, "redis mode (pg + redis locking)", "")
    else:
        _check(
            "Storage backend",
            False,
            f"unknown backend: {storage_backend}",
            "Set BERNSTEIN_STORAGE_BACKEND to memory, postgres, or redis",
        )

    # 10. Overall readiness
    any_adapter_key = any_adapter and any_key
    _check(
        "Ready to run",
        py_ok and any_adapter_key,
        "yes" if (py_ok and any_adapter_key) else "missing adapter or API key",
        "Install an adapter (claude/codex/gemini) and set its API key" if not any_adapter_key else "",
    )

    if as_json:
        import json as _json

        click.echo(_json.dumps({"checks": checks}, indent=2))
        failed = [c for c in checks if not c["ok"]]
        if failed:
            raise SystemExit(1)
        return

    from rich.table import Table

    table = Table(title="Bernstein Doctor", header_style="bold cyan", show_lines=False)
    table.add_column("Check", min_width=22)
    table.add_column("Status", min_width=8)
    table.add_column("Detail", min_width=35)
    table.add_column("Fix")

    for c in checks:
        icon = "[green]✓[/green]" if c["ok"] else "[red]✗[/red]"
        table.add_row(
            c["name"],
            icon,
            c["detail"],
            f"[dim]{c['fix']}[/dim]" if c["fix"] else "",
        )

    console.print(table)

    failed_checks = [c for c in checks if not c["ok"]]
    if failed_checks:
        console.print(f"\n[red]{len(failed_checks)} issue(s) found.[/red]")
        raise SystemExit(1)
    else:
        console.print("\n[green]All checks passed.[/green]")


# ---------------------------------------------------------------------------
# install-hooks — git pre-push hook installer
# ---------------------------------------------------------------------------


@cli.command("install-hooks")
@click.option("--force", is_flag=True, default=False, help="Overwrite existing hook.")
def install_hooks(force: bool) -> None:
    """Install a pre-push git hook that runs lint and unit tests before pushing.

    \b
      bernstein install-hooks          # install hook (skip if already present)
      bernstein install-hooks --force  # overwrite existing hook
    """
    from bernstein.core.ci_fix import install_pre_push_hook

    repo_root = Path.cwd()
    installed = install_pre_push_hook(repo_root, force=force)
    hook_path = repo_root / ".git" / "hooks" / "pre-push"
    if installed:
        console.print(f"[green]Pre-push hook installed:[/green] {hook_path}")
        console.print("  Runs: ruff check + ruff format --check + pytest tests/unit/")
    else:
        console.print(f"[yellow]Hook already exists:[/yellow] {hook_path}")
        console.print("  Use --force to overwrite.")


# ---------------------------------------------------------------------------
# plugins — list discovered plugins and their hooks
# ---------------------------------------------------------------------------


@cli.command("plugins")
@click.option("--workdir", default=".", show_default=True, help="Project root to read bernstein.yaml from.")
def plugins_cmd(workdir: str) -> None:
    """List discovered plugins and the hooks they implement.

    \b
      bernstein plugins                 # discover plugins in current project
      bernstein plugins --workdir /srv  # use a different project root
    """
    from rich.table import Table

    from bernstein.plugins.manager import PluginManager

    pm = PluginManager()
    pm.load_from_workdir(Path(workdir))

    names = pm.registered_names
    if not names:
        console.print("[dim]No plugins discovered.[/dim]")
        console.print(
            "[dim]Register plugins via entry_points([/dim][cyan]'bernstein.plugins'[/cyan][dim]) "
            "or add a [/dim][cyan]plugins:[/cyan][dim] list to bernstein.yaml.[/dim]"
        )
        return

    table = Table(show_header=True, header_style="bold magenta", title="Bernstein Plugins")
    table.add_column("Plugin", style="cyan")
    table.add_column("Hooks implemented")

    for name in names:
        hooks = pm.plugin_hooks(name)
        hook_str = ", ".join(hooks) if hooks else "[dim]none[/dim]"
        table.add_row(name, hook_str)

    console.print(table)
    console.print(f"\n[dim]Total: {len(names)} plugin(s)[/dim]")


# ---------------------------------------------------------------------------
# recap — post-run summary
# ---------------------------------------------------------------------------


@cli.command("recap")
@click.option(
    "--archive",
    default=".sdd/archive/tasks.jsonl",
    show_default=True,
    hidden=True,
    help="Path to the task archive JSONL file.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def recap(archive: str, as_json: bool) -> None:
    """Print a one-line post-run summary: tasks, pass/fail, cost, and duration.

    \b
    Reads from .sdd/archive/tasks.jsonl and .sdd/metrics/.

    \b
      bernstein recap           # human-readable summary
      bernstein recap --json    # machine-readable output
    """
    import datetime

    archive_path = Path(archive)

    if not archive_path.exists():
        if as_json:
            click.echo(json.dumps({"error": f"Archive not found: {archive_path}"}))
        else:
            console.print(f"[yellow]No archive found:[/yellow] {archive_path}")
            console.print("[dim]Run 'bernstein' to start, then check again after tasks complete.[/dim]")
        raise SystemExit(0)

    records: list[dict[str, Any]] = []
    for line in archive_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not records:
        if as_json:
            click.echo(json.dumps({"tasks": 0, "done": 0, "failed": 0, "cost_usd": 0.0}))
        else:
            console.print("[dim]Archive is empty — no tasks have completed yet.[/dim]")
        return

    done = [r for r in records if r.get("status") == "done"]
    failed = [r for r in records if r.get("status") == "failed"]
    total = len(done) + len(failed)

    # Total cost
    cost_usd = sum(float(r.get("cost_usd") or 0.0) for r in records)

    # Also add cost from .sdd/metrics/tasks.jsonl if available
    metrics_path = archive_path.parent.parent / "metrics" / "tasks.jsonl"
    if metrics_path.exists():
        seen_tasks: set[str] = {r.get("task_id", "") for r in records}
        for line in metrics_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                tid = rec.get("task_id", "")
                if tid not in seen_tasks:
                    cost_usd += float(rec.get("cost_usd") or 0.0)
            except json.JSONDecodeError:
                continue

    # Time range
    timestamps: list[float] = []
    for r in records:
        _ts = r.get("created_at") or r.get("completed_at")
        if _ts is not None:
            timestamps.append(float(_ts))
    start_ts: float | None = min(timestamps) if timestamps else None
    _completed_ts: list[float] = []
    for r in records:
        _ct = r.get("completed_at")
        if _ct is not None:
            _completed_ts.append(float(_ct))
    end_ts: float | None = max(_completed_ts) if _completed_ts else None

    duration_s: float | None = None
    if start_ts is not None and end_ts is not None:
        duration_s = end_ts - start_ts

    if as_json:
        output: dict[str, Any] = {
            "tasks": total,
            "done": len(done),
            "failed": len(failed),
            "cost_usd": round(cost_usd, 6),
            "start_ts": start_ts,
            "end_ts": end_ts,
            "duration_s": duration_s,
        }
        click.echo(json.dumps(output, indent=2))
        return

    # Human-readable one-liner
    start_str = ""
    end_str = ""
    if start_ts:
        start_str = datetime.datetime.fromtimestamp(float(start_ts)).strftime("%H:%M")
    if end_ts:
        end_str = datetime.datetime.fromtimestamp(float(end_ts)).strftime("%H:%M")

    dur_str = ""
    if duration_s is not None:
        m, s = divmod(int(float(duration_s)), 60)
        dur_str = f" in {m}m{s:02d}s" if m else f" in {s}s"

    cost_str = f"${cost_usd:.2f}" if cost_usd > 0 else "$0.00"

    parts: list[str] = []
    if start_str and end_str:
        parts.append(f"{start_str} → {end_str}")
    parts.append(f"{total} task(s) total")
    parts.append(f"[green]{len(done)} done[/green]")
    if failed:
        parts.append(f"[red]{len(failed)} failed[/red]")
    if dur_str:
        parts.append(dur_str.strip())
    parts.append(f"[cyan]{cost_str}[/cyan] spent")

    console.print("  ".join(parts))


# ---------------------------------------------------------------------------
# trace — view agent decision trace for a task
# ---------------------------------------------------------------------------


@cli.command("trace")
@click.argument("task_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.option(
    "--traces-dir",
    default=".sdd/traces",
    show_default=True,
    hidden=True,
    help="Directory containing trace files.",
)
def trace_cmd(task_id: str, as_json: bool, traces_dir: str) -> None:
    """Show the step-by-step decision trace for TASK_ID.

    \b
    Each agent execution is recorded as a structured trace capturing:
      - Files read (orient), decisions made (plan), edits (edit), tests run (verify)
      - Model, effort, total duration, and outcome

    \b
      bernstein trace abc123          # Rich tree view
      bernstein trace abc123 --json   # Raw JSON
    """
    from bernstein.core.traces import TraceStore

    store = TraceStore(Path(traces_dir))
    traces = store.read_by_task(task_id)

    if not traces:
        # Also try treating task_id as a trace_id directly
        t = store.read_by_trace_id(task_id)
        if t is not None:
            traces = [t]

    if not traces:
        console.print(f"[yellow]No trace found for:[/yellow] {task_id}")
        console.print("[dim]Traces are written to .sdd/traces/ during agent runs.[/dim]")
        raise SystemExit(1)

    if as_json:
        click.echo(json.dumps([t.to_dict() for t in traces], indent=2))
        return

    from rich.panel import Panel
    from rich.tree import Tree

    for trace in traces:
        dur = ""
        if trace.duration_s is not None:
            m, s = divmod(int(trace.duration_s), 60)
            dur = f" • {m}m{s:02d}s" if m else f" • {s}s"

        outcome_style = {
            "success": "[green]success[/green]",
            "failed": "[red]failed[/red]",
            "unknown": "[yellow]unknown[/yellow]",
        }.get(trace.outcome, trace.outcome)

        header = f"[bold]{trace.agent_role}[/bold] agent [dim]{trace.model}/{trace.effort}[/dim]{dur} • {outcome_style}"
        tree = Tree(header)
        tree.add(f"[dim]trace_id:[/dim] {trace.trace_id}")
        tree.add(f"[dim]session:[/dim]  {trace.session_id}")
        tree.add(f"[dim]tasks:[/dim]    {', '.join(trace.task_ids)}")

        steps_node = tree.add(f"[bold cyan]steps ({len(trace.steps)})[/bold cyan]")

        STEP_STYLES: dict[str, str] = {
            "spawn": "dim",
            "orient": "blue",
            "plan": "yellow",
            "edit": "magenta",
            "verify": "cyan",
            "complete": "green",
            "fail": "red",
        }

        for step in trace.steps:
            style = STEP_STYLES.get(step.type, "white")

            # Build suffix: duration + tokens
            suffix_parts: list[str] = []
            if step.duration_ms > 0:
                if step.duration_ms >= 1000:
                    suffix_parts.append(f"[dim]{step.duration_ms // 1000}s[/dim]")
                else:
                    suffix_parts.append(f"[dim]{step.duration_ms}ms[/dim]")
            if step.tokens > 0:
                suffix_parts.append(f"[dim]{step.tokens:,} tok[/dim]")
            suffix = ("  " + "  ".join(suffix_parts)) if suffix_parts else ""

            label = f"[{style}]{step.type:8s}[/{style}]  {step.detail}{suffix}"
            snode = steps_node.add(label)
            if step.files:
                for f in step.files[:5]:
                    snode.add(f"[dim]{f}[/dim]")
                if len(step.files) > 5:
                    snode.add(f"[dim]… and {len(step.files) - 5} more[/dim]")

        # Token/cost summary across all steps
        total_tokens = sum(s.tokens for s in trace.steps)
        if total_tokens > 0:
            from bernstein.core.cost import _model_cost  # type: ignore[attr-defined]

            cost_per_1k = _model_cost(trace.model)
            est_cost = total_tokens / 1000 * cost_per_1k
            tree.add(f"[dim]tokens:[/dim]  {total_tokens:,}  [dim]est. cost:[/dim] [yellow]${est_cost:.4f}[/yellow]")

        console.print(Panel(tree, border_style="blue", expand=False))


# ---------------------------------------------------------------------------
# replay — re-run a task from a trace
# ---------------------------------------------------------------------------


@cli.command("replay")
@click.argument("trace_id")
@click.option("--model", default=None, help="Override model (opus/sonnet/haiku).")
@click.option("--effort", default=None, help="Override effort (max/high/medium/low).")
@click.option(
    "--traces-dir",
    default=".sdd/traces",
    show_default=True,
    hidden=True,
    help="Directory containing trace files.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would be replayed without actually spawning an agent.",
)
def replay_cmd(
    trace_id: str,
    model: str | None,
    effort: str | None,
    traces_dir: str,
    dry_run: bool,
) -> None:
    """Re-run a task from a previous trace.

    Loads the trace identified by TRACE_ID (or task ID), then re-submits
    the same task(s) to the task server so the orchestrator picks them up.

    \b
      bernstein replay abc123                # replay with same model
      bernstein replay abc123 --model opus   # retry with better model
      bernstein replay abc123 --dry-run      # preview without spawning
    """
    from bernstein.core.traces import TraceStore

    store = TraceStore(Path(traces_dir))

    # Try trace_id first, then as task_id
    trace = store.read_by_trace_id(trace_id)
    if trace is None:
        traces = store.read_by_task(trace_id)
        if traces:
            trace = traces[-1]  # most recent

    if trace is None:
        console.print(f"[red]Trace not found:[/red] {trace_id}")
        console.print("[dim]Use 'bernstein trace <task_id>' to list available traces.[/dim]")
        raise SystemExit(1)

    effective_model = model or trace.model
    effective_effort = effort or trace.effort

    console.print(f"[bold]Replaying trace[/bold] [cyan]{trace.trace_id}[/cyan]")
    console.print(
        f"  role:    {trace.agent_role}\n"
        f"  tasks:   {', '.join(trace.task_ids)}\n"
        f"  model:   [yellow]{effective_model}[/yellow] "
        f"{'(overridden)' if model else '(original)'}\n"
        f"  effort:  [yellow]{effective_effort}[/yellow] "
        f"{'(overridden)' if effort else '(original)'}"
    )

    if not trace.task_ids:
        console.print("[red]No task IDs found in trace — cannot replay.[/red]")
        raise SystemExit(1)

    if dry_run:
        console.print("\n[dim][dry-run] No tasks submitted.[/dim]")
        return

    # Re-submit tasks via the task server: re-open them if they exist,
    # or re-create from stored snapshots if available.
    submitted: list[str] = []
    errors: list[str] = []

    for task_id in trace.task_ids:
        # Find snapshot for this task (stored in trace at spawn time)
        snapshot = next(
            (s for s in trace.task_snapshots if s.get("id") == task_id),
            None,
        )

        # Try fetching current task from server to get fresh metadata
        current = _server_get(f"/tasks/{task_id}")
        if current is not None:
            # Re-create as a new task (copy title/description, use new model/effort)
            src = current
        elif snapshot is not None:
            src = snapshot
        else:
            errors.append(f"{task_id}: not found on server and no snapshot available")
            continue

        payload: dict[str, Any] = {
            "title": f"[replay] {src.get('title', task_id)}",
            "description": src.get("description", ""),
            "role": src.get("role", trace.agent_role),
            "priority": src.get("priority", 2),
            "scope": src.get("scope", "medium"),
            "complexity": src.get("complexity", "medium"),
            "model": effective_model,
            "effort": effective_effort,
        }
        resp = _server_post("/tasks", payload)
        if resp is not None:
            new_id = resp.get("id", "?")
            submitted.append(new_id)
        else:
            errors.append(f"{task_id}: failed to create replay task on server")

    if submitted:
        console.print(f"\n[green]Submitted {len(submitted)} task(s) for replay:[/green]")
        for tid in submitted:
            console.print(f"  [cyan]{tid}[/cyan]")
        console.print("[dim]Run 'bernstein run' to process them.[/dim]")

    for err in errors:
        console.print(f"[red]Error:[/red] {err}")

    if errors and not submitted:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# github — GitHub App integration commands
# ---------------------------------------------------------------------------


@cli.group("github")
def github_group() -> None:
    """GitHub App integration.

    \b
      bernstein github setup          # print setup instructions
      bernstein github test-webhook   # send a test webhook
    """


@github_group.command("setup")
def github_setup() -> None:
    """Print instructions for setting up the GitHub App."""
    console.print("\n[bold cyan]GitHub App Setup Instructions[/bold cyan]\n")
    console.print(
        "1. Go to https://github.com/settings/apps/new\n"
        "2. Fill in the App name (e.g. 'bernstein-orchestrator')\n"
        "3. Set the Webhook URL to your server's /webhooks/github endpoint\n"
        "   e.g. https://your-server.example.com/webhooks/github\n"
        "4. Generate a webhook secret and save it\n"
        "5. Under Permissions, grant:\n"
        "   - Issues: Read & Write\n"
        "   - Pull requests: Read & Write\n"
        "   - Contents: Read\n"
        "6. Subscribe to events:\n"
        "   - Issues\n"
        "   - Pull request\n"
        "   - Pull request review comment\n"
        "   - Push\n"
        "7. Create the App and note the App ID\n"
        "8. Generate a private key (PEM file)\n"
        "9. Install the App on your repository\n\n"
        "[bold]Set these environment variables:[/bold]\n\n"
        "  export GITHUB_APP_ID=<your-app-id>\n"
        "  export GITHUB_APP_PRIVATE_KEY=<path-to-pem-or-pem-string>\n"
        "  export GITHUB_WEBHOOK_SECRET=<your-webhook-secret>\n\n"
        "[dim]See deploy/github-app/README.md for detailed instructions.[/dim]"
    )


@github_group.command("test-webhook")
@click.option("--event", default="issues", show_default=True, help="GitHub event type to simulate.")
@click.option(
    "--server-url",
    default=None,
    help="Task server URL (default: $BERNSTEIN_SERVER_URL or http://localhost:8052).",
)
def github_test_webhook(event: str, server_url: str | None) -> None:
    """Send a test webhook to verify GitHub App configuration.

    Sends a synthetic webhook event to the server's /webhooks/github
    endpoint to verify that event parsing and task creation work.
    """
    import hashlib
    import hmac as hmac_mod

    url = server_url or SERVER_URL
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "test-secret")

    # Build a synthetic payload based on event type
    payloads: dict[str, dict[str, Any]] = {
        "issues": {
            "action": "opened",
            "issue": {
                "number": 9999,
                "title": "Test issue from bernstein github test-webhook",
                "body": "This is a synthetic test issue to verify webhook integration.",
                "labels": [{"name": "enhancement"}],
            },
            "repository": {"full_name": "test-owner/test-repo"},
            "sender": {"login": "bernstein-test"},
        },
        "push": {
            "ref": "refs/heads/main",
            "commits": [
                {
                    "id": "abc12345deadbeef",
                    "message": "test: synthetic push event",
                }
            ],
            "repository": {"full_name": "test-owner/test-repo"},
            "sender": {"login": "bernstein-test"},
        },
    }

    payload = payloads.get(event)
    if payload is None:
        console.print(f"[red]Unsupported test event type:[/red] {event}")
        console.print(f"[dim]Supported: {', '.join(payloads.keys())}[/dim]")
        raise SystemExit(1)

    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac_mod.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    console.print(f"[dim]Sending test {event} webhook to {url}/webhooks/github ...[/dim]")

    try:
        resp = httpx.post(
            f"{url}/webhooks/github",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": event,
                "X-Hub-Signature-256": sig,
            },
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            console.print(f"[green]Success![/green] Tasks created: {data.get('tasks_created', 0)}")
            for tid in data.get("task_ids", []):
                console.print(f"  [cyan]{tid}[/cyan]")
        else:
            console.print(f"[red]Failed:[/red] HTTP {resp.status_code} — {resp.text}")
            raise SystemExit(1)
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {url}[/red]")
        console.print("[dim]Is the Bernstein server running? Run 'bernstein' first.[/dim]")
        raise SystemExit(1) from None


# ---------------------------------------------------------------------------
# mcp — expose Bernstein as an MCP server
# ---------------------------------------------------------------------------


@cli.command("mcp")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse"]),
    default="stdio",
    show_default=True,
    help="MCP transport: stdio (local IDE) or sse (remote/web).",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="SSE server host (only used with --transport sse).",
)
@click.option(
    "--port",
    default=8053,
    show_default=True,
    help="SSE server port (only used with --transport sse).",
)
@click.option(
    "--server-url",
    default=SERVER_URL,
    show_default=True,
    help="Bernstein task server URL.",
)
def mcp_server(transport: str, host: str, port: int, server_url: str) -> None:
    """Start Bernstein as an MCP server.

    \b
    stdio transport (default) — for local IDE integration:
      bernstein mcp

    SSE transport — for remote/web clients:
      bernstein mcp --transport sse --port 8053

    Once running, any MCP client (Cursor, Claude Code, Cline, Windsurf)
    can call bernstein_run, bernstein_status, bernstein_tasks, and more.
    """
    from bernstein.mcp.server import run_sse, run_stdio

    if transport == "sse":
        console.print(f"[dim]Starting Bernstein MCP server (SSE) on {host}:{port} ...[/dim]")
        run_sse(server_url=server_url, host=host, port=port)
    else:
        run_stdio(server_url=server_url)


# ---------------------------------------------------------------------------
# completions — shell completion scripts
# ---------------------------------------------------------------------------


@cli.command("completions")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completions(shell: str) -> None:
    """Generate shell completion script for bash, zsh, or fish.

    \b
      eval "$(bernstein completions bash)"
      eval "$(bernstein completions zsh)"
      bernstein completions fish | source
    """
    # Click 8+ ships built-in shell completion via _<PROG>_COMPLETE env vars.
    # We generate the activation snippet the user can eval.
    shell_map = {
        "bash": 'eval "$(_BERNSTEIN_COMPLETE=bash_source bernstein)"',
        "zsh": 'eval "$(_BERNSTEIN_COMPLETE=zsh_source bernstein)"',
        "fish": "_BERNSTEIN_COMPLETE=fish_source bernstein | source",
    }
    click.echo(shell_map[shell])


# ---------------------------------------------------------------------------
# quarantine — manage cross-run task quarantine
# ---------------------------------------------------------------------------


@cli.group("quarantine")
def quarantine_group() -> None:
    """Manage the cross-run task quarantine.

    Tasks that fail repeatedly (3+ times across runs) are quarantined
    so Bernstein stops re-attempting known-bad work.
    """


@quarantine_group.command("list")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    help="Bernstein project directory.",
)
@click.option("--all", "show_all", is_flag=True, default=False, help="Include expired entries.")
def quarantine_list(workdir: str, show_all: bool) -> None:
    """List quarantined tasks.

    By default shows only active (non-expired) entries.
    Use --all to include expired entries older than 7 days.
    """
    from pathlib import Path

    from bernstein.core.quarantine import QuarantineStore

    store = QuarantineStore(Path(workdir) / ".sdd" / "runtime" / "quarantine.json")
    entries = store.load() if show_all else store.get_all()

    if not entries:
        console.print("[dim]No quarantined tasks.[/dim]")
        return

    from rich.table import Table

    table = Table(title="Quarantined Tasks", show_header=True, header_style="bold red")
    table.add_column("Title", style="white", no_wrap=False)
    table.add_column("Fails", style="red", justify="right")
    table.add_column("Last Failure", style="yellow")
    table.add_column("Action", style="cyan")
    table.add_column("Reason", style="dim")

    for entry in entries:
        table.add_row(
            entry.task_title,
            str(entry.fail_count),
            entry.last_failure,
            entry.action,
            entry.reason,
        )

    console.print(table)


@quarantine_group.command("clear")
@click.argument("title", required=False, default=None)
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    help="Bernstein project directory.",
)
def quarantine_clear(title: str | None, workdir: str) -> None:
    """Remove a task from quarantine.

    \b
    Remove a specific task:
      bernstein quarantine clear "519 — Distributed cluster mode"

    Remove all quarantined tasks:
      bernstein quarantine clear
    """
    from pathlib import Path

    from bernstein.core.quarantine import QuarantineStore

    store = QuarantineStore(Path(workdir) / ".sdd" / "runtime" / "quarantine.json")
    store.clear(title)
    if title:
        console.print(f"[green]Cleared quarantine entry for:[/green] {title}")
    else:
        console.print("[green]Cleared all quarantine entries.[/green]")
