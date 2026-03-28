"""Fast-path execution for trivial tasks that don't need an LLM agent.

Classifies tasks into complexity levels:
- L0 (Trivial): formatting, import sorting, renames, config tweaks
  -> Deterministic execution via ruff/AST/regex, no LLM needed
- L1 (Simple): add a test, update docstring, fix lint error
  -> Route to cheapest model (Haiku)
- L2+ (Complex): feature work, refactoring, architecture
  -> Full LLM agent (current behavior)

L0 tasks bypass the spawner entirely, saving LLM cost and executing in <1s.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from bernstein.core.metrics import get_collector
from bernstein.core.models import Complexity, ModelConfig, Scope, Task

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class TaskLevel(Enum):
    """Task complexity level for fast-path routing."""
    L0 = "L0"  # Trivial: deterministic execution, no LLM
    L1 = "L1"  # Simple: cheapest model (Haiku)
    L2 = "L2"  # Complex: full LLM agent


class FastPathAction(Enum):
    """What deterministic action to run for an L0 task."""
    RUFF_FORMAT = "ruff_format"
    RUFF_FIX = "ruff_fix"
    SORT_IMPORTS = "sort_imports"
    RENAME_SYMBOL = "rename_symbol"


@dataclass
class ClassificationResult:
    """Result of classifying a task's complexity level."""
    level: TaskLevel
    action: FastPathAction | None = None  # Set for L0 tasks
    confidence: float = 1.0
    reason: str = ""
    matched_rule: str | None = None


@dataclass
class FastPathResult:
    """Result of executing a task via fast-path."""
    success: bool
    action: FastPathAction
    duration_s: float = 0.0
    files_modified: int = 0
    summary: str = ""
    error: str | None = None


@dataclass
class FastPathStats:
    """Cumulative stats for fast-path execution in a session."""
    tasks_bypassed: int = 0
    total_time_saved_s: float = 0.0
    estimated_cost_saved_usd: float = 0.0
    actions: dict[str, int] = field(default_factory=dict)

    def record(self, result: FastPathResult, estimated_llm_cost: float = 0.15) -> None:
        """Record a completed fast-path execution."""
        self.tasks_bypassed += 1
        self.total_time_saved_s += max(60.0 - result.duration_s, 0.0)  # assume ~60s LLM baseline
        self.estimated_cost_saved_usd += estimated_llm_cost
        action_name = result.action.value
        self.actions[action_name] = self.actions.get(action_name, 0) + 1


# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------

# Regex patterns for L0 (trivial) tasks — matched against lowercased title+description
_L0_PATTERNS: list[tuple[re.Pattern[str], FastPathAction, str]] = [
    (re.compile(r"\b(format|formatting|auto-?format|black|prettier)\b"), FastPathAction.RUFF_FORMAT, "formatting"),
    (re.compile(r"\b(lint|linting|ruff fix|fix lint|autofix)\b"), FastPathAction.RUFF_FIX, "lint-fix"),
    (re.compile(r"\b(sort imports?|isort|import order|organiz\w+ imports?)\b"), FastPathAction.SORT_IMPORTS, "import-sort"),
    (re.compile(r"\brename\s+['\"]?\w+['\"]?\s+(?:to|->|=>)\s+['\"]?\w+['\"]?"), FastPathAction.RENAME_SYMBOL, "rename"),
]

# Regex patterns for L1 (simple) tasks — route to cheapest model
_L1_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(add docstring|update docstring|missing docstring)\b"), "docstring"),
    (re.compile(r"\b(add type hint|type annotation|add typing)\b"), "type-hint"),
    (re.compile(r"\b(fix typo|spelling|typo)\b"), "typo-fix"),
]


