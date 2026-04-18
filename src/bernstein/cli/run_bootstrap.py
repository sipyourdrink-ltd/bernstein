"""Main Click commands and execution bootstrap for Bernstein runs."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import datetime
import json
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
    print_banner,
    server_get,
)
from bernstein.cli.run_preflight import (
    _configure_quality_gate_bypass,
    _emit_preflight_runtime_warnings,
    _estimate_run_preview,
    _finalize_run_output,
    _make_profile_ctx,
    _quiet_bootstrap_console,
    _show_run_summary,
)
from bernstein.core.cost import estimate_run_cost
from bernstein.core.manager_parsing import _resolve_depends_on  # pyright: ignore[reportPrivateUsage]
from bernstein.core.plan_loader import PlanLoadError, load_plan, load_plan_from_yaml

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


def _load_dry_run_tasks(plan_file: Path | None) -> list[Any]:
    """Load tasks for a dry run from a plan file or running server.

    Args:
        plan_file: Optional plan file path.

    Returns:
        List of Task objects.

    Raises:
        SystemExit: On plan load error or server connectivity failure.
    """
    from bernstein.core.models import Task

    if plan_file is not None:
        try:
            _plan_config, tasks = load_plan(plan_file)
            return tasks
        except PlanLoadError as exc:
            console.print(f"[red]Plan load error:[/red] {exc}")
            raise SystemExit(1) from exc

    _headers: dict[str, str] = {}
    _token = os.environ.get("BERNSTEIN_AUTH_TOKEN", "")
    if _token:
        _headers["Authorization"] = f"Bearer {_token}"
    try:
        resp = httpx.get(
            "http://127.0.0.1:8052/tasks?status=open",
            headers=_headers,
            timeout=5.0,
        )
        resp.raise_for_status()
        tasks_data = resp.json()
    except httpx.ConnectError as err:
        console.print("[red]Task server not running. Start with `bernstein conduct` first,[/red]")
        console.print("[red]or pass a plan file: `bernstein run --dry-run plan.yaml`[/red]")
        raise SystemExit(1) from err
    except Exception as exc:
        console.print(f"[red]Failed to fetch tasks:[/red] {exc}")
        raise SystemExit(1) from exc

    return [Task.from_dict(td) for td in tasks_data]


def _confirm_run(*, goal: str | None, seed_file: str | None) -> bool:
    """Show confirmation prompt before execution. Returns True to proceed."""
    effective_goal = goal
    team: list[str] | None = None

    if effective_goal is None:
        _peek_path: Path | None = Path(seed_file) if seed_file is not None else find_seed_file()
        if _peek_path is not None:
            try:
                from bernstein.core.seed import parse_seed as _parse_seed

                _seed = _parse_seed(_peek_path)
                effective_goal = _seed.goal
                team = list(_seed.team) if _seed.team != "auto" else None
                from bernstein.core.plan_approval import configure_plan_models

                configure_plan_models(_seed.role_model_policy)
            except Exception:
                pass

    if effective_goal:
        plan_obj, plan_tasks = _build_synthetic_plan(effective_goal, team)
        from bernstein.cli.plan_display import display_plan_and_confirm

        return display_plan_and_confirm(plan_obj, plan_tasks, console=console)

    try:
        if not click.confirm("\nProceed with execution?", default=True):
            console.print("[dim]Cancelled.[/dim]")
            return False
    except (UnicodeDecodeError, EOFError):
        pass
    return True


def _propagate_env_flags(
    *,
    profile: bool,
    workflow: str | None,
    routing: str | None,
    compliance: str | None,
    sandbox: str | None,
    container: bool,
    container_image: str | None,
    two_phase_sandbox: bool,
    quiet: bool,
    task_filter: str | None,
    auto_pr: bool,
    activity_log_path: str | None,
    audit: bool,
) -> None:
    """Set environment variables so orchestrator subprocesses inherit CLI flags."""
    _flag_map: list[tuple[bool, str]] = [
        (profile, "BERNSTEIN_PROFILE"),
        (two_phase_sandbox, "BERNSTEIN_TWO_PHASE_SANDBOX"),
        (quiet, "BERNSTEIN_QUIET"),
        (auto_pr, "BERNSTEIN_AUTO_PR"),
        (audit, "BERNSTEIN_AUDIT"),
    ]
    for flag, key in _flag_map:
        if flag:
            os.environ[key] = "1"

    _str_map: list[tuple[str | None, str]] = [
        (workflow, "BERNSTEIN_WORKFLOW"),
        (routing, "BERNSTEIN_ROUTING"),
        (compliance, "BERNSTEIN_COMPLIANCE"),
        (container_image, "BERNSTEIN_CONTAINER_IMAGE"),
        (task_filter, "BERNSTEIN_TASK_FILTER"),
        (activity_log_path, "BERNSTEIN_ACTIVITY_LOG"),
    ]
    for val, key in _str_map:
        if val:
            os.environ[key] = val

    if sandbox:
        os.environ["BERNSTEIN_CONTAINER"] = "1"
        os.environ["BERNSTEIN_SANDBOX_RUNTIME"] = sandbox.lower()
    elif container:
        os.environ["BERNSTEIN_CONTAINER"] = "1"


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
        _workdir: Project root directory.
        plan_file: Optional plan file path.
        _goal: Optional goal string.
        _seed_file: Optional seed file path.
        model_override: Optional model override.
        _cli: Optional CLI override.
    """
    _ = workdir  # Part of interface
    _ = seed_file  # Part of interface
    _ = cli  # Part of interface
    from rich.table import Table

    console.print("\n[bold]Dry-run mode — no agents will be spawned.[/bold]\n")

    tasks = _load_dry_run_tasks(plan_file)

    if not tasks:
        console.print("[yellow]No tasks to schedule.[/yellow]")
        return

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

    deps_found = False
    for task in tasks:
        if task.depends_on:
            if not deps_found:
                console.print("\n[bold]Dependency graph:[/bold]")
                deps_found = True
            dep_str = ", ".join(task.depends_on)
            console.print(f"  {task.title} -> [{dep_str}]")

    est_model = model_override or "sonnet"
    low_usd, high_usd = estimate_run_cost(len(tasks), est_model)
    console.print(f"\n  Total tasks: {len(tasks)}")
    console.print(f"  Estimated cost: ${(low_usd + high_usd) / 2:.2f} (${low_usd:.2f}-${high_usd:.2f})")

    console.print("\n[green]Dry run complete. No agents were spawned.[/green]")


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


