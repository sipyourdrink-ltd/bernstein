"""Load and parse YAML project plans."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import yaml

from bernstein.core.models import CompletionSignal, Complexity, Scope, Task, TaskStatus, TaskType

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class PlanLoadError(Exception):
    """Raised when a plan file cannot be loaded or parsed."""


@dataclass
class RepoRef:
    """A repository reference declared in a multi-repo plan.

    Attributes:
        path: Relative or absolute path to the repository root.
        branch: Branch to check out in that repo (default: "main").
        name: Optional logical name for referencing in stage ``repo:`` fields.
            Falls back to the last component of ``path`` when omitted.
    """

    path: str
    branch: str = "main"
    name: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.path.rstrip("/").rsplit("/", 1)[-1]


@dataclass
class PlanConfig:
    """Top-level metadata and orchestration settings extracted from a plan file.

    Attributes:
        name: Short plan name used as the orchestration goal.
        description: Human-readable summary of what the plan achieves.
        constraints: Global constraints injected into every agent context.
        context_files: Extra files injected into agent context.
        cli: CLI agent override (e.g. "claude", "codex", "auto").
        budget: Spending cap string (e.g. "$10", "5.00").
        max_agents: Max concurrent agent processes.
        repos: Repositories involved in a multi-repo plan.  When present,
            stages can declare a ``repo`` field to route their tasks to a
            specific repository.
    """

    name: str = ""
    description: str = ""
    constraints: list[str] = field(default_factory=list)
    context_files: list[str] = field(default_factory=list)
    cli: str | None = None
    budget: str | None = None
    max_agents: int | None = None
    repos: list[RepoRef] = field(default_factory=list)


def _parse_completion_signals(raw_signals: list[object]) -> list[CompletionSignal]:
    """Parse a list of completion signal dicts from YAML into CompletionSignal objects.

    Invalid entries are logged and skipped.

    Args:
        raw_signals: List of raw YAML signal dicts.

    Returns:
        List of valid CompletionSignal instances.
    """
    valid_types: set[str] = {"path_exists", "glob_exists", "test_passes", "file_contains", "llm_review", "llm_judge"}
    signals: list[CompletionSignal] = []
    for i, raw in enumerate(raw_signals):
        if not isinstance(raw, dict):
            logger.warning("completion_signals[%d] is not a mapping — skipping", i)
            continue
        sig_type = raw.get("type", "")
        # Support 'value' or 'path'/'command'/'contains' as the signal value
        sig_value = raw.get("value") or raw.get("path") or raw.get("command") or raw.get("contains") or ""
        if sig_type not in valid_types:
            logger.warning("completion_signals[%d] has invalid type %r — skipping", i, sig_type)
            continue
        if not sig_value:
            logger.warning("completion_signals[%d] has empty value — skipping", i)
            continue
        signals.append(CompletionSignal(type=sig_type, value=str(sig_value)))  # type: ignore[arg-type]
    return signals


def _step_title(step: dict[object, object], stage_name: str, step_idx: int) -> str:
    """Extract the step title from a step dict, supporting both 'title' and 'goal' keys.

    Args:
        step: Step dict from the YAML plan.
        stage_name: Stage name for error context.
        step_idx: Zero-based step index for error context.

    Returns:
        Non-empty title string.

    Raises:
        PlanLoadError: If neither 'title' nor 'goal' is present or both are empty.
    """
    title = step.get("title") or step.get("goal")
    if not title:
        raise PlanLoadError(f"Step {step_idx} in stage {stage_name!r} is missing a 'title' (or 'goal') field")
    return str(title)


def load_plan(path: Path) -> tuple[PlanConfig, list[Task]]:
    """Load a YAML plan file and return plan-level config plus a list of Task objects.

    The plan format uses ``stages`` and ``steps``. Stages run sequentially by
    default; steps within a stage run in parallel. Use ``depends_on`` at the
    stage level to express cross-stage dependencies.

    Steps support both ``title`` (preferred) and ``goal`` (legacy) as the task
    title field.

    Args:
        path: Path to the YAML plan file.

    Returns:
        Tuple of (PlanConfig, list[Task]).  ``depends_on`` on each task contains
        the *titles* of the tasks it depends on; call
        ``_resolve_depends_on(tasks)`` from ``manager_parsing`` to map them to
        generated task IDs before submitting to the task server.

    Raises:
        PlanLoadError: If the file is missing, invalid YAML, or missing required fields.
    """
    if not path.exists():
        raise PlanLoadError(f"Plan file not found: {path}")

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PlanLoadError(f"Failed to parse YAML plan: {exc}") from exc

    if not isinstance(data, dict):
        raise PlanLoadError("Plan file must be a YAML mapping")

    stages = data.get("stages")
    if not stages:
        raise PlanLoadError("Plan file must contain a 'stages' list")

    # Extract plan-level config
    budget_raw = data.get("budget")
    max_agents_raw = data.get("max_agents")

    # Parse optional repos list for multi-repo plans
    repos: list[RepoRef] = []
    for raw_repo in data.get("repos") or []:
        if not isinstance(raw_repo, dict):
            logger.warning("repos entry is not a mapping — skipping")
            continue
        repo_path = raw_repo.get("path")
        if not repo_path:
            logger.warning("repos entry missing 'path' — skipping")
            continue
        repos.append(
            RepoRef(
                path=str(repo_path),
                branch=str(raw_repo.get("branch", "main")),
                name=str(raw_repo.get("name", "")),
            )
        )

    config = PlanConfig(
        name=str(data.get("name", "")),
        description=str(data.get("description", "")),
        constraints=list(data.get("constraints") or []),
        context_files=list(data.get("context_files") or []),
        cli=str(data["cli"]) if data.get("cli") else None,
        budget=str(budget_raw) if budget_raw is not None else None,
        max_agents=int(max_agents_raw) if max_agents_raw is not None else None,
        repos=repos,
    )

    tasks: list[Task] = []
    # Map stage name -> list of task titles produced by that stage
    stage_tasks: dict[str, list[str]] = {}

    for i, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise PlanLoadError(f"Stage {i} must be a mapping")

        stage_name = stage.get("name")
        if not stage_name:
            raise PlanLoadError(f"Stage {i} is missing a name")

        steps = stage.get("steps")
        if not steps:
            logger.warning("Stage %r has no steps", stage_name)
            stage_tasks[str(stage_name)] = []
            continue

        stage_tasks[str(stage_name)] = []
        stage_deps: list[str] = [str(d) for d in (stage.get("depends_on") or [])]

        # Stage-level repo routing: steps inherit the stage repo unless overridden
        stage_repo: str | None = str(stage["repo"]) if stage.get("repo") else None

        for j, step in enumerate(steps):
            if not isinstance(step, dict):
                raise PlanLoadError(f"Step {j} in stage {stage_name!r} must be a mapping")

            title = _step_title(step, str(stage_name), j)

            # Resolve cross-stage dependencies to the *titles* of upstream tasks
            depends_on: list[str] = []
            for dep_stage in stage_deps:
                if dep_stage in stage_tasks:
                    depends_on.extend(stage_tasks[dep_stage])
                else:
                    logger.warning("Stage %r depends on unknown stage %r", stage_name, dep_stage)

            # Parse completion signals if present
            raw_signals: list[object] = list(step.get("completion_signals") or [])
            signals = _parse_completion_signals(raw_signals)

            # Map 'files' → owned_files
            owned_files: list[str] = [str(f) for f in (step.get("files") or [])]

            # Optional routing overrides
            model_raw = step.get("model")
            effort_raw = step.get("effort")
            estimated_minutes_raw = step.get("estimated_minutes")

            # Execution mode: "batch" delegates to Claude Code /batch skill
            mode_raw = step.get("mode")
            execution_mode: str | None = str(mode_raw) if mode_raw else None

            # Repo routing: step-level overrides stage-level
            step_repo_raw = step.get("repo")
            task_repo: str | None = str(step_repo_raw) if step_repo_raw else stage_repo

            # Cross-repo dependency: which repo must complete first
            depends_on_repo_raw = step.get("depends_on_repo")
            task_depends_on_repo: str | None = str(depends_on_repo_raw) if depends_on_repo_raw else None

            task = Task(
                id=f"plan-{i}-{j}",
                title=title,
                description=str(step.get("description", title)),
                role=str(step.get("role", "backend")),
                priority=int(step.get("priority", 2)),
                scope=Scope(step.get("scope", "medium")),
                complexity=Complexity(step.get("complexity", "medium")),
                estimated_minutes=int(estimated_minutes_raw) if estimated_minutes_raw is not None else 30,
                status=TaskStatus.OPEN,
                task_type=TaskType.STANDARD,
                depends_on=depends_on,
                owned_files=owned_files,
                completion_signals=signals,
                model=str(model_raw) if model_raw else None,
                effort=str(effort_raw) if effort_raw else None,
                execution_mode=execution_mode,
                repo=task_repo,
                depends_on_repo=task_depends_on_repo,
            )
            tasks.append(task)
            stage_tasks[str(stage_name)].append(title)

    return config, tasks


def load_plan_from_yaml(path: Path) -> list[Task]:
    """Load a YAML plan file and return a list of Task objects.

    This is a convenience wrapper around :func:`load_plan` for callers that
    only need the task list and not the plan-level config.

    Args:
        path: Path to the YAML plan file.

    Returns:
        List of Task objects with dependencies mapped by title.

    Raises:
        PlanLoadError: If the file is missing, invalid YAML, or missing required fields.
    """
    _config, tasks = load_plan(path)
    return tasks