def classify_task(task: Task) -> ClassificationResult:
    """Classify a task into L0/L1/L2 based on title and description.

    Uses rule-based pattern matching on the task text. Tasks with
    high complexity, large scope, or certain roles (manager, architect,
    security) are never fast-pathed.

    Args:
        task: Task to classify.

    Returns:
        ClassificationResult with the determined level and action.
    """
    # Never fast-path high-stakes tasks
    if task.role in ("manager", "architect", "security"):
        return ClassificationResult(level=TaskLevel.L2, reason=f"role={task.role} excluded")

    if task.complexity == Complexity.HIGH:
        return ClassificationResult(level=TaskLevel.L2, reason="high complexity")

    if task.scope == Scope.LARGE:
        return ClassificationResult(level=TaskLevel.L2, reason="large scope")

    if task.priority == 1:
        return ClassificationResult(level=TaskLevel.L2, reason="critical priority")

    # Check for manager-specified model/effort overrides (respect explicit routing)
    if task.model and task.model.lower() in ("opus",):
        return ClassificationResult(level=TaskLevel.L2, reason="manager requested opus")

    text = f"{task.title} {task.description}".lower()

    # Check L0 patterns
    for pattern, action, rule_name in _L0_PATTERNS:
        if pattern.search(text):
            return ClassificationResult(
                level=TaskLevel.L0,
                action=action,
                confidence=0.9,
                reason=f"matched L0 rule: {rule_name}",
                matched_rule=rule_name,
            )

    # Check L1 patterns
    for pattern, rule_name in _L1_PATTERNS:
        if pattern.search(text):
            return ClassificationResult(
                level=TaskLevel.L1,
                action=None,
                confidence=0.85,
                reason=f"matched L1 rule: {rule_name}",
                matched_rule=rule_name,
            )

    # Low complexity + small scope → L1 candidate
    if task.complexity == Complexity.LOW and task.scope == Scope.SMALL:
        return ClassificationResult(
            level=TaskLevel.L1,
            confidence=0.7,
            reason="low complexity + small scope",
        )

    return ClassificationResult(level=TaskLevel.L2, reason="no fast-path match")


# ---------------------------------------------------------------------------
# L0 executors — deterministic, no LLM
# ---------------------------------------------------------------------------

