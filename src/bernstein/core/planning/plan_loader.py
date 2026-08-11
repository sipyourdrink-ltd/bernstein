"""Load and parse YAML project plans."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

import yaml

from bernstein.core.models import CompletionSignal, Complexity, Scope, Task, TaskStatus, TaskType
from bernstein.core.tasks.artifacts import ArtifactSpec, ArtifactSpecError, parse_artifact_spec

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class PlanLoadError(Exception):
    """Raised when a plan file cannot be loaded or parsed."""


# Top-level keys that identify a seed config (``bernstein.yaml`` shape) rather
# than a staged plan. ``goal`` is the required seed field; ``model`` is a
# common companion. Deliberately excludes ``cli``, which is also a valid
# top-level ``PlanConfig`` key (a staged plan may set a default CLI for all
# its steps) -- keying off it would mislabel a genuine plan with a missing or
# typo'd ``stages`` list as a seed. Presence of a key from this set on a file
# that is missing ``stages`` is a reliable signal that the file was meant to
# be a seed, not a plan (issue #3009).
_SEED_SHAPED_KEYS = ("goal", "model")


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
            logger.warning("completion_signals[%d] is not a mapping - skipping", i)
            continue
        sig_type = raw.get("type", "")
        # Support 'value' or 'path'/'command'/'contains' as the signal value
        sig_value = raw.get("value") or raw.get("path") or raw.get("command") or raw.get("contains") or ""
        if sig_type not in valid_types:
            logger.warning("completion_signals[%d] has invalid type %r - skipping", i, sig_type)
            continue
        if not sig_value:
            logger.warning("completion_signals[%d] has empty value - skipping", i)
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


def _parse_enum_field[EnumT: Enum](
    enum_cls: type[EnumT],
    raw: object,
    field_name: str,
    stage_name: str,
    step_idx: int,
) -> EnumT:
    """Convert a raw YAML value to *enum_cls*, guarding the conversion.

    A bare ``ValueError`` out of an enum constructor escapes ``load_plan``'s
    documented failure contract -- callers catch ``PlanLoadError`` alone, so an
    enum typo in a plan surfaced as an unhandled traceback (issue #3515).

    Args:
        enum_cls: The target enum class (e.g. ``Scope``, ``Complexity``).
        raw: Raw value from the step dict.
        field_name: Step field name, for error context.
        stage_name: Stage name for error context.
        step_idx: Zero-based step index for error context.

    Returns:
        The matching enum member.

    Raises:
        PlanLoadError: If *raw* is not one of the enum's values, naming the
            field, the offending value, and the accepted values.
    """
    try:
        return enum_cls(raw)
    except ValueError as exc:
        accepted = ", ".join(str(member.value) for member in enum_cls)
        raise PlanLoadError(
            f"Step {step_idx} in stage {stage_name!r}: invalid {field_name!r} value {raw!r}; accepted: {accepted}"
        ) from exc


def _parse_int_field(
    step: dict[str, Any],
    field_name: str,
    stage_name: str,
    step_idx: int,
    *,
    default: int,
) -> int:
    """Parse an integer step field, sharing validate_plan's boundary contract.

    ``int(...)`` coercion silently accepted string values the schema types as
    integers and let a non-numeric string escape as a bare ``ValueError``
    instead of the documented ``PlanLoadError`` (#3516). Booleans are rejected
    too: JSON Schema's ``integer`` excludes them even though Python's ``bool``
    subclasses ``int``. Presence is checked on the step dict itself so an
    explicit ``null`` is rejected like any other non-integer instead of
    silently receiving the default reserved for an absent key.

    Args:
        step: The step dict the field is read from.
        field_name: Step field name, for error context.
        stage_name: Stage name for error context.
        step_idx: Zero-based step index for error context.
        default: Value returned when the key is absent.

    Returns:
        The integer value.

    Raises:
        PlanLoadError: If the key is present but its value is not an integer
            (including an explicit ``null``).
    """
    if field_name not in step:
        return default
    raw = step[field_name]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise PlanLoadError(
            f"Step {step_idx} in stage {stage_name!r}: {field_name!r} must be an integer, got {type(raw).__name__}"
        )
    return raw


def _step_context(stage_name: str, step_idx: int) -> str:
    """Error-message prefix naming a step by its stage and position."""
    return f"Step {step_idx} in stage {stage_name!r}"


def _parse_string_list_field(raw: object, field_name: str, context: str) -> list[str]:
    """Parse an optional list-of-strings field.

    Args:
        raw: Raw field value from the YAML document.
        field_name: Field name, for error context.
        context: Where in the document the field was found, used verbatim as
            the error-message prefix -- ``"Plan"``, ``"Stage 'build'"``, or
            ``_step_context(...)``. The same list fields exist at three levels
            of a plan file, so the message has to name the level the reader
            must go and edit; a step-shaped prefix on a plan-level field would
            send them to a step that does not exist.

    Returns:
        The string items, or an empty list when the field is absent or ``null``.

    Raises:
        PlanLoadError: If the field is present but not a list, or if any list
            item is not a string.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PlanLoadError(f"{context}: {field_name!r} must be a list of strings, got {type(raw).__name__}")
    items = cast(list[object], raw)
    parsed: list[str] = []
    for item_idx, item in enumerate(items):
        if not isinstance(item, str):
            raise PlanLoadError(f"{context}: {field_name}[{item_idx}] must be a string, got {type(item).__name__}")
        parsed.append(item)
    return parsed


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
        PlanLoadError: If the file is missing, invalid YAML, missing required
            fields, or a step's enum-typed field (``scope``, ``complexity``)
            carries a value outside the enum.
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
        found_seed_keys = [key for key in _SEED_SHAPED_KEYS if key in data]
        if found_seed_keys:
            found = ", ".join(repr(key) for key in found_seed_keys)
            raise PlanLoadError(
                f"{path} looks like a seed config, not a staged plan (found "
                f"{found} but no 'stages' list). Name it 'bernstein.yaml' so "
                f"it is auto-discovered, pass it with '--seed {path}', or add "
                "a 'stages:' list to make it a plan."
            )
        raise PlanLoadError("Plan file must contain a 'stages' list")

    # Extract plan-level config
    budget_raw = data.get("budget")
    max_agents_raw = data.get("max_agents")

    # Parse optional repos list for multi-repo plans
    repos: list[RepoRef] = []
    for raw_repo in data.get("repos") or []:
        if not isinstance(raw_repo, dict):
            logger.warning("repos entry is not a mapping - skipping")
            continue
        repo_path = raw_repo.get("path")
        if not repo_path:
            logger.warning("repos entry missing 'path' - skipping")
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
        constraints=_parse_string_list_field(data.get("constraints"), "constraints", "Plan"),
        context_files=_parse_string_list_field(data.get("context_files"), "context_files", "Plan"),
        cli=str(data["cli"]) if data.get("cli") else None,
        budget=str(budget_raw) if budget_raw is not None else None,
        max_agents=int(max_agents_raw) if max_agents_raw is not None else None,
        repos=repos,
    )

    tasks: list[Task] = []
    stage_tasks: dict[str, list[str]] = {}

    for i, stage in enumerate(stages):
        _parse_stage(stage, i, stage_tasks, tasks)

    # Issue #3375: plan-level ``context_files`` must reach the workers, but
    # every caller that needs only the task list discards the config half of
    # this return value. Stamping the declaration onto each task's metadata
    # here - the key the spawner reads at dispatch - closes that gap at the
    # source instead of relying on callers to carry the config through.
    if config.context_files:
        for task in tasks:
            task.metadata["context_files"] = list(config.context_files)

    return config, tasks


def _parse_stage(
    stage: object,
    stage_index: int,
    stage_tasks: dict[str, list[str]],
    tasks: list[Task],
) -> None:
    """Parse a single stage and append its tasks to *tasks*."""
    if not isinstance(stage, dict):
        raise PlanLoadError(f"Stage {stage_index} must be a mapping")

    stage_name = stage.get("name")
    if not stage_name:
        raise PlanLoadError(f"Stage {stage_index} is missing a name")

    steps = stage.get("steps")
    if not steps:
        logger.warning("Stage %r has no steps", stage_name)
        stage_tasks[str(stage_name)] = []
        return

    stage_tasks[str(stage_name)] = []
    stage_deps: list[str] = _parse_string_list_field(stage.get("depends_on"), "depends_on", f"Stage {stage_name!r}")
    stage_repo: str | None = str(stage["repo"]) if stage.get("repo") else None

    for j, step in enumerate(steps):
        task = _parse_step(step, stage_index, j, str(stage_name), stage_deps, stage_tasks, stage_repo)
        tasks.append(task)
        stage_tasks[str(stage_name)].append(task.title)


def _parse_step(
    step: object,
    stage_index: int,
    step_index: int,
    stage_name: str,
    stage_deps: list[str],
    stage_tasks: dict[str, list[str]],
    stage_repo: str | None,
) -> Task:
    """Parse a single step dict into a Task."""
    if not isinstance(step, dict):
        raise PlanLoadError(f"Step {step_index} in stage {stage_name!r} must be a mapping")

    title = _step_title(step, stage_name, step_index)

    depends_on: list[str] = []
    for dep_stage in stage_deps:
        if dep_stage in stage_tasks:
            depends_on.extend(stage_tasks[dep_stage])
        else:
            logger.warning("Stage %r depends on unknown stage %r", stage_name, dep_stage)

    raw_signals: list[object] = list(step.get("completion_signals") or [])
    signals = _parse_completion_signals(raw_signals)
    # Reject scalar / non-list shapes and non-string items so direct loads
    # match the CLI pre-check (validate_plan) boundary for list[string] fields.
    raw_files: object = step.get("files")
    owned_files = _parse_string_list_field(raw_files, "files", _step_context(stage_name, step_index))
    # Issue #1797: operator-supplied image attachments. The orchestrator
    # builds a MultiModalContext at spawn time from these paths.
    # Reject scalar / non-list shapes so a YAML typo like
    # ``attachments: ./shot.png`` does not silently iterate the string
    # character-by-character. (bot-ack: 3284182784 -- CodeRabbit major.)
    raw_attachments = step.get("attachments")
    if raw_attachments is None:
        attachments: list[str] = []
    elif isinstance(raw_attachments, list):
        attachments = [str(a) for a in raw_attachments]
    else:
        raise PlanLoadError(
            f"Step {step_index} in stage {stage_name!r}: 'attachments' must be a "
            f"list of paths, got {type(raw_attachments).__name__}"
        )

    model_raw = step.get("model")
    effort_raw = step.get("effort")
    cli_raw = step.get("cli")
    mode_raw = step.get("mode")
    execution_mode: str | None = str(mode_raw) if mode_raw else None
    step_repo_raw = step.get("repo")
    task_repo: str | None = str(step_repo_raw) if step_repo_raw else stage_repo
    depends_on_repo_raw = step.get("depends_on_repo")
    task_depends_on_repo: str | None = str(depends_on_repo_raw) if depends_on_repo_raw else None

    # Issue #3110: the declared artifact contract. Parsed by the one strict
    # parser every declaration surface shares. Fail-closed: a malformed block
    # aborts the plan load naming the offending field - it must never fall
    # back to the default code_diff contract, because a task that silently
    # completes on a git SHA is the wrong completion identity for the
    # artifact the operator declared.
    raw_artifact = step.get("artifact_spec")
    artifact_spec = ArtifactSpec.default()
    if raw_artifact is not None:
        try:
            artifact_spec = parse_artifact_spec(raw_artifact)
        except ArtifactSpecError as exc:
            raise PlanLoadError(f"Step {step_index} in stage {stage_name!r}: {exc}") from exc

    metadata: dict[str, object] = {}
    phases_raw = step.get("phases")
    if phases_raw:
        from bernstein.core.orchestration.phase_pipeline import parse_phases

        try:
            parsed = parse_phases(phases_raw)
        except ValueError as exc:
            raise PlanLoadError(f"Step {step_index} in stage {stage_name!r}: invalid 'phases' field - {exc}") from exc
        if parsed:
            metadata["phases"] = [p.value for p in parsed]

    return Task(
        id=f"plan-{stage_index}-{step_index}",
        title=title,
        description=str(step.get("description", title)),
        role=str(step.get("role", "backend")),
        priority=_parse_int_field(step, "priority", stage_name, step_index, default=2),
        scope=_parse_enum_field(Scope, step.get("scope", "medium"), "scope", stage_name, step_index),
        complexity=_parse_enum_field(
            Complexity, step.get("complexity", "medium"), "complexity", stage_name, step_index
        ),
        estimated_minutes=_parse_int_field(step, "estimated_minutes", stage_name, step_index, default=30),
        status=TaskStatus.OPEN,
        task_type=TaskType.STANDARD,
        depends_on=depends_on,
        owned_files=owned_files,
        completion_signals=signals,
        artifact_spec=artifact_spec,
        model=str(model_raw) if model_raw else None,
        effort=str(effort_raw) if effort_raw else None,
        cli=str(cli_raw) if cli_raw else None,
        execution_mode=execution_mode,
        repo=task_repo,
        depends_on_repo=task_depends_on_repo,
        metadata=metadata,
        attachments=attachments,
    )


def load_plan_from_yaml(path: Path) -> list[Task]:
    """Load a YAML plan file and return a list of Task objects.

    This is a convenience wrapper around :func:`load_plan` for callers that
    only need the task list and not the plan-level config.

    Args:
        path: Path to the YAML plan file.

    Returns:
        List of Task objects with dependencies mapped by title.

    Raises:
        PlanLoadError: If the file is missing, invalid YAML, missing required
            fields, or a step's enum-typed field (``scope``, ``complexity``)
            carries a value outside the enum.
    """
    _config, tasks = load_plan(path)
    return tasks
