"""Realistic test data generators for Bernstein task payloads.

This module provides configurable generators for test tasks, plans,
and completion signals that mirror the structure of real agent
work in the Bernstein orchestrator.

Used by the test suite to generate realistic task graphs without
hand-coding dozens of fixture objects.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import UTC
from typing import Any

__all__ = [
    "GeneratedTask",
    "TaskTemplate",
    "generate_completion_signal",
    "generate_plan",
    "generate_task",
    "generate_task_batch",
]


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_ROLES = ["coder", "reviewer", "tester", "devops", "architect"]

_COMPLEXITY_FILE_COUNTS: dict[str, tuple[int, int]] = {
    "low": (1, 3),
    "medium": (2, 6),
    "high": (5, 15),
}

_COMPLEXITY_DEPENDENCY_COUNTS: dict[str, tuple[int, int]] = {
    "low": (0, 0),
    "medium": (0, 2),
    "high": (2, 4),
}

_COMPLEXITY_GATE_COUNTS: dict[str, tuple[int, int]] = {
    "low": (1, 2),
    "medium": (2, 3),
    "high": (3, 5),
}

_COMPLEXITY_PRIORITIES: dict[str, tuple[int, int]] = {
    "low": (7, 10),
    "medium": (4, 6),
    "high": (1, 3),
}

_QUALITY_GATES_ALL = [
    "tests_pass",
    "lint_clean",
    "type_check",
    "coverage_above_80",
    "security_scan",
    "doc_updated",
]

_TASK_PREFIXES = [
    "Fix",
    "Add",
    "Refactor",
    "Optimize",
    "Implement",
    "Remove",
    "Update",
    "Migrate",
    "Split",
    "Merge",
]

_TASK_NOUNS = [
    "auth",
    "cache",
    "api",
    "ui",
    "database",
    "logger",
    "config",
    "tests",
    "docs",
    "migration",
    "errors",
    "metrics",
    "pagination",
    "rate_limiting",
    "health_checks",
]

_TASK_MODIFIERS = [
    "middleware",
    "handler",
    "service",
    "client",
    "integration",
    "endpoint",
    "pipeline",
    "store",
    "gateway",
]


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TaskTemplate:
    """Template for generating realistic test tasks.

    Attributes:
        role: The primary agent role responsible for this task
            (e.g., "coder", "reviewer", "tester").
        complexity: One of "low", "medium", or "high". Controls file
            counts, dependency depth, and priority ranges.
        file_count_range: Inclusive range of files this task touches.
        has_dependencies: Whether this task depends on sibling tasks.
        quality_gates: Collection of quality gates the task must pass.
    """

    role: str
    complexity: str  # "low" | "medium" | "high"
    file_count_range: tuple[int, int]
    has_dependencies: bool
    quality_gates: tuple[str, ...]


@dataclass(frozen=True)
class GeneratedTask:
    """A generated realistic test task.

    Attributes:
        task_id: Unique identifier for this task (8-char hex).
        title: Human-readable task title (e.g., "Fix auth middleware").
        goal: Detailed description of what the task accomplishes.
        role: The agent role responsible for execution.
        scope: List of file paths the task touches.
        dependencies: Task IDs this task depends on.
        quality_gates: Quality gates that must pass for completion.
        priority: Integer priority (1 = highest).
        complexity: Complexity tier ("low", "medium", "high").
    """

    task_id: str
    title: str
    goal: str
    role: str
    scope: list[str]
    dependencies: list[str]
    quality_gates: list[str]
    priority: int
    complexity: str


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _generate_scope(template: TaskTemplate) -> list[str]:
    """Generate a realistic file-scope list for a task."""
    count = random.randint(*template.file_count_range)
    modules = random.sample(_TASK_NOUNS, k=min(count, len(_TASK_NOUNS)))
    scope = []
    for mod in modules:
        scope.append(f"src/bernstein/core/{mod}.py")
        if random.random() > 0.5:
            scope.append(f"tests/unit/test_{mod}.py")
    return scope


def _generate_title() -> str:
    """Generate a plausible task title."""
    prefix = random.choice(_TASK_PREFIXES)
    noun = random.choice(_TASK_NOUNS)
    modifier = random.choice(_TASK_MODIFIERS)
    # Avoid awkward repetitions
    if noun in modifier:
        modifier = random.choice(_TASK_MODIFIERS)
    return f"{prefix} {noun} {modifier}"


def _generate_goal(title: str, scope: list[str]) -> str:
    """Generate a task goal from a title and scope."""
    primary_file = scope[0] if scope else "the relevant module"
    return (
        f"Complete the following task: {title}. "
        f"Modify {primary_file} and related files. "
        f"Ensure all quality gates pass before marking complete."
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def generate_task(template: TaskTemplate | None = None) -> GeneratedTask:
    """Generate a single realistic test task.

    Args:
        template: Optional template controlling complexity, role, and
            gate configuration. If omitted, a random template is used.

    Returns:
        A fully-populated GeneratedTask instance.
    """
    if template is None:
        complexity = random.choice(["low", "medium", "high"])
        role = random.choice(_ROLES)
        file_counts = _COMPLEXITY_FILE_COUNTS[complexity]
        _COMPLEXITY_DEPENDENCY_COUNTS[complexity]
        gate_counts = _COMPLEXITY_GATE_COUNTS[complexity]
        _COMPLEXITY_PRIORITIES[complexity]

        num_gates = random.randint(*gate_counts)
        gates = sorted(random.sample(_QUALITY_GATES_ALL, k=num_gates))

        template = TaskTemplate(
            role=role,
            complexity=complexity,
            file_count_range=file_counts,
            has_dependencies=random.choice([True, False]),
            quality_gates=tuple(gates),
        )

    title = _generate_title()
    scope = _generate_scope(template)
    goal = _generate_goal(title, scope)

    random.randint(*_COMPLEXITY_DEPENDENCY_COUNTS[template.complexity])

    return GeneratedTask(
        task_id=uuid.uuid4().hex[:8],
        title=title,
        goal=goal,
        role=template.role,
        scope=scope,
        dependencies=[],  # populated by caller or generate_plan
        quality_gates=list(template.quality_gates),
        priority=random.randint(*_COMPLEXITY_PRIORITIES[template.complexity]),
        complexity=template.complexity,
    )


def generate_task_batch(
    count: int,
    roles: list[str] | None = None,
    complexity: str | None = None,
) -> list[GeneratedTask]:
    """Generate a batch of realistic test tasks.

    Args:
        count: Number of tasks to generate.
        roles: Optional list of roles to restrict task generation to.
            If None, roles are chosen randomly.
        complexity: Optional fixed complexity for all tasks.
            If None, complexity is chosen randomly per task.

    Returns:
        A list of `count` unique GeneratedTask instances.
    """
    tasks: list[GeneratedTask] = []
    seen_ids: set[str] = set()

    for _ in range(count):
        role = random.choice(roles) if roles else random.choice(_ROLES)
        cmp = complexity or random.choice(["low", "medium", "high"])
        file_counts = _COMPLEXITY_FILE_COUNTS[cmp]
        gate_counts = _COMPLEXITY_GATE_COUNTS[cmp]
        _COMPLEXITY_PRIORITIES[cmp]

        num_gates = random.randint(*gate_counts)
        gates = sorted(random.sample(_QUALITY_GATES_ALL, k=num_gates))

        template = TaskTemplate(
            role=role,
            complexity=cmp,
            file_count_range=file_counts,
            has_dependencies=random.random() > 0.4,
            quality_gates=tuple(gates),
        )

        task = generate_task(template)
        # Ensure uniqueness
        while task.task_id in seen_ids:
            task = generate_task(template)
        seen_ids.add(task.task_id)
        tasks.append(task)

    return tasks


def generate_plan(
    stages: int = 3,
    tasks_per_stage: int = 3,
) -> dict[str, Any]:
    """Generate a realistic multi-stage test plan with inter-stage dependencies.

    Each stage's tasks depend on tasks from the previous stage, forming
    a realistic dependency chain that mirrors staged refactoring or
    feature development.

    Args:
        stages: Number of plan stages to generate (default 3).
        tasks_per_stage: Number of tasks per stage (default 3).

    Returns:
        A dict with keys `"stages"` (list of stage dicts) and
        `"tasks"` (flat list of all GeneratedTask instances).
        Each stage dict contains `stage_id`, `tasks`, and
        `depends_on` (task IDs from the previous stage).
    """
    all_tasks: list[GeneratedTask] = []
    stage_outputs: list[list[str]] = []  # task_ids per stage

    for _stage_idx in range(stages):
        stage_tasks = generate_task_batch(tasks_per_stage)
        prev_ids = stage_outputs[-1] if stage_outputs else []

        # Wire inter-stage dependencies
        wired_tasks: list[GeneratedTask] = []
        for task in stage_tasks:
            num_deps = min(len(prev_ids), random.randint(0, 2)) if prev_ids else 0
            deps = random.sample(prev_ids, k=num_deps) if num_deps else []
            wired_tasks.append(
                GeneratedTask(
                    task_id=task.task_id,
                    title=task.title,
                    goal=task.goal,
                    role=task.role,
                    scope=task.scope,
                    dependencies=deps,
                    quality_gates=task.quality_gates,
                    priority=task.priority,
                    complexity=task.complexity,
                )
            )

        all_tasks.extend(wired_tasks)
        stage_outputs.append([t.task_id for t in wired_tasks])

    # Build stage structure
    stage_objs: list[dict[str, Any]] = []
    for i, task_group in enumerate(stage_outputs):
        stage_objs.append(
            {
                "stage_id": i + 1,
                "task_ids": task_group,
                "depends_on": stage_outputs[i - 1] if i > 0 else [],
            }
        )

    return {
        "stages": stage_objs,
        "tasks": [
            {
                "task_id": t.task_id,
                "title": t.title,
                "role": t.role,
                "complexity": t.complexity,
                "priority": t.priority,
                "dependencies": t.dependencies,
                "scope": t.scope,
                "quality_gates": t.quality_gates,
                "goal": t.goal,
            }
            for t in all_tasks
        ],
    }


def generate_completion_signal(task: GeneratedTask) -> dict[str, Any]:
    """Generate a realistic task completion signal.

    Simulates the payload that the Bernstein agent runtime emits when
    a task is marked complete.

    Args:
        task: The GeneratedTask that was completed.

    Returns:
        A dict containing `task_id`, `files_changed`,
        `tests_pass`, `quality_gates_pass`, `duration_seconds`,
        and `completed_at`.
    """
    from datetime import datetime

    gate_results = {
        gate: random.random() > 0.1  # ~90% pass rate per gate
        for gate in task.quality_gates
    }

    # Bias tests_pass based on overall gate health
    all_pass = all(gate_results.values())
    tests_pass = all_pass or random.random() > 0.3

    # Derive files_changed from scope length
    files_changed = len(task.scope)

    return {
        "task_id": task.task_id,
        "files_changed": files_changed,
        "tests_pass": tests_pass,
        "quality_gates_pass": gate_results,
        "duration_seconds": random.randint(60, 7200),
        "completed_at": datetime.now(UTC).isoformat(),
    }
