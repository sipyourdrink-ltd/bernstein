"""Recipe/cook commands, demo, and confirmation helpers for Bernstein runs."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import click
import httpx

from bernstein.cli.helpers import (
    SDD_DIRS,
    console,
    is_alive,
    print_banner,
    server_get,
)
from bernstein.core.manager_parsing import _resolve_depends_on  # pyright: ignore[reportPrivateUsage]
from bernstein.core.plan_loader import PlanLoadError, load_plan

_STYLE_BOLD_MAGENTA = "bold magenta"

# ---------------------------------------------------------------------------
# Recipe helpers
# ---------------------------------------------------------------------------


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_ANY = "dict[str, Any]"
_CAST_LIST_OBJ = "list[object]"


@dataclass(frozen=True)
class RecipeStage:
    """Stage metadata extracted from a recipe/plan file."""

    name: str
    depends_on: list[str]
    step_titles: list[str]


def _extract_recipe_stages(recipe_path: Path) -> list[RecipeStage]:
    """Parse recipe stage metadata for dry-run graph and progress reporting."""
    import yaml

    raw_data = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, dict):
        return []
    data = cast(_CAST_DICT_STR_ANY, raw_data)
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list):
        return []
    stages_raw = cast(_CAST_LIST_OBJ, raw_stages)

    stages: list[RecipeStage] = []
    for idx, raw_stage in enumerate(stages_raw):
        if not isinstance(raw_stage, dict):
            continue
        stage_map = cast(_CAST_DICT_STR_ANY, raw_stage)
        stage_name_raw = stage_map.get("name")
        stage_name = str(stage_name_raw).strip() if stage_name_raw else f"Stage {idx + 1}"
        deps_raw = stage_map.get("depends_on")
        depends_on: list[str] = []
        if isinstance(deps_raw, list):
            for dep in cast(_CAST_LIST_OBJ, deps_raw):
                depends_on.append(str(dep).strip())

        step_titles: list[str] = []
        steps_raw = stage_map.get("steps")
        if isinstance(steps_raw, list):
            for step in cast(_CAST_LIST_OBJ, steps_raw):
                if not isinstance(step, dict):
                    continue
                step_map = cast(_CAST_DICT_STR_ANY, step)
                title = step_map.get("title") or step_map.get("goal")
                if title:
                    step_titles.append(str(title))
        stages.append(RecipeStage(name=stage_name, depends_on=depends_on, step_titles=step_titles))
    return stages


def _completed_sprints(stages: list[RecipeStage], task_rows: list[dict[str, Any]]) -> int:
    """Return how many recipe stages are fully complete."""
    terminal = {"done", "failed", "cancelled"}
    statuses_by_title: dict[str, set[str]] = {}
    for row in task_rows:
        title = str(row.get("title", ""))
        status = str(row.get("status", ""))
        if not title:
            continue
        statuses_by_title.setdefault(title, set()).add(status)

    completed = 0
    for stage in stages:
        if not stage.step_titles:
            continue
        if all(any(s in terminal for s in statuses_by_title.get(title, set())) for title in stage.step_titles):
            completed += 1
    return completed


def _print_cook_dry_run(
    *,
    recipe_path: Path,
    goal: str,
    stages: list[RecipeStage],
    tasks: list[Any],
) -> None:
    """Render recipe graph and per-task cost estimate without execution."""
    from rich.table import Table

    from bernstein.core.plan_approval import create_plan

    plan = create_plan(goal, tasks)
    estimate_by_task = {estimate.task_id: estimate for estimate in plan.task_estimates}
    stage_by_title: dict[str, str] = {}
    for stage in stages:
        for title in stage.step_titles:
            stage_by_title.setdefault(title, stage.name)

    console.print(f"[bold cyan]Recipe:[/bold cyan] {recipe_path}")
    console.print(f"[bold cyan]Goal:[/bold cyan] {goal}")
    console.print("[bold cyan]Mode:[/bold cyan] dry-run (no execution)\n")

    stage_table = Table(show_header=True, header_style=_STYLE_BOLD_MAGENTA)
    stage_table.add_column("Sprint")
    stage_table.add_column("Depends On")
    stage_table.add_column("Tasks", justify="right")
    for stage in stages:
        stage_table.add_row(
            stage.name,
            ", ".join(stage.depends_on) if stage.depends_on else "-",
            str(len(stage.step_titles)),
        )
    console.print(stage_table)

    task_table = Table(show_header=True, header_style=_STYLE_BOLD_MAGENTA)
    task_table.add_column("Sprint")
    task_table.add_column("Role")
    task_table.add_column("Model")
    task_table.add_column("Est. Cost", justify="right")
    task_table.add_column("Task")
    for task in tasks:
        estimate = estimate_by_task.get(task.id)
        model_name = estimate.model if estimate is not None else (task.model or "sonnet")
        cost_text = f"${estimate.estimated_cost_usd:.4f}" if estimate is not None else "$0.0000"
        task_table.add_row(
            stage_by_title.get(task.title, "-"),
            task.role,
            model_name,
            cost_text,
            task.title,
        )
    console.print()
    console.print(task_table)
    console.print(
        f"\n[bold yellow]Estimated total:[/bold yellow] ${plan.total_estimated_cost_usd:.4f} "
        f"across {len(tasks)} task(s)"
    )


def _wait_for_recipe_completion(
    *,
    stages: list[RecipeStage],
    total_tasks: int,
    poll_interval_s: float = 2.0,
    timeout_s: float = 3600.0,
) -> dict[str, Any] | None:
    """Wait for recipe run completion while printing live sprint/cost progress."""
    deadline = time.time() + timeout_s
    last_status: dict[str, Any] | None = None
    last_line = ""
    while time.time() < deadline:
        status_payload = server_get("/status")
        health_payload = server_get("/health")
        tasks_payload = server_get("/tasks")
        if isinstance(status_payload, dict):
            last_status = status_payload

        if not (isinstance(status_payload, dict) and isinstance(health_payload, dict)):
            time.sleep(poll_interval_s)
            continue

        done_count = int(status_payload.get("done", 0) or 0)
        failed_count = int(status_payload.get("failed", 0) or 0)
        open_count = int(status_payload.get("open", 0) or 0)
        claimed_count = int(status_payload.get("claimed", 0) or 0)
        agent_count = int(health_payload.get("agent_count", 0) or 0)
        spent_usd = float(status_payload.get("total_cost_usd", 0.0) or 0.0)
        completed_tasks = done_count + failed_count
        pct_complete = round((completed_tasks / max(total_tasks, 1)) * 100)
        sprint_done = _completed_sprints(stages, tasks_payload) if isinstance(tasks_payload, list) else 0
        line = f"Sprint {sprint_done}/{max(len(stages), 1)} | {pct_complete}% complete | ${spent_usd:.2f} spent"
        if line != last_line:
            console.print(line)
            last_line = line

        total = int(status_payload.get("total", 0) or 0)
        if total > 0 and open_count == 0 and claimed_count == 0 and agent_count == 0:
            return status_payload
        time.sleep(poll_interval_s)
    return last_status


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
        "filename": "1-fix-off-by-one.md",
        "content": (
            "# Fix off-by-one in get_item route\n\n"
            "**Role:** backend\n"
            "**Priority:** 1\n"
            "**Scope:** small\n"
            "**Complexity:** low\n\n"
            "BUG: `get_item(n)` in `app.py` accesses `ITEMS[n]` (zero-indexed) "
            "but the `/items/<n>` route is 1-indexed. "
            "Fix: use `ITEMS[n - 1]` and return 404 when `n` is out of range.\n"
        ),
    },
    {
        "filename": "2-fix-missing-import.md",
        "content": (
            "# Fix missing `request` import\n\n"
            "**Role:** backend\n"
            "**Priority:** 1\n"
            "**Scope:** small\n"
            "**Complexity:** low\n\n"
            "BUG: `app.py` uses `request.args` in the `/echo` endpoint but "
            "`request` is not imported from flask. "
            "Add `request` to the `from flask import ...` line.\n"
        ),
    },
    {
        "filename": "3-fix-health-status-code.md",
        "content": (
            "# Fix health endpoint returns 201 instead of 200\n\n"
            "**Role:** backend\n"
            "**Priority:** 2\n"
            "**Scope:** small\n"
            "**Complexity:** low\n\n"
            "BUG: the `/health` endpoint in `app.py` returns HTTP 201 (Created) "
            "instead of 200 (OK). Remove the explicit status code so it defaults "
            "to 200 and `test_health_returns_200` passes.\n"
        ),
    },
    {
        "filename": "4-fix-broken-test.md",
        "content": (
            "# Fix broken assertion in test_hello_returns_200\n\n"
            "**Role:** qa\n"
            "**Priority:** 2\n"
            "**Scope:** small\n"
            "**Complexity:** low\n\n"
            "BUG: `tests/test_app.py::test_hello_returns_200` asserts "
            "`resp.status_code == 404` — wrong, it should assert 200. "
            "Fix the assertion so the test suite goes green.\n"
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
        # Contains 4 intentional bugs matching the demo tasks.
        (project_dir / "app.py").write_text(
            '"""Simple Flask web application for the Bernstein demo.\n\n'
            "Contains four intentional bugs for the demo to fix.\n"
            '"""\n'
            "from flask import Flask, jsonify  "
            "# BUG 2: 'request' is missing from this import\n\n"
            "app = Flask(__name__)\n\n"
            'ITEMS = ["apple", "banana", "cherry", "date"]\n\n\n'
            '@app.route("/")\n'
            "def hello() -> object:\n"
            '    """Return a greeting."""\n'
            '    return jsonify({"message": "Hello, World!", "status": "ok"})\n\n\n'
            '@app.route("/items/<int:n>")\n'
            "def get_item(n: int) -> object:\n"
            '    """Return the nth item (1-indexed). BUG 1: off-by-one."""\n'
            '    return jsonify({"id": n, "item": ITEMS[n]})  # off-by-one\n\n\n'
            '@app.route("/echo")\n'
            "def echo() -> object:\n"
            '    """Echo a query param. BUG 2: request not imported."""\n'
            '    msg = request.args.get("msg", "")  '
            "# type: ignore[name-defined]  # noqa: F821\n"
            '    return jsonify({"echo": msg})\n\n\n'
            '@app.route("/health")\n'
            "def health() -> object:\n"
            '    """Health check. BUG 3: returns 201 instead of 200."""\n'
            '    return jsonify({"status": "healthy", "version": "1.0.0"}), 201  '
            "# type: ignore[return-value]\n\n\n"
            'if __name__ == "__main__":\n'
            "    app.run(debug=True)\n"
        )
        (project_dir / "requirements.txt").write_text("flask>=3.0.0\npytest>=8.0.0\n")
        tests_dir = project_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "__init__.py").write_text("")
        (tests_dir / "test_app.py").write_text(
            '"""Tests for the demo Flask app. BUG 4: one broken assertion."""\n'
            "import pytest\n"
            "from app import app as flask_app\n\n\n"
            "@pytest.fixture\n"
            "def client():\n"
            '    flask_app.config["TESTING"] = True\n'
            "    with flask_app.test_client() as c:\n"
            "        yield c\n\n\n"
            "def test_hello_returns_200(client):\n"
            '    """BUG 4: asserts 404 instead of 200."""\n'
            '    resp = client.get("/")\n'
            "    assert resp.status_code == 404  # wrong — should be 200\n\n\n"
            "def test_hello_json_structure(client):\n"
            '    resp = client.get("/")\n'
            "    data = resp.get_json()\n"
            "    assert data is not None\n"
            '    assert "message" in data\n'
            '    assert data["status"] == "ok"\n\n\n'
            "def test_get_item_first(client):\n"
            '    resp = client.get("/items/1")\n'
            "    assert resp.status_code == 200\n"
            '    assert resp.get_json()["item"] == "apple"\n\n\n'
            "def test_health_returns_200(client):\n"
            '    resp = client.get("/health")\n'
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
            from bernstein.core.platform_compat import kill_process

            kill_process(pid, sig=15)
        pid_file.unlink(missing_ok=True)


def _print_demo_summary(project_dir: Path, server_url: str, elapsed_secs: float = 0.0) -> None:
    """Print final demo summary: bugs fixed, files changed, cost, next steps.

    Args:
        project_dir: Demo project root.
        server_url: Base URL of the demo task server.
        elapsed_secs: Wall-clock seconds the orchestration took.
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

    elapsed_str = f"{elapsed_secs:.0f}s" if elapsed_secs > 0 else "\u2014"

    ruler = "\u2500" * 42
    console.print(f"\n[bold cyan]\u2500\u2500 Demo Summary {ruler}[/bold cyan]")

    table = Table(show_header=True, header_style=_STYLE_BOLD_MAGENTA, show_lines=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Bugs fixed", f"[green]{done}[/green] / {total}")
    if failed:
        table.add_row("Tasks failed", f"[red]{failed}[/red]")
    table.add_row("Elapsed", elapsed_str)

    # Count Python files in the project dir (excluding .sdd/)
    py_files = [p for p in project_dir.glob("**/*.py") if ".sdd" not in p.parts]
    table.add_row("Python files", str(len(py_files)))
    table.add_row("API cost", f"${total_cost:.4f}")
    console.print(table)

    # Governance story
    console.print(
        "\n[dim]Every agent decision was logged. "
        "Run [bold]bernstein audit verify --merkle[/bold] to inspect the audit trail.[/dim]"
    )

    # Primary CTA
    console.print(f"\n[bold green]Fixed {done} bug{'s' if done != 1 else ''} in {elapsed_str}.[/bold green]")
    console.print("Run [bold cyan]bernstein run[/bold cyan] in your own project to get started.\n")

    console.print(f"[dim]Project left at:[/dim] {project_dir}")
    console.print("[dim]  cd <dir> && pip install -r requirements.txt && pytest tests/ -q[/dim]")


@click.command("cook")
@click.argument("recipe", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Parse recipe, show sprint graph + estimated cost, and exit.",
)
@click.option("--port", default=8052, show_default=True, help="Task server port in execution mode.")
@click.option("--cells", default=1, show_default=True, help="Number of cells in execution mode.")
@click.option("--cli", default=None, help="CLI adapter override (defaults to recipe config or auto).")
@click.option("--model", default=None, help="Model override for execution.")
@click.option("--timeout", default=3600, show_default=True, help="Max seconds to wait for completion.")
def cook(
    recipe: Path,
    dry_run: bool,
    port: int,
    cells: int,
    cli: str | None,
    model: str | None,
    timeout: int,
) -> None:
    """Execute a recipe/plan YAML with optional dry-run planning output."""
    print_banner()
    try:
        plan_config, tasks = load_plan(recipe)
    except PlanLoadError as exc:
        console.print(f"[red]Failed to load recipe:[/red] {exc}")
        raise SystemExit(1) from exc

    _resolve_depends_on(tasks)
    stages = _extract_recipe_stages(recipe)
    goal = plan_config.name.strip() if plan_config.name.strip() else recipe.stem

    if dry_run:
        _print_cook_dry_run(recipe_path=recipe, goal=goal, stages=stages, tasks=tasks)
        console.print("\n[dim]Dry-run only. No agents were spawned.[/dim]")
        return

    selected_cli = cli or plan_config.cli or "auto"
    console.print(
        f"[bold cyan]Executing recipe[/bold cyan] {recipe} [dim](cli={selected_cli}, cells={cells}, port={port})[/dim]"
    )

    from bernstein.core.bootstrap import bootstrap_from_goal  # pyright: ignore[reportUnknownVariableType]

    bootstrap_from_goal(
        goal=goal,
        workdir=Path.cwd(),
        port=port,
        cells=cells,
        cli=selected_cli,
        model=model,
        tasks=tasks,
    )
    final_status = _wait_for_recipe_completion(
        stages=stages,
        total_tasks=len(tasks),
        timeout_s=float(timeout),
    )
    if isinstance(final_status, dict):
        done = int(final_status.get("done", 0) or 0)
        failed = int(final_status.get("failed", 0) or 0)
        spent = float(final_status.get("total_cost_usd", 0.0) or 0.0)
        console.print(f"[bold green]Recipe finished:[/bold green] done={done}, failed={failed}, spent=${spent:.2f}")
    else:
        console.print("[yellow]Recipe monitor timed out before a final status snapshot was available.[/yellow]")


@click.command("demo")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show the demo plan without spawning any agents.",
)
@click.option(
    "--real",
    is_flag=True,
    default=False,
    help="Use real agents (requires API key) instead of mock agents.",
)
@click.option(
    "--adapter",
    default=None,
    metavar="NAME",
    help="CLI adapter to use (auto-detected by default for --real, mock for demo).",
)
@click.option(
    "--timeout",
    default=60,
    show_default=True,
    help="Maximum seconds to wait for tasks to complete.",
)
def demo(dry_run: bool, real: bool, adapter: str | None, timeout: int) -> None:
    """Zero-config demo: fix 4 bugs in a Flask app in under 60 seconds.

    \b
    Creates a temp Flask app with 4 intentional bugs, seeds fix tasks,
    then runs agents to resolve them — all while showing live progress.
    No API key required in mock mode.

    \b
      bernstein demo              # mock agents (no API key, ~30 seconds)
      bernstein demo --real       # real agents (requires API key, ~$0.15)
      bernstein demo --dry-run    # preview the plan without spawning
      bernstein demo --real --timeout 120
    """
    import tempfile

    print_banner()

    # Resolve adapter: mock by default, or real CLI if --real is specified
    if real:
        detected = adapter or detect_available_adapter()
        if detected is None:
            from bernstein.cli.errors import no_cli_agent_found

            no_cli_agent_found().print()
            raise SystemExit(1)
        cost_estimate = "~$0.15 in API credits"
    else:
        detected = "mock"
        cost_estimate = "[green]free[/green] (simulated agents, no API calls)"

    # Always print cost estimate before doing anything
    console.print(
        f"\n[bold yellow]Cost estimate:[/bold yellow] "
        f"{cost_estimate} (4 bug-fix tasks)\n"
        f"[dim]Adapter: {detected}  |  Mode: {'real' if real else 'demo'}  |  Timeout: {timeout}s[/dim]"
    )

    if dry_run:
        console.print("\n[bold cyan][DRY RUN] What would happen:[/bold cyan]\n")
        from rich.table import Table

        plan_table = Table(show_header=True, header_style=_STYLE_BOLD_MAGENTA)
        plan_table.add_column("Step")
        plan_table.add_column("Action")
        plan_table.add_column("Detail")
        plan_table.add_row("1", "Create project", "Temp dir with buggy Flask app (4 intentional bugs)")
        plan_table.add_row("2", "Seed backlog", f"{len(DEMO_TASKS)} bug-fix tasks in .sdd/backlog/open/")
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
    console.print(f"\n[dim]Creating demo project in {project_dir}\u2026[/dim]")

    setup_demo_project(project_dir, detected)
    console.print("[green]\u2713[/green] Flask app with 4 intentional bugs created")
    console.print(
        "[green]\u2713[/green] 4 bug-fix tasks seeded: "
        "off-by-one \u00b7 missing import \u00b7 wrong status code \u00b7 broken test"
    )

    server_url = f"http://127.0.0.1:{_DEMO_PORT}"
    orchestration_start = time.monotonic()

    try:
        # Bootstrap: start server + spawner in the demo project dir
        console.print("\n[bold]Starting orchestration\u2026[/bold]")
        from bernstein.core.bootstrap import bootstrap_from_goal  # pyright: ignore[reportUnknownVariableType]

        bootstrap_from_goal(
            goal="Fix the four bugs in the demo Flask app.",
            workdir=project_dir,
            port=_DEMO_PORT,
            cli=detected,
        )

        # Poll for completion with a live progress indicator and per-task events
        from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

        deadline = orchestration_start + timeout
        seen_done: set[str] = set()
        seen_failed: set[str] = set()

        console.print()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        ) as progress:
            poll_task = progress.add_task("Agents working\u2026", total=None)

            while time.monotonic() < deadline:
                try:
                    resp = httpx.get(f"{server_url}/status", timeout=3.0)
                    if resp.status_code == 200:
                        payload = resp.json()
                        tasks_list: list[dict[str, Any]] = payload.get("tasks", [])
                        done_count = sum(1 for t in tasks_list if t.get("status") == "done")
                        failed_count = sum(1 for t in tasks_list if t.get("status") == "failed")
                        total_tasks = len(tasks_list)

                        # Emit a line for each newly-completed task
                        for t in tasks_list:
                            tid = t.get("id", "")
                            title = (t.get("title") or "")[:60]
                            role = t.get("role", "agent")
                            if t.get("status") == "done" and tid not in seen_done:
                                seen_done.add(tid)
                                progress.console.print(f"  [green]\u2713[/green] [{role}] {title}")
                            elif t.get("status") == "failed" and tid not in seen_failed:
                                seen_failed.add(tid)
                                progress.console.print(f"  [red]\u2717[/red] [{role}] {title}")

                        progress.update(
                            poll_task,
                            description=(
                                f"Agents working\u2026 "
                                f"[green]{done_count}[/green]/{total_tasks} bugs fixed"
                                + (f"  [red]{failed_count} failed[/red]" if failed_count else "")
                            ),
                        )
                        if total_tasks > 0 and done_count + failed_count >= total_tasks:
                            break
                except Exception:
                    pass
                time.sleep(2)

        console.print("[green]\u2713[/green] Orchestration finished")

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
    except RuntimeError as exc:
        from bernstein.cli.errors import bootstrap_failed

        bootstrap_failed(exc).print()
    finally:
        _stop_demo_processes(project_dir)

    elapsed = time.monotonic() - orchestration_start
    _print_demo_summary(project_dir, server_url, elapsed_secs=elapsed)
