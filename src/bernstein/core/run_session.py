"""Deterministic run session recording and replay.

Every ``bernstein run`` that produces a task plan records a *session* to
``.sdd/runtime/sessions/<session_id>.json``.  The session captures:

- The goal string used to drive planning.
- An integer random seed that was in effect during the run.
- The resolved task list (title, description, role, priority, …).
- Routing decisions and model selection metadata.
- The git HEAD SHA at run time.

On ``bernstein replay <session_id>``, the session is loaded from disk,
the same tasks are injected directly into the bootstrap (bypassing LLM
planning), and the same random seed is re-applied.  This guarantees that
two replay runs produce the same task decomposition as the original run,
regardless of what the LLM would return today.

Usage::

    # Record automatically — pass session_id to bootstrap helpers
    session = RunSession.create(goal="Build X", run_seed=42)
    session.record_tasks(tasks)
    session.save(workdir / ".sdd" / "runtime" / "sessions")

    # Replay
    session = RunSession.load(sessions_dir, "20240101-120000-abc123")
    tasks = session.to_tasks()
    session.apply_seed()
    bootstrap_from_goal(goal=session.goal, tasks=tasks, ...)
"""

from __future__ import annotations

import json
import logging
import random
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_SESSION_FILE_SUFFIX = ".json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_head_sha() -> str:
    """Return current HEAD commit SHA, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _generate_session_id() -> str:
    """Generate a unique session ID based on timestamp + random hex suffix."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"{random.getrandbits(24):06x}"
    return f"{ts}-{suffix}"


# ---------------------------------------------------------------------------
# RunSession dataclass
# ---------------------------------------------------------------------------


