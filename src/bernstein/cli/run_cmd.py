"""Run commands: init, conduct, downbeat (legacy start), and the main CLI group."""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

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
from bernstein.core.cost import estimate_run_cost
from bernstein.core.manager_parsing import _resolve_depends_on  # pyright: ignore[reportPrivateUsage]
from bernstein.core.plan_loader import PlanLoadError, load_plan, load_plan_from_yaml
from bernstein.core.runtime_state import directory_size_bytes

# ---------------------------------------------------------------------------
# Plan helpers
# ---------------------------------------------------------------------------


def _build_synthetic_plan(goal: str, team: list[str] | None = None) -> tuple[Any, list[Any]]:
    """Build a synthetic TaskPlan from a goal for --plan-only or confirmation display.

    Args:
        goal: Project goal string.
        team: Optional list of role names. Defaults to ["manager"].

    Returns:
        Tuple of (TaskPlan, list[Task]).
    """
    from bernstein.core.models import Complexity, Scope, Task
    from bernstein.core.plan_approval import create_plan

    roles = team or ["manager"]
    tasks: list[Task] = []
    for i, role in enumerate(roles):
        tasks.append(
            Task(
                id=f"planned-{i + 1}",
                title=f"[{role}] {goal[:70]}",
                description=goal,
                role=role,
                priority=i + 1,
                scope=Scope.MEDIUM,
                complexity=Complexity.MEDIUM,
            )
        )
    plan = create_plan(goal, tasks)
    return plan, tasks


def _load_plan_goal(plan_path: Path) -> str:
    """Extract the goal from a saved plan file (JSON or markdown).

    Args:
        plan_path: Path to the plan file.

    Returns:
        Goal string extracted from the plan.

    Raises:
        ValueError: If the goal cannot be extracted.
    """
    content = plan_path.read_text()

    # Try JSON first (PlanStore format)
    if plan_path.suffix == ".json":
        try:
            data = json.loads(content)
            if "goal" in data:
                return str(data["goal"])
        except json.JSONDecodeError:
            pass

    # Fall back to markdown: look for "**Goal:** ..." line
    for line in content.splitlines():
        if line.startswith("**Goal:**"):
            return line.replace("**Goal:**", "").strip()

    raise ValueError(f"Could not extract goal from plan file: {plan_path}")


def _save_plan_markdown(md: str, workdir: Path) -> Path:
    """Save rendered plan markdown to .sdd/runtime/plans/ with a timestamp name.

    Args:
        md: Markdown content to save.
        workdir: Project root directory.

    Returns:
        Path to the saved file.
    """
    plans_dir = workdir / ".sdd" / "runtime" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    plan_file = plans_dir / f"plan-{ts}.md"
    plan_file.write_text(md)
    return plan_file


