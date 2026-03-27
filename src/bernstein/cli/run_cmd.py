"""Run commands: init, conduct, downbeat (legacy start), and the main CLI group."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import click
import httpx

from bernstein.cli.helpers import (
    SDD_DIRS,
    console,
    find_seed_file,
    is_alive,
    print_banner,
    server_get,
)
from bernstein.cli.run import render_run_summary_from_dict
from bernstein.cli.ui import make_console

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
        "cli: auto  # Bernstein picks the best agent per task",
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


@click.command("overture", hidden=True)
@click.option(
    "--dir",
    "target_dir",
    default=".",
    show_default=True,
    help="Directory to initialise (default: current directory).",
)
def init(target_dir: str) -> None:
    """Init workspace -- create .sdd/ structure."""
    print_banner()
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
# Post-run summary helper
# ---------------------------------------------------------------------------


def _show_run_summary() -> None:
    """Fetch final status from the task server and render a summary.

    Silently returns if the server is unreachable (e.g. already stopped).
    """
    data = server_get("/status")
    if data is None:
        return
    force_no_color = not sys.stdout.isatty()
    con = make_console(no_color=force_no_color)
    render_run_summary_from_dict(data, console=con)


# ---------------------------------------------------------------------------
# run  (the "one command" Seed UX)
# ---------------------------------------------------------------------------


@click.command("conduct", hidden=True)
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
@click.option(
    "--cli",
    default=None,
    type=click.Choice(["auto", "claude", "codex", "gemini", "aider", "qwen"], case_sensitive=False),
    help="Force specific CLI agent (overrides auto-detection and config file).",
)
@click.option(
    "--model",
    default=None,
    help="Force specific model (e.g. opus, sonnet, o4-mini; overrides config file).",
)
def run(
    goal: str | None,
    seed_file: str | None,
    port: int,
    cells: int,
    remote: bool,
    cli: str | None,
    model: str | None,
) -> None:
    """Parse seed, init workspace, start server, launch agents.

    \b
      bernstein conduct                     # reads bernstein.yaml
      bernstein conduct --goal "Build X"    # inline goal
      bernstein conduct --seed custom.yaml  # custom seed file
      bernstein conduct --cells 3           # 3 parallel cells (multi-cell mode)
      bernstein conduct --remote            # bind to 0.0.0.0 for cluster access
      bernstein conduct --cli claude        # force Claude Code agent
      bernstein conduct --model opus        # force Opus model
    """
    print_banner()

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
            bootstrap_from_goal(
                goal=goal,
                workdir=workdir,
                port=port,
                cells=cells,
                cli=cli or "auto",  # Default to "auto" if not specified
                model=model,
            )
        except RuntimeError as exc:
            from bernstein.cli.errors import bootstrap_failed

            bootstrap_failed(exc).print()
            raise SystemExit(1) from exc
        import sys as _sys

        if _sys.stdout.isatty():
            try:
                from bernstein.cli.dashboard import BernsteinApp as DashboardApp

                DashboardApp().run()
            except Exception:
                _show_run_summary()
        else:
            _show_run_summary()
        return

    # Seed file mode
    if seed_file is not None:
        path = Path(seed_file)
    else:
        found = find_seed_file()
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
        bootstrap_from_seed(
            seed_path=path,
            workdir=workdir,
            port=port,
            cells=cli_cells,
            remote=remote,
            cli=cli,
            model=model,
        )
    except SeedError as exc:
        from bernstein.cli.errors import seed_parse_error

        seed_parse_error(exc).print()
        raise SystemExit(1) from exc
    except RuntimeError as exc:
        from bernstein.cli.errors import bootstrap_failed

        bootstrap_failed(exc).print()
        raise SystemExit(1) from exc

    # Launch interactive TUI dashboard if running in a terminal
    import sys as _sys

    if _sys.stdout.isatty():
        try:
            from bernstein.cli.dashboard import BernsteinApp as DashboardApp

            app = DashboardApp()
            app.run()
        except Exception:
            _show_run_summary()
    else:
        _show_run_summary()


# ---------------------------------------------------------------------------
# start  (legacy, kept for backward compat)
# ---------------------------------------------------------------------------


@click.command("downbeat", hidden=True)
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
    print_banner()

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
            from bernstein.cli.errors import no_seed_file

            no_seed_file(seed_file).print()
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
    _show_run_summary()


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

DEMO_TASKS: list[dict[str, str]] = [
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


def detect_available_adapter() -> str | None:
    """Return the name of the first available CLI adapter found in PATH.

    Returns:
        Adapter name (e.g. ``'claude'``) or None if none found.
    """
    import shutil as _shutil

    for name, cmd in _ADAPTER_COMMANDS.items():
        if _shutil.which(cmd) is not None:
            return name
    return None


def setup_demo_project(project_dir: Path, adapter: str) -> None:
    """Copy demo template files and seed three backlog tasks.

    Args:
        project_dir: Destination directory (should be empty / temp dir).
        adapter: CLI adapter name -- written into the workspace config.
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
    for task in DEMO_TASKS:
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
        if is_alive(pid):
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


@click.command("demo")
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

    print_banner()

    # Resolve adapter
    detected = adapter or detect_available_adapter()
    if detected is None:
        from bernstein.cli.errors import no_cli_agent_found

        no_cli_agent_found().print()
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
        for i, t in enumerate(DEMO_TASKS, start=3):
            # Parse task inline to get title/role
            parts = t["content"].split("\n")
            title = parts[0].lstrip("# ").strip()
            role = next(
                (ln.split("**Role:**")[-1].strip() for ln in parts if "**Role:**" in ln),
                "backend",
            )
            plan_table.add_row(str(i), f"Run {role} agent", title)
        plan_table.add_row(str(len(DEMO_TASKS) + 3), "Print summary", "tasks done, cost, files changed")
        console.print(plan_table)
        console.print("\n[dim]No agents were spawned. Run [bold]bernstein demo[/bold] to execute.[/dim]")
        return

    # Create temp project dir
    project_dir = Path(tempfile.mkdtemp(prefix="bernstein-demo-"))
    console.print(f"\n[dim]Creating demo project in {project_dir}…[/dim]")

    setup_demo_project(project_dir, detected)
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

        start_time = time.monotonic()
        deadline = start_time + timeout

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
                        done_count = sum(1 for t in tasks_list if t.get("status") == "done")
                        failed_count = sum(1 for t in tasks_list if t.get("status") == "failed")
                        total_tasks = len(tasks_list)
                        progress.update(
                            poll_task,
                            description=(
                                f"Agents working… "
                                f"[green]{done_count}[/green]/{total_tasks} done"
                                + (f"  [red]{failed_count} failed[/red]" if failed_count else "")
                            ),
                        )
                        if total_tasks > 0 and done_count + failed_count >= total_tasks:
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
