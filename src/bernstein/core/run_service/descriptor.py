"""The run descriptor: the immutable submission record for a detached run (#2352).

A goal is decomposed into an explicit task list by the planner (out of scope
here); the descriptor is the small, portable record the supervisor reads to
know what work the run comprises. The goal text is hashed so the run.open
ledger entry can bind the submission without carrying the (possibly sensitive)
prompt into the portable chain.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class RunDescriptorError(ValueError):
    """Raised when a descriptor file is missing or malformed."""


def goal_digest(goal: str) -> str:
    """Return the lower-case hex SHA-256 of *goal* (utf-8)."""
    return hashlib.sha256(goal.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunDescriptor:
    """Immutable submission record for one detached run."""

    run_id: str
    goal: str
    task_ids: list[str] = field(default_factory=list)
    created_ts: float = 0.0

    @property
    def goal_sha256(self) -> str:
        """Hex SHA-256 of the goal text."""
        return goal_digest(self.goal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "goal_sha256": self.goal_sha256,
            "task_ids": self.task_ids.copy(),
            "created_ts": self.created_ts,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RunDescriptor:
        task_ids = raw.get("task_ids")
        return cls(
            run_id=str(raw["run_id"]),
            goal=str(raw.get("goal", "")),
            task_ids=[str(t) for t in task_ids] if isinstance(task_ids, list) else [],
            created_ts=float(raw.get("created_ts", 0.0)),
        )

    def write(self, path: Path) -> None:
        """Atomically persist the descriptor to *path*."""
        from bernstein.core.persistence.atomic_write import write_atomic_json

        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic_json(path, self.to_dict(), indent=2)

    @classmethod
    def read(cls, path: Path) -> RunDescriptor:
        """Load a descriptor from *path*.

        Raises:
            RunDescriptorError: When the file is absent or not valid JSON.
        """
        if not path.exists():
            msg = f"no run descriptor at {path}"
            raise RunDescriptorError(msg)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"run descriptor {path} is unreadable: {exc}"
            raise RunDescriptorError(msg) from exc
        if not isinstance(raw, dict):
            msg = f"run descriptor {path} is not a JSON object"
            raise RunDescriptorError(msg)
        return cls.from_dict(raw)


__all__ = [
    "RunDescriptor",
    "RunDescriptorError",
    "goal_digest",
]