def _show_dry_run_plan(
    workdir: Path,
    plan_file: Path | None,
    goal: str | None,
    seed_file: str | None,
    model_override: str | None,
    cli: str | None,
) -> None:
    """Show scheduling plan without executing.

    When a plan file is provided, loads tasks directly from it so no running
    server is required.  Otherwise falls back to fetching open tasks from a
    running task server.

    Args:
        workdir: Project root directory.
        plan_file: Optional plan file path.
        goal: Optional goal string.
        seed_file: Optional seed file path.
        model_override: Optional model override.
        cli: Optional CLI override.
    """
    from rich.table import Table

    from bernstein.core.models import Task

    console.print("\n[bold]Dry-run mode — no agents will be spawned.[/bold]\n")

    tasks: list[Task] = []

    # If a plan file is given, load tasks directly (no server needed)
    if plan_file is not None:
        try:
            _plan_config, tasks = load_plan(plan_file)
        except PlanLoadError as exc:
            console.print(f"[red]Plan load error:[/red] {exc}")
            raise SystemExit(1) from exc
    else:
        # Fall back to fetching from running server

        try:
            resp = httpx.get("http://127.0.0.1:8052/tasks?status=open", timeout=5.0)
            resp.raise_for_status()
            tasks_data = resp.json()
        except httpx.ConnectError as err:
            console.print("[red]Task server not running. Start with `bernstein conduct` first,[/red]")
            console.print("[red]or pass a plan file: `bernstein run --dry-run plan.yaml`[/red]")
            raise SystemExit(1) from err
        except Exception as exc:
            console.print(f"[red]Failed to fetch tasks:[/red] {exc}")
            raise SystemExit(1) from exc

        tasks = [Task.from_dict(td) for td in tasks_data]

    if not tasks:
        console.print("[yellow]No tasks to schedule.[/yellow]")
        return

    # Build dry-run task table
    table = Table(title="Dry-Run Scheduling Plan", show_header=True, header_style="bold cyan")
    table.add_column("#", justify="right")
    table.add_column("Role", style="cyan")
    table.add_column("Title")
    table.add_column("Model", style="green")
    table.add_column("Effort")
    table.add_column("Priority", justify="center")

    for i, task in enumerate(tasks, 1):
        table.add_row(
            str(i),
            task.role,
            task.title[:60] + "..." if len(task.title) > 60 else task.title,
            task.model or "auto",
            task.effort or "auto",
            str(task.priority),
        )

    console.print(table)

    # Dependency graph (show tasks that have depends_on)
    deps_found = False
    for task in tasks:
        if task.depends_on:
            if not deps_found:
                console.print("\n[bold]Dependency graph:[/bold]")
                deps_found = True
            dep_str = ", ".join(task.depends_on)
            console.print(f"  {task.title} -> [{dep_str}]")

    # Cost estimate
    est_model = model_override or "sonnet"
    low_usd, high_usd = estimate_run_cost(len(tasks), est_model)
    console.print(f"\n  Total tasks: {len(tasks)}")
    console.print(f"  Estimated cost: ${(low_usd + high_usd) / 2:.2f} (${low_usd:.2f}-${high_usd:.2f})")

    console.print("\n[green]Dry run complete. No agents were spawned.[/green]")


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
    data = cast("dict[str, Any]", raw_data)
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list):
        return []
    stages_raw = cast("list[object]", raw_stages)

    stages: list[RecipeStage] = []
    for idx, raw_stage in enumerate(stages_raw):
        if not isinstance(raw_stage, dict):
            continue
        stage_map = cast("dict[str, Any]", raw_stage)
        stage_name_raw = stage_map.get("name")
        stage_name = str(stage_name_raw).strip() if stage_name_raw else f"Stage {idx + 1}"
        deps_raw = stage_map.get("depends_on")
        depends_on: list[str] = []
        if isinstance(deps_raw, list):
            for dep in cast("list[object]", deps_raw):
                depends_on.append(str(dep).strip())

        step_titles: list[str] = []
        steps_raw = stage_map.get("steps")
        if isinstance(steps_raw, list):
            for step in cast("list[object]", steps_raw):
                if not isinstance(step, dict):
                    continue
                step_map = cast("dict[str, Any]", step)
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

    stage_table = Table(show_header=True, header_style="bold magenta")
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

    task_table = Table(show_header=True, header_style="bold magenta")
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


@dataclass(frozen=True)
class RunCostEstimate:
    """Preflight cost estimate for a pending run."""

    task_count: int
    model: str
    low_usd: float
    high_usd: float


def _estimate_run_preview(
    *,
    workdir: Path,
    plan_file: Path | None,
    goal: str | None,
    seed_file: str | None,
    model_override: str | None,
) -> RunCostEstimate:
    """Estimate run cost before bootstrapping the orchestrator.

    Args:
        workdir: Repository root.
        plan_file: Optional explicit YAML plan file.
        goal: Optional inline goal.
        seed_file: Optional seed path override.
        model_override: Optional CLI ``--model`` override.

    Returns:
        Cost estimate using the best available task count and model hint.
    """
    est_task_count = 5
    if plan_file is not None:
        try:
            est_task_count = max(1, len(load_plan_from_yaml(plan_file)))
        except Exception:
            est_task_count = 5
    elif goal is None:
        backlog_dir = workdir / ".sdd" / "backlog" / "open"
        if backlog_dir.exists():
            est_task_count = max(1, len(list(backlog_dir.glob("*.md"))))

    est_model = model_override or "sonnet"
    seed_path = Path(seed_file) if seed_file is not None else find_seed_file()
    if model_override is None and seed_path is not None and seed_path.exists():
        try:
            from bernstein.core.seed import parse_seed

            seed = parse_seed(seed_path)
            if seed.model:
                est_model = seed.model
        except Exception:
            est_model = "sonnet"

    low_usd, high_usd = estimate_run_cost(est_task_count, est_model)
    return RunCostEstimate(
        task_count=est_task_count,
        model=est_model,
        low_usd=low_usd,
        high_usd=high_usd,
    )