def _run_ruff_format(workdir: Path, owned_files: list[str]) -> FastPathResult:
    """Run ruff format on owned files or entire project."""
    start = time.monotonic()
    targets = owned_files if owned_files else ["."]

    try:
        proc = subprocess.run(
            ["uv", "run", "ruff", "format", *targets],
            capture_output=True,
            text=True,
            cwd=workdir,
            timeout=30,
        )
        duration = time.monotonic() - start
        changed = proc.stdout.count("reformatted") if proc.stdout else 0

        if proc.returncode not in (0, 1):  # ruff returns 1 when files were changed
            return FastPathResult(
                success=False,
                action=FastPathAction.RUFF_FORMAT,
                duration_s=duration,
                error=proc.stderr[:500] if proc.stderr else "ruff format failed",
            )

        return FastPathResult(
            success=True,
            action=FastPathAction.RUFF_FORMAT,
            duration_s=duration,
            files_modified=changed,
            summary=f"ruff format: {changed} file(s) reformatted in {duration:.1f}s",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return FastPathResult(
            success=False,
            action=FastPathAction.RUFF_FORMAT,
            duration_s=time.monotonic() - start,
            error=str(exc),
        )


def _run_ruff_fix(workdir: Path, owned_files: list[str]) -> FastPathResult:
    """Run ruff check --fix on owned files or entire project."""
    start = time.monotonic()
    targets = owned_files if owned_files else ["."]

    try:
        proc = subprocess.run(
            ["uv", "run", "ruff", "check", "--fix", *targets],
            capture_output=True,
            text=True,
            cwd=workdir,
            timeout=30,
        )
        duration = time.monotonic() - start
        fixed = proc.stdout.count("Fixed") if proc.stdout else 0

        return FastPathResult(
            success=proc.returncode in (0, 1),
            action=FastPathAction.RUFF_FIX,
            duration_s=duration,
            files_modified=fixed,
            summary=f"ruff fix: {fixed} issue(s) fixed in {duration:.1f}s",
            error=proc.stderr[:500] if proc.returncode not in (0, 1) and proc.stderr else None,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return FastPathResult(
            success=False,
            action=FastPathAction.RUFF_FIX,
            duration_s=time.monotonic() - start,
            error=str(exc),
        )


def _run_sort_imports(workdir: Path, owned_files: list[str]) -> FastPathResult:
    """Run ruff check --select I --fix to sort imports."""
    start = time.monotonic()
    targets = owned_files if owned_files else ["."]

    try:
        proc = subprocess.run(
            ["uv", "run", "ruff", "check", "--select", "I", "--fix", *targets],
            capture_output=True,
            text=True,
            cwd=workdir,
            timeout=30,
        )
        duration = time.monotonic() - start
        fixed = proc.stdout.count("Fixed") if proc.stdout else 0

        return FastPathResult(
            success=proc.returncode in (0, 1),
            action=FastPathAction.SORT_IMPORTS,
            duration_s=duration,
            files_modified=fixed,
            summary=f"import sort: {fixed} file(s) fixed in {duration:.1f}s",
            error=proc.stderr[:500] if proc.returncode not in (0, 1) and proc.stderr else None,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return FastPathResult(
            success=False,
            action=FastPathAction.SORT_IMPORTS,
            duration_s=time.monotonic() - start,
            error=str(exc),
        )


def _run_rename(workdir: Path, owned_files: list[str], task: Task | None = None) -> FastPathResult:
    """Rename a symbol across owned files using word-boundary regex replacement.

    Extracts the rename pattern from the task title/description. Falls back to
    failure if the pattern can't be parsed (task should be escalated to an LLM).

    Args:
        workdir: Project root directory.
        owned_files: Files to perform the rename in.
        task: The task containing rename instructions (required for this executor).

    Returns:
        FastPathResult with execution outcome.
    """
    start = time.monotonic()

    if task is None:
        return FastPathResult(
            success=False,
            action=FastPathAction.RENAME_SYMBOL,
            error="rename executor requires a task with title/description",
        )

    text = f"{task.title} {task.description}"
    match = re.search(
        r"rename\s+['\"]?(\w+)['\"]?\s+(?:to|->|=>)\s+['\"]?(\w+)['\"]?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return FastPathResult(
            success=False,
            action=FastPathAction.RENAME_SYMBOL,
            duration_s=time.monotonic() - start,
            error="could not parse rename pattern from task description",
        )

    old_name, new_name = match.group(1), match.group(2)
    targets = owned_files if owned_files else []
    if not targets:
        return FastPathResult(
            success=False,
            action=FastPathAction.RENAME_SYMBOL,
            duration_s=time.monotonic() - start,
            error="no owned_files specified for rename",
        )

    modified = 0
    for rel_path in targets:
        fpath = workdir / rel_path
        if not fpath.is_file():
            continue
        try:
            content = fpath.read_text()
            new_content = re.sub(rf"\b{re.escape(old_name)}\b", new_name, content)
            if new_content != content:
                fpath.write_text(new_content)
                modified += 1
        except OSError as exc:
            logger.warning("Rename failed for %s: %s", rel_path, exc)

    duration = time.monotonic() - start
    return FastPathResult(
        success=True,
        action=FastPathAction.RENAME_SYMBOL,
        duration_s=duration,
        files_modified=modified,
        summary=f"Renamed '{old_name}' -> '{new_name}' in {modified} file(s) in {duration:.1f}s",
    )


# Map actions to executor functions
_EXECUTORS: dict[FastPathAction, object] = {
    FastPathAction.RUFF_FORMAT: _run_ruff_format,
    FastPathAction.RUFF_FIX: _run_ruff_fix,
    FastPathAction.SORT_IMPORTS: _run_sort_imports,
    FastPathAction.RENAME_SYMBOL: _run_rename,
}


def execute_fast_path(
    action: FastPathAction,
    workdir: Path,
    owned_files: list[str],
    task: Task | None = None,
) -> FastPathResult:
    """Execute a deterministic fast-path action.

    Args:
        action: Which fast-path action to run.
        workdir: Project root directory.
        owned_files: Files the task owns (empty = whole project).
        task: The originating task (needed by rename executor to parse old/new names).

    Returns:
        FastPathResult with execution outcome.
    """
    executor = _EXECUTORS.get(action)
    if executor is None:
        return FastPathResult(
            success=False,
            action=action,
            error=f"No executor for action: {action.value}",
        )
    if action == FastPathAction.RENAME_SYMBOL:
        return executor(workdir, owned_files, task=task)  # type: ignore[operator]
    return executor(workdir, owned_files)  # type: ignore[operator]


# ---------------------------------------------------------------------------
# L1 model override — cheapest model for simple tasks
# ---------------------------------------------------------------------------

L1_MODEL_CONFIG = ModelConfig(model="haiku", effort="low", max_tokens=50_000)


def get_l1_model_config() -> ModelConfig:
    """Return the cheapest model config for L1 (simple) tasks."""
    return L1_MODEL_CONFIG


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------

_ESTIMATED_SAVINGS_PER_TASK_USD = 0.15  # Conservative estimate vs. Sonnet invocation


def _persist_fast_path_record(
    metrics_dir: Path,
    task: Task,
    result: FastPathResult,
    estimated_savings_usd: float = _ESTIMATED_SAVINGS_PER_TASK_USD,
) -> None:
    """Append a fast-path task record to tasks.jsonl for cost reporting.

    Writes the same schema as the evolution aggregator's TaskMetrics.to_dict(),
    plus an ``estimated_savings_usd`` field that bernstein cost can display.

    Args:
        metrics_dir: `.sdd/metrics` directory.
        task: The fast-pathed task.
        result: Execution result.
        estimated_savings_usd: Estimated LLM cost avoided by fast-pathing.
    """
    record = {
        "timestamp": time.time(),
        "task_id": task.id,
        "role": task.role,
        "model": "fast-path",
        "provider": "local",
        "duration_seconds": result.duration_s,
        "tokens_prompt": 0,
        "tokens_completion": 0,
        "cost_usd": 0.0,
        "estimated_savings_usd": estimated_savings_usd,
        "janitor_passed": True,
        "files_modified": result.files_modified,
        "lines_added": 0,
        "lines_deleted": 0,
        "fast_path_action": result.action.value,
    }
    tasks_jsonl = metrics_dir / "tasks.jsonl"
    try:
        metrics_dir.mkdir(parents=True, exist_ok=True)
        with tasks_jsonl.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.warning("Could not persist fast-path record to tasks.jsonl: %s", exc)


def try_fast_path_batch(
    batch: list[Task],
    workdir: Path,
    client: httpx.Client,
    server_url: str,
    stats: FastPathStats,
) -> bool:
    """Attempt to handle a batch via fast-path. Returns True if handled.

    Only handles single-task batches classified as L0. Claims the task,
    runs the deterministic executor, and marks it complete/failed on the
    task server.

    Args:
        batch: Task batch (only single-task L0 batches are handled).
        workdir: Project root directory.
        client: httpx client for task server communication.
        server_url: Task server base URL.
        stats: Cumulative fast-path stats (mutated in place).

    Returns:
        True if the batch was handled (caller should skip spawner).
        False if the batch should proceed to normal LLM spawning.
    """
    if len(batch) != 1:
        return False

    task = batch[0]
    classification = classify_task(task)

    if classification.level != TaskLevel.L0 or classification.action is None:
        return False

    logger.info(
        "Fast-path L0 for task %s (%s): %s",
        task.id,
        classification.matched_rule,
        classification.reason,
    )

    # Execute the deterministic action
    result = execute_fast_path(classification.action, workdir, task.owned_files, task=task)

    # Record metrics
    collector = get_collector(workdir / ".sdd" / "metrics")
    collector.start_task(
        task_id=task.id,
        role=task.role,
        model="fast-path",
        provider="local",
    )

    if result.success:
        # Mark task complete on server
        try:
            resp = client.post(
                f"{server_url}/tasks/{task.id}/complete",
                json={"result_summary": f"[fast-path] {result.summary}"},
            )
            resp.raise_for_status()
        except (httpx.HTTPError, httpx.TransportError) as exc:
            logger.error("Failed to complete fast-path task %s: %s", task.id, exc)
            return False

        collector.complete_task(
            task_id=task.id,
            success=True,
            tokens_used=0,
            cost_usd=0.0,
            files_modified=result.files_modified,
        )
        stats.record(result)
        logger.info(
            "Fast-path completed task %s in %.2fs (saved ~$0.15): %s",
            task.id,
            result.duration_s,
            result.summary,
        )
    else:
        # Mark task failed — it will be retried via normal LLM path
        logger.warning(
            "Fast-path failed for task %s: %s — will retry via LLM",
            task.id,
            result.error,
        )
        try:
            client.post(
                f"{server_url}/tasks/{task.id}/fail",
                json={"reason": f"[fast-path] {result.error}"},
            )
        except (httpx.HTTPError, httpx.TransportError):
            pass
        collector.complete_task(task_id=task.id, success=False, error=result.error)

    return True