@dataclass
class RunSession:
    """Immutable record of a single Bernstein orchestration run.

    Attributes:
        session_id: Unique identifier for this run (e.g. ``20240101-120000-abc123``).
        goal: The goal string used to drive task planning.
        run_seed: Integer random seed applied before planning.
        tasks: Serialised task objects (list of dicts matching Task fields).
        routing_decisions: Optional mapping of task_id → model chosen.
        git_sha: HEAD commit SHA at run time (empty string if unavailable).
        created_at: ISO-8601 timestamp string when the session was created.
        bernstein_version: Package version at run time (empty if unavailable).
    """

    session_id: str
    goal: str
    run_seed: int
    tasks: list[dict[str, Any]] = field(default_factory=list)
    routing_decisions: dict[str, str] = field(default_factory=dict)
    git_sha: str = ""
    created_at: str = ""
    bernstein_version: str = ""

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, goal: str, run_seed: int | None = None) -> RunSession:
        """Create a new session with auto-generated ID and metadata.

        Args:
            goal: Project goal string.
            run_seed: Optional deterministic seed.  A random one is chosen
                if not provided.

        Returns:
            A new :class:`RunSession` with fields populated.
        """
        seed = run_seed if run_seed is not None else random.randint(0, 2**31 - 1)
        version = _get_bernstein_version()
        return cls(
            session_id=_generate_session_id(),
            goal=goal,
            run_seed=seed,
            git_sha=_git_head_sha(),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            bernstein_version=version,
        )

    # ------------------------------------------------------------------
    # Task recording
    # ------------------------------------------------------------------

    def record_tasks(self, tasks: list[Any]) -> None:
        """Serialize and store the task list for replay.

        Accepts either :class:`bernstein.core.models.Task` dataclass instances
        or plain dicts.

        Args:
            tasks: Task objects or dicts produced by the planning step.
        """
        from dataclasses import asdict, fields

        serialised: list[dict[str, Any]] = []
        for task in tasks:
            if isinstance(task, dict):
                serialised.append(task)
            else:
                # Dataclass — convert enums to their values
                try:
                    raw = asdict(task)
                except TypeError:
                    # Fallback: iterate fields manually
                    raw = {f.name: getattr(task, f.name) for f in fields(task)}
                # Coerce enum values to their .value
                coerced: dict[str, Any] = {}
                for k, v in raw.items():
                    if hasattr(v, "value"):
                        coerced[k] = v.value
                    else:
                        coerced[k] = v
                serialised.append(coerced)
        self.tasks = serialised

    def record_routing(self, task_id: str, model: str) -> None:
        """Record a routing decision for a single task.

        Args:
            task_id: Server-assigned task identifier.
            model: Model name selected for this task.
        """
        self.routing_decisions[task_id] = model

    # ------------------------------------------------------------------
    # Determinism helpers
    # ------------------------------------------------------------------

    def apply_seed(self) -> None:
        """Seed Python's ``random`` module with this session's run_seed.

        Call this before replaying so that any stochastic routing logic
        produces the same sequence of decisions as the original run.
        """
        random.seed(self.run_seed)
        logger.debug("Applied run seed %d for session %s", self.run_seed, self.session_id)

    def to_tasks(self) -> list[Any]:
        """Deserialise recorded tasks back into :class:`bernstein.core.models.Task` objects.

        Returns:
            List of :class:`~bernstein.core.models.Task` dataclass instances
            ready to pass to :func:`~bernstein.core.bootstrap.bootstrap_from_goal`.
        """
        from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType

        _status_map = {s.value: s for s in TaskStatus}
        _scope_map = {s.value: s for s in Scope}
        _complexity_map = {c.value: c for c in Complexity}
        _type_map = {t.value: t for t in TaskType}

        result: list[Task] = []
        for d in self.tasks:
            result.append(
                Task(
                    id=d.get("id", ""),
                    title=d.get("title", ""),
                    description=d.get("description", ""),
                    role=d.get("role", "backend"),
                    priority=int(d.get("priority", 5)),
                    scope=_scope_map.get(d.get("scope", "medium"), Scope.MEDIUM),
                    complexity=_complexity_map.get(d.get("complexity", "medium"), Complexity.MEDIUM),
                    status=_status_map.get(d.get("status", "open"), TaskStatus.OPEN),
                    estimated_minutes=d.get("estimated_minutes"),
                    depends_on=d.get("depends_on") or [],
                    owned_files=d.get("owned_files") or [],
                    task_type=_type_map.get(d.get("task_type", "feature"), TaskType.FEATURE),
                )
            )
        return result  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, sessions_dir: Path) -> Path:
        """Write the session to *sessions_dir*/<session_id>.json.

        Args:
            sessions_dir: Directory to write the session file into.
                Created automatically if it does not exist.

        Returns:
            Path to the written session file.
        """
        sessions_dir.mkdir(parents=True, exist_ok=True)
        path = sessions_dir / f"{self.session_id}{_SESSION_FILE_SUFFIX}"
        data = {
            "session_id": self.session_id,
            "goal": self.goal,
            "run_seed": self.run_seed,
            "tasks": self.tasks,
            "routing_decisions": self.routing_decisions,
            "git_sha": self.git_sha,
            "created_at": self.created_at,
            "bernstein_version": self.bernstein_version,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info("Session saved: %s", path)
        return path

    @classmethod
    def load(cls, sessions_dir: Path, session_id: str) -> RunSession:
        """Load a session from *sessions_dir*/<session_id>.json.

        Args:
            sessions_dir: Directory containing session files.
            session_id: Session identifier (filename without .json suffix).

        Returns:
            Loaded :class:`RunSession`.

        Raises:
            FileNotFoundError: If the session file does not exist.
            ValueError: If the session JSON is malformed.
        """
        path = sessions_dir / f"{session_id}{_SESSION_FILE_SUFFIX}"
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed session file {path}: {exc}") from exc
        return cls(
            session_id=data.get("session_id", session_id),
            goal=data.get("goal", ""),
            run_seed=int(data.get("run_seed", 0)),
            tasks=data.get("tasks", []),
            routing_decisions=data.get("routing_decisions", {}),
            git_sha=data.get("git_sha", ""),
            created_at=data.get("created_at", ""),
            bernstein_version=data.get("bernstein_version", ""),
        )

    @classmethod
    def list_sessions(cls, sessions_dir: Path) -> list[str]:
        """Return all session IDs stored in *sessions_dir*, newest first.

        Args:
            sessions_dir: Directory containing session JSON files.

        Returns:
            List of session ID strings, sorted by file modification time
            descending.
        """
        if not sessions_dir.is_dir():
            return []
        files = sorted(
            sessions_dir.glob(f"*{_SESSION_FILE_SUFFIX}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return [f.stem for f in files]


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def sessions_dir_for(workdir: Path) -> Path:
    """Return the default sessions directory for a project workdir.

    Args:
        workdir: Project root directory.

    Returns:
        ``.sdd/runtime/sessions/`` path.
    """
    return workdir / ".sdd" / "runtime" / "sessions"


def _get_bernstein_version() -> str:
    """Return the installed bernstein package version, or empty string."""
    try:
        from importlib.metadata import version

        return version("bernstein")
    except Exception:
        return ""