def _emit_preflight_runtime_warnings(
    *,
    workdir: Path,
    estimate: RunCostEstimate,
    auto_approve: bool,
    quiet: bool,
) -> None:
    """Show startup cost and disk-usage warnings before execution.

    Args:
        workdir: Repository root.
        estimate: Cost estimate computed from local context.
        auto_approve: Whether confirmation prompts are disabled.
        quiet: Whether normal startup output is suppressed.

    Raises:
        SystemExit: When the operator declines a high-cost run.
    """
    sdd_dir = workdir / ".sdd"
    disk_usage_gb = directory_size_bytes(sdd_dir) / (1024**3)
    if not quiet:
        console.print(
            "[bold yellow]Estimated cost:[/bold yellow] "
            f"${estimate.low_usd:.2f}-${estimate.high_usd:.2f} "
            f"based on {estimate.task_count} task(s) at {estimate.model} pricing"
        )
        if disk_usage_gb >= 1.0:
            console.print(
                "[yellow]Warning:[/yellow] "
                f".sdd/ is using {disk_usage_gb:.2f} GB. "
                "Run [bold]bernstein cleanup[/bold] if stale worktrees or logs are accumulating."
            )

    if (
        estimate.high_usd > 10.0
        and not auto_approve
        and not click.confirm(
            f"Warning: estimated cost may reach ${estimate.high_usd:.2f}. Continue?",
            default=True,
        )
    ):
        raise SystemExit(1)


@contextlib.contextmanager
def _quiet_bootstrap_console(enabled: bool) -> Any:
    """Suppress bootstrap Rich output while leaving the final summary visible.

    Args:
        enabled: When True, redirects bootstrap console writes to an in-memory buffer.

    Yields:
        ``None`` while the bootstrap module uses a muted console.
    """
    if not enabled:
        yield
        return

    from rich.console import Console

    import bernstein.core.bootstrap as bootstrap_module

    original_console = bootstrap_module.console
    bootstrap_module.console = Console(file=io.StringIO(), force_terminal=False, color_system=None)
    try:
        yield
    finally:
        bootstrap_module.console = original_console


def _wait_for_run_completion(
    *,
    poll_interval_s: float = 2.0,
    timeout_s: float = 3600.0,
) -> dict[str, Any] | None:
    """Poll the server until the run becomes quiescent.

    Args:
        poll_interval_s: Delay between status polls.
        timeout_s: Maximum total time to wait.

    Returns:
        Final ``/status`` payload when quiescent, else the last observed payload.
    """
    deadline = time.time() + timeout_s
    last_status: dict[str, Any] | None = None
    while time.time() < deadline:
        status_payload = server_get("/status")
        health_payload = server_get("/health")
        if isinstance(status_payload, dict):
            last_status = status_payload
        if isinstance(status_payload, dict) and isinstance(health_payload, dict):
            total = int(status_payload.get("total", 0) or 0)
            open_count = int(status_payload.get("open", 0) or 0)
            claimed_count = int(status_payload.get("claimed", 0) or 0)
            agent_count = int(health_payload.get("agent_count", 0) or 0)
            if total > 0 and open_count == 0 and claimed_count == 0 and agent_count == 0:
                return status_payload
        time.sleep(poll_interval_s)
    return last_status


def _make_profile_ctx(profile: bool, workdir: Path) -> contextlib.AbstractContextManager[Any]:
    """Return a ProfilerSession context manager, or a no-op if profiling is disabled.

    Args:
        profile: Whether profiling is enabled.
        workdir: Project root directory used to resolve output path.

    Returns:
        A context manager that profiles the wrapped block (or does nothing).
    """
    import contextlib

    if profile:
        from bernstein.core.profiler import ProfilerSession, resolve_profile_output_dir

        return ProfilerSession(resolve_profile_output_dir(workdir))
    return contextlib.nullcontext()


