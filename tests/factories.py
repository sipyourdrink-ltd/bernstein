"""Test data generators for realistic task payloads.

Factory functions that create valid ``Task``, ``AgentSession`` and related
objects with sensible defaults.  Use these in tests to avoid boilerplate
``Task(id=..., title=..., description=..., role=...)`` constructors
scattered across the test suite.

See GitHub issue #513.
"""

from __future__ import annotations

import time
from uuid import uuid4

from bernstein.core.tasks.models import (
    AgentSession,
    Complexity,
    ModelConfig,
    Scope,
    Task,
    TaskStatus,
)

# ---------------------------------------------------------------------------
# Roles used when generating batches with varied roles.
# ---------------------------------------------------------------------------
_DEFAULT_ROLES: list[str] = [
    "backend",
    "frontend",
    "qa",
    "devops",
    "security",
]

# Titles keyed by role — used by ``make_task_batch`` for realism.
_ROLE_TITLES: dict[str, list[str]] = {
    "backend": [
        "Add pagination to /tasks endpoint",
        "Implement retry logic for task store",
        "Optimise SQL query for task listing",
    ],
    "frontend": [
        "Build task-detail overlay component",
        "Add dark-mode support to TUI",
        "Wire up progress bar to heartbeat stream",
    ],
    "qa": [
        "Write integration tests for approval flow",
        "Add property-based tests for task splitter",
        "Verify guardrail checks block secrets",
    ],
    "devops": [
        "Set up CI pipeline for protocol tests",
        "Add container health-check probe",
        "Automate release tagging workflow",
    ],
    "security": [
        "Audit token handling in agent adapters",
        "Add rate-limit enforcement to task API",
        "Review RBAC rules for multi-tenant mode",
    ],
}

_SCOPE_VALUES: list[Scope] = [Scope.SMALL, Scope.MEDIUM, Scope.LARGE]
_COMPLEXITY_VALUES: list[Complexity] = [Complexity.LOW, Complexity.MEDIUM, Complexity.HIGH]


# ---------------------------------------------------------------------------
# Factory: single Task
# ---------------------------------------------------------------------------


def make_task(
    title: str = "Implement feature X",
    status: TaskStatus = TaskStatus.OPEN,
    role: str = "backend",
    priority: int = 2,
    scope: str = "medium",
    complexity: str = "medium",
    owned_files: list[str] | None = None,
    depends_on: list[str] | None = None,
    **overrides: object,
) -> Task:
    """Create a realistic ``Task`` with sensible defaults.

    Args:
        title: Human-readable task title.
        status: Initial task status.
        role: Specialist role (e.g. ``"backend"``, ``"qa"``).
        priority: 1=critical, 2=normal, 3=nice-to-have.
        scope: ``"small"``, ``"medium"``, or ``"large"``.
        complexity: ``"low"``, ``"medium"``, or ``"high"``.
        owned_files: File paths this task may modify.
        depends_on: IDs of tasks that must complete first.
        **overrides: Any extra ``Task`` field to override.

    Returns:
        A fully initialised ``Task`` instance.
    """
    task_id: str = str(overrides.pop("id", None) or uuid4().hex[:12])
    now = time.time()

    return Task(
        id=task_id,
        title=title,
        description=overrides.pop("description", f"Auto-generated task: {title}"),  # type: ignore[arg-type]
        role=role,
        priority=priority,
        scope=Scope(scope),
        complexity=Complexity(complexity),
        status=status,
        owned_files=owned_files or [],
        depends_on=depends_on or [],
        created_at=overrides.pop("created_at", now),  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Factory: single AgentSession
# ---------------------------------------------------------------------------


def make_session(
    task_ids: list[str] | None = None,
    role: str = "backend",
    model: str = "sonnet",
    **overrides: object,
) -> AgentSession:
    """Create a realistic ``AgentSession`` with sensible defaults.

    Args:
        task_ids: IDs of tasks assigned to this session.
        role: Specialist role for the agent.
        model: Model name passed to ``ModelConfig``.
        **overrides: Any extra ``AgentSession`` field to override.

    Returns:
        A fully initialised ``AgentSession`` instance.
    """
    session_id: str = str(overrides.pop("id", None) or uuid4().hex[:12])

    model_config = overrides.pop(
        "model_config",
        ModelConfig(model=model, effort="high"),
    )

    return AgentSession(
        id=session_id,
        role=role,
        task_ids=task_ids or [],
        model_config=model_config,  # type: ignore[arg-type]
        spawn_ts=overrides.pop("spawn_ts", time.time()),  # type: ignore[arg-type]
        status=overrides.pop("status", "working"),  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Factory: batch of related Tasks
# ---------------------------------------------------------------------------


def make_task_batch(
    count: int = 5,
    roles: list[str] | None = None,
) -> list[Task]:
    """Create a batch of related tasks with realistic titles and dependencies.

    The first task in the batch has no dependencies.  Subsequent tasks
    depend on the immediately preceding task, forming a simple chain.

    Args:
        count: Number of tasks to generate (minimum 1).
        roles: Roles to cycle through.  Defaults to
            ``["backend", "frontend", "qa", "devops", "security"]``.

    Returns:
        A list of ``Task`` objects with chained ``depends_on`` relationships.
    """
    if count < 1:
        msg = "count must be >= 1"
        raise ValueError(msg)

    effective_roles = roles or list(_DEFAULT_ROLES)
    tasks: list[Task] = []

    for i in range(count):
        role = effective_roles[i % len(effective_roles)]
        titles_for_role = _ROLE_TITLES.get(role, [f"Task for {role}"])
        title = titles_for_role[i % len(titles_for_role)]

        depends: list[str] = []
        if tasks:
            depends = [tasks[-1].id]

        scope = _SCOPE_VALUES[i % len(_SCOPE_VALUES)]
        complexity = _COMPLEXITY_VALUES[i % len(_COMPLEXITY_VALUES)]

        task = make_task(
            title=title,
            role=role,
            scope=scope.value,
            complexity=complexity.value,
            depends_on=depends,
            priority=(i % 3) + 1,
        )
        tasks.append(task)

    return tasks


# ---------------------------------------------------------------------------
# Factory: completion data dict
# ---------------------------------------------------------------------------


def make_completion_data(
    files_changed: int = 3,
    tests_passing: bool = True,
) -> dict[str, object]:
    """Create realistic completion signal data for a POST /tasks/{id}/complete.

    Args:
        files_changed: Number of files the agent modified.
        tests_passing: Whether all tests pass after the agent's changes.

    Returns:
        A dict suitable for serialising as JSON in a completion request.
    """
    return {
        "files_changed": files_changed,
        "tests_passing": tests_passing,
        "errors": 0 if tests_passing else 1,
        "result_summary": (
            f"Changed {files_changed} file(s), all tests passing."
            if tests_passing
            else f"Changed {files_changed} file(s), 1 test failure."
        ),
        "timestamp": time.time(),
    }