@click.command("init")
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


def exec_restart() -> None:
    """Re-exec the current process as ``bernstein run`` (full stack restart).

    On macOS/Linux, uses ``os.execv`` which replaces the current process
    image entirely — no orphan.  On Windows, ``os.execv`` does not truly
    replace the process (it spawns a child), so we use ``subprocess.Popen``
    and ``sys.exit`` instead.
    """
    import subprocess

    argv = [sys.executable, "-m", "bernstein.cli.main", "run"]
    if sys.platform == "win32":
        # Windows: execv creates a child process and the parent stays alive,
        # so we spawn explicitly and exit the current process.
        subprocess.Popen(argv, close_fds=True)
        raise SystemExit(0)
    else:
        # Unix: execv replaces the process image — clean restart.
        os.execv(sys.executable, argv)


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
    type=click.Choice(["static", "bandit", "bandit-shadow"], case_sensitive=False),
    help=(
        "Model routing strategy: 'static' = fixed cascade heuristics (default), "
        "'bandit' = contextual LinUCB bandit that learns cost-quality tradeoffs, "
        "'bandit-shadow' = log bandit decisions without changing live routing."
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
@click.option(
    "--task",
    "-t",
    "task_filter",
    default=None,
    metavar="PATTERN",
    help="Run only backlog tasks matching PATTERN (e.g. 'gh-62' or 'mutant-fish').",
)
@click.option(
    "--auto-pr",
    is_flag=True,
    default=False,
    help="Automatically create a GitHub PR when all tasks complete.",
)
@click.option(
    "--activity-log",
    "activity_log_path",
    is_flag=False,
    flag_value=".sdd/logs/activity.log",
    default=None,
    help="Write activity to log file (default: .sdd/logs/activity.log).",
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
    task_filter: str | None = None,
    auto_pr: bool = False,
    activity_log_path: str | None = None,
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
      bernstein conduct --routing bandit-shadow  # log bandit decisions without changing live routing
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

    _propagate_env_flags(
        profile=profile,
        workflow=workflow,
        routing=routing,
        compliance=compliance,
        sandbox=sandbox,
        container=container,
        container_image=container_image,
        two_phase_sandbox=two_phase_sandbox,
        quiet=quiet,
        task_filter=task_filter,
        auto_pr=auto_pr,
        activity_log_path=activity_log_path,
        audit=audit,
    )

    _configure_quality_gate_bypass(
        goal=goal,
        seed_file=seed_file,
        skip_gate=skip_gate,
        skip_gate_reason=skip_gate_reason,
    )

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
            plan_approval_follows=not auto_approve,
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
    if not auto_approve and not _confirm_run(goal=goal, seed_file=seed_file):
        return

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


@click.command("start")
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