def _finalize_run_output(*, quiet: bool) -> None:
    """Render either the interactive dashboard or the final summary.

    Args:
        quiet: When True, wait for quiescence and print only the terminal summary.
    """
    if quiet:
        _wait_for_run_completion()
        _show_run_summary()
        return

    if sys.stdout.isatty():
        try:
            from bernstein.cli.dashboard import BernsteinApp as DashboardApp

            app = DashboardApp()
            with contextlib.suppress(SystemExit):
                app.run()
            # Hot restart: Textual has restored terminal, re-exec safely
            if getattr(app, "_restart_on_exit", False):
                os.execv(sys.executable, [sys.executable, "-m", "bernstein.cli.main", "live"])
        except Exception:
            _show_run_summary()
    else:
        _show_run_summary()


def _configure_quality_gate_bypass(
    *,
    goal: str | None,
    seed_file: str | None,
    skip_gate: tuple[str, ...],
    skip_gate_reason: str | None,
) -> None:
    """Validate and export quality-gate bypass settings for the orchestrator."""
    if not skip_gate and not skip_gate_reason:
        os.environ.pop("BERNSTEIN_SKIP_GATES", None)
        os.environ.pop("BERNSTEIN_SKIP_GATE_REASON", None)
        return
    if skip_gate_reason and not skip_gate:
        raise click.UsageError("--skip-gate-reason requires at least one --skip-gate")
    if goal is not None:
        raise click.UsageError("--skip-gate requires a seed file with quality_gates.allow_bypass: true")

    from bernstein.core.seed import SeedError, parse_seed

    seed_path = Path(seed_file) if seed_file is not None else find_seed_file()
    if seed_path is None:
        raise click.UsageError("--skip-gate requires a seed file with quality_gates.allow_bypass: true")

    try:
        seed = parse_seed(seed_path)
    except SeedError as exc:
        raise click.UsageError(str(exc)) from exc

    if seed.quality_gates is None or not seed.quality_gates.allow_bypass:
        raise click.UsageError("quality_gates.allow_bypass must be true to use --skip-gate")

    normalized = sorted({gate.strip() for gate in skip_gate if gate.strip()})
    if not normalized:
        raise click.UsageError("At least one non-empty --skip-gate is required")
    os.environ["BERNSTEIN_SKIP_GATES"] = ",".join(normalized)
    if skip_gate_reason:
        os.environ["BERNSTEIN_SKIP_GATE_REASON"] = skip_gate_reason
    else:
        os.environ.pop("BERNSTEIN_SKIP_GATE_REASON", None)


# ---------------------------------------------------------------------------
# run  (the "one command" Seed UX)
# ---------------------------------------------------------------------------


@click.command("conduct", hidden=True)
@click.argument(
    "plan_file",
    required=False,
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
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
@click.option(
    "--workflow",
    default=None,
    type=click.Choice(["governed"], case_sensitive=False),
    help="Activate a governed workflow mode (deterministic phase-based execution).",
)
@click.option(
    "--routing",
    default=None,
    type=click.Choice(["static", "bandit"], case_sensitive=False),
    help=(
        "Model routing strategy: 'static' = fixed cascade heuristics (default), "
        "'bandit' = contextual LinUCB bandit that learns cost-quality tradeoffs."
    ),
)
@click.option(
    "--compliance",
    default=None,
    type=click.Choice(["development", "standard", "regulated"], case_sensitive=False),
    help=(
        "Compliance preset: 'development' = audit + WAL + AI labels, "
        "'standard' = + HMAC chain + governed workflow + approval gates, "
        "'regulated' = + signed WAL + data residency + SBOM + evidence bundle."
    ),
)
@click.option(
    "--container/--no-container",
    default=False,
    help="Run agents inside containers for kernel-level isolation (requires Docker or Podman).",
)
@click.option(
    "--container-image",
    default=None,
    help="Container image for agent execution (default: bernstein-agent:latest). Requires --container.",
)
@click.option(
    "--sandbox",
    default=None,
    type=click.Choice(["docker", "podman"], case_sensitive=False),
    help="Run agents in a Docker/Podman sandbox. Preferred alias for --container.",
)
@click.option(
    "--two-phase-sandbox/--no-two-phase-sandbox",
    default=False,
    help=(
        "Codex-style two-phase sandboxed execution: "
        "Phase 1 installs dependencies with network access, "
        "Phase 2 runs the agent with the network fully disabled. "
        "Requires --container."
    ),
)
@click.option(
    "--plan-only",
    is_flag=True,
    default=False,
    help="Generate and display the execution plan without running any agents.",
)
@click.option(
    "--from-plan",
    "from_plan",
    default=None,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Load a saved plan file and execute it (skips interactive planning).",
)
@click.option(
    "--auto-approve",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt before execution.",
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress startup/TUI output and print only the final summary.",
)
@click.option(
    "--skip-gate",
    "skip_gate",
    multiple=True,
    help="Bypass a named quality gate for this run (requires quality_gates.allow_bypass: true).",
)
@click.option(
    "--skip-gate-reason",
    default=None,
    help="Operator-visible reason recorded for quality-gate bypasses.",
)
@click.option(
    "--audit",
    is_flag=True,
    default=False,
    help=(
        "Enable SOC 2 audit mode: append-only HMAC-chained audit log for every "
        "task lifecycle event, with Merkle tree seal on shutdown."
    ),
)
@click.option(
    "--ab-test",
    is_flag=True,
    default=False,
    help="A/B testing mode: spawn two agents with different models for each task.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show scheduling plan without executing: which agent/model/tier each task would be assigned to.",
)
@click.option(
    "--profile",
    is_flag=True,
    default=False,
    help=(
        "Profile orchestrator execution with cProfile. Writes .prof binary and .txt report to .sdd/runtime/profiles/."
    ),
)
def run(
    plan_file: Path | None,
    goal: str | None,
    seed_file: str | None,
    port: int,
    cells: int,
    remote: bool,
    cli: str | None,
    model: str | None,
    workflow: str | None,
    routing: str | None,
    compliance: str | None,
    container: bool,
    container_image: str | None,
    two_phase_sandbox: bool,
    plan_only: bool,
    from_plan: Path | None,
    auto_approve: bool,
    quiet: bool,
    skip_gate: tuple[str, ...],
    skip_gate_reason: str | None,
    audit: bool,
    sandbox: str | None = None,
    ab_test: bool = False,
    dry_run: bool = False,
    profile: bool = False,
) -> None:
    """Parse seed, init workspace, start server, launch agents.

    \b
      bernstein run plan.yaml                  # loadable YAML plan (stages + steps)
      bernstein conduct                        # reads bernstein.yaml
      bernstein conduct --goal "Build X"       # inline goal
      bernstein conduct --seed custom.yaml     # custom seed file
      bernstein conduct --plan-only            # show plan without executing
      bernstein conduct --from-plan plan.md    # execute a saved plan
      bernstein conduct --auto-approve         # skip confirmation prompt
      bernstein conduct --cells 3              # 3 parallel cells (multi-cell mode)
      bernstein conduct --remote               # bind to 0.0.0.0 for cluster access
      bernstein conduct --cli claude           # force Claude Code agent
      bernstein conduct --model opus           # force Opus model
      bernstein conduct --workflow governed    # governed workflow mode
      bernstein conduct --routing bandit       # contextual bandit routing (learns over time)
      bernstein conduct --compliance standard  # compliance mode (development/standard/regulated)
      bernstein conduct --container            # run agents in containers
      bernstein conduct --sandbox docker       # run agents in Docker sandbox
      bernstein conduct --container --two-phase-sandbox  # two-phase sandboxed execution
      bernstein conduct --audit                # SOC 2 audit mode (HMAC-chained log + Merkle seal)
    """
    # Banner already printed by cli() — don't duplicate

    # Set process title so orchestrator is visible in Activity Monitor / ps
    try:
        import setproctitle

        setproctitle.setproctitle("bernstein: orchestrator")
    except ImportError:
        pass

    from bernstein.core.bootstrap import (  # pyright: ignore[reportUnknownVariableType]
        bootstrap_from_goal,
        bootstrap_from_seed,
    )
    from bernstein.core.seed import SeedError

    # Propagate profiler flag via env var so the orchestrator subprocess picks it up
    if profile:
        os.environ["BERNSTEIN_PROFILE"] = "1"

    # Propagate workflow mode to orchestrator subprocess via env var
    if workflow:
        os.environ["BERNSTEIN_WORKFLOW"] = workflow

    # Propagate routing mode so the orchestrator picks up bandit vs static
    if routing:
        os.environ["BERNSTEIN_ROUTING"] = routing

    # Propagate compliance preset so the orchestrator subprocess picks it up
    if compliance:
        os.environ["BERNSTEIN_COMPLIANCE"] = compliance

    # Propagate container isolation settings to the orchestrator subprocess
    if sandbox:
        os.environ["BERNSTEIN_CONTAINER"] = "1"
        os.environ["BERNSTEIN_SANDBOX_RUNTIME"] = sandbox.lower()
    elif container:
        os.environ["BERNSTEIN_CONTAINER"] = "1"
    if container_image:
        os.environ["BERNSTEIN_CONTAINER_IMAGE"] = container_image
    if two_phase_sandbox:
        os.environ["BERNSTEIN_TWO_PHASE_SANDBOX"] = "1"

    # Propagate quiet flag so the orchestrator suppresses the live summary card
    if quiet:
        os.environ["BERNSTEIN_QUIET"] = "1"

    _configure_quality_gate_bypass(
        goal=goal,
        seed_file=seed_file,
        skip_gate=skip_gate,
        skip_gate_reason=skip_gate_reason,
    )

    # Propagate audit mode so the orchestrator enables SOC 2 audit logging
    if audit:
        os.environ["BERNSTEIN_AUDIT"] = "1"

    # --dry-run: show scheduling plan without executing
    if dry_run:
        _show_dry_run_plan(
            workdir=Path.cwd(),
            plan_file=plan_file,
            goal=goal,
            seed_file=seed_file,
            model_override=model,
            cli=cli,
        )
        return

    workdir = Path.cwd()
    if not plan_only:
        estimate = _estimate_run_preview(
            workdir=workdir,
            plan_file=plan_file,
            goal=goal,
            seed_file=seed_file,
            model_override=model,
        )
        _emit_preflight_runtime_warnings(
            workdir=workdir,
            estimate=estimate,
            auto_approve=auto_approve,
            quiet=quiet,
        )

    # --plan_file: loadable YAML plan (stages + steps)
    if plan_file is not None:
        try:
            tasks = load_plan_from_yaml(plan_file)
            _resolve_depends_on(tasks)
            # Create a synthetic goal from the plan name
            try:
                import yaml as _yaml

                plan_data = _yaml.safe_load(plan_file.read_text())
                goal = plan_data.get("name", str(plan_file))
            except Exception:
                goal = str(plan_file)

            console.print(f"[dim]Loaded plan from:[/dim] {plan_file}")
            console.print(f"[dim]Plan name:[/dim] {goal}")
            loaded_goal = goal or str(plan_file)

            with _make_profile_ctx(profile, workdir), _quiet_bootstrap_console(quiet):
                bootstrap_from_goal(
                    goal=loaded_goal,
                    workdir=workdir,
                    port=port,
                    cells=cells,
                    cli=cli or "auto",
                    model=model,
                    tasks=tasks,
                    ab_test=ab_test,
                )

            _finalize_run_output(quiet=quiet)
            return
        except Exception as exc:
            console.print(f"[red]Failed to load plan file:[/red] {exc}")
            raise SystemExit(1) from exc

    # --from-plan: load goal from saved plan file, override inline goal
    elif from_plan is not None:
        try:
            goal = _load_plan_goal(from_plan)
            console.print(f"[dim]Loaded plan from:[/dim] {from_plan}")
            console.print(f"[dim]Goal:[/dim] {goal[:100]}")
        except (ValueError, OSError) as exc:
            console.print(f"[red]Failed to load plan:[/red] {exc}")
            raise SystemExit(1) from exc

    # --plan-only: build a synthetic plan, render to markdown, save, and exit
    if plan_only:
        from bernstein.core.plan_builder import PlanBuilder
        from bernstein.core.seed import SeedError, parse_seed

        effective_goal = goal
        team: list[str] | None = None

        if effective_goal is None:
            # Resolve seed file
            if seed_file is not None:
                seed_path = Path(seed_file)
            else:
                found = find_seed_file()
                if found is not None:
                    seed_path = found
                else:
                    from bernstein.cli.errors import no_seed_or_goal

                    no_seed_or_goal().print()
                    raise SystemExit(1)
            try:
                seed = parse_seed(seed_path)
                effective_goal = seed.goal
                team = list(seed.team) if seed.team != "auto" else None
            except SeedError as exc:
                from bernstein.cli.errors import seed_parse_error

                seed_parse_error(exc).print()
                raise SystemExit(1) from exc

        plan_obj, tasks = _build_synthetic_plan(effective_goal, team)
        builder = PlanBuilder(plan_obj, tasks)
        md = builder.render_to_markdown()

        # Render to terminal
        from rich.markdown import Markdown

        console.print(Markdown(md))

        # Save to file
        plan_file = _save_plan_markdown(md, workdir)
        console.print(f"\n[dim]Plan saved to:[/dim] {plan_file}")
        console.print(f"[dim]Execute with:[/dim] bernstein run --from-plan {plan_file}")
        return

    # Confirmation prompt before execution (skip with --auto-approve)
    if not auto_approve:
        effective_goal_for_confirm = goal
        team_for_confirm: list[str] | None = None

        if effective_goal_for_confirm is None:
            # Peek at seed to get goal for confirmation display
            if seed_file is not None:
                _peek_path: Path | None = Path(seed_file)
            else:
                _peek_path = find_seed_file()

            if _peek_path is not None:
                try:
                    from bernstein.core.seed import parse_seed as _parse_seed

                    _seed = _parse_seed(_peek_path)
                    effective_goal_for_confirm = _seed.goal
                    team_for_confirm = list(_seed.team) if _seed.team != "auto" else None
                except Exception:
                    pass

        if effective_goal_for_confirm:
            plan_obj, plan_tasks = _build_synthetic_plan(effective_goal_for_confirm, team_for_confirm)
            from bernstein.cli.plan_display import display_plan_and_confirm

            if not display_plan_and_confirm(plan_obj, plan_tasks, console=console):
                return
        elif not auto_approve:
            # No goal resolved -- fall back to simple confirmation
            try:
                if not click.confirm("\nProceed with execution?", default=True):
                    console.print("[dim]Cancelled.[/dim]")
                    return
            except (UnicodeDecodeError, EOFError):
                # Non-ASCII input (e.g. Cyrillic keyboard) -- treat as "yes"
                pass

    if goal is not None:
        # Inline goal mode -- no YAML needed
        try:
            with _make_profile_ctx(profile, workdir), _quiet_bootstrap_console(quiet):
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
        _finalize_run_output(quiet=quiet)
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

    if not quiet:
        console.print(f"[dim]Using seed file:[/dim] {path}")
    try:
        # CLI --cells overrides seed file value when explicitly set (cells > 1)
        cli_cells: int | None = cells if cells > 1 else None
        with _make_profile_ctx(profile, workdir), _quiet_bootstrap_console(quiet):
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

    _finalize_run_output(quiet=quiet)


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

    from bernstein.core.bootstrap import (  # pyright: ignore[reportUnknownVariableType]
        bootstrap_from_goal,
        bootstrap_from_seed,
    )
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

    elapsed_str = f"{elapsed_secs:.0f}s" if elapsed_secs > 0 else "—"

    console.print("\n[bold cyan]── Demo Summary ──────────────────────────[/bold cyan]")

    table = Table(show_header=True, header_style="bold magenta", show_lines=False)
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

        plan_table = Table(show_header=True, header_style="bold magenta")
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
    console.print(f"\n[dim]Creating demo project in {project_dir}…[/dim]")

    setup_demo_project(project_dir, detected)
    console.print("[green]✓[/green] Flask app with 4 intentional bugs created")
    console.print(
        "[green]✓[/green] 4 bug-fix tasks seeded: off-by-one · missing import · wrong status code · broken test"
    )

    server_url = f"http://127.0.0.1:{_DEMO_PORT}"
    orchestration_start = time.monotonic()

    try:
        # Bootstrap: start server + spawner in the demo project dir
        console.print("\n[bold]Starting orchestration…[/bold]")
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

                        # Emit a line for each newly-completed task
                        for t in tasks_list:
                            tid = t.get("id", "")
                            title = (t.get("title") or "")[:60]
                            role = t.get("role", "agent")
                            if t.get("status") == "done" and tid not in seen_done:
                                seen_done.add(tid)
                                progress.console.print(f"  [green]✓[/green] [{role}] {title}")
                            elif t.get("status") == "failed" and tid not in seen_failed:
                                seen_failed.add(tid)
                                progress.console.print(f"  [red]✗[/red] [{role}] {title}")

                        progress.update(
                            poll_task,
                            description=(
                                f"Agents working… "
                                f"[green]{done_count}[/green]/{total_tasks} bugs fixed"
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

    elapsed = time.monotonic() - orchestration_start
    _print_demo_summary(project_dir, server_url, elapsed_secs=elapsed)
