"""Collaborative plan editing with a CRDT-inspired model layer.

Provides an operation-based editing model where multiple authors can
concurrently propose changes to a YAML plan.  Conflicts are resolved
via last-writer-wins (LWW) keyed on ``(path, timestamp)``.

Typical flow:
    1. Load a plan dict (from YAML) into ``CollaborativePlan``.
    2. Authors create ``EditOperation`` objects and call ``apply_op()``.
    3. When two branches diverge, ``merge_concurrent_ops()`` reconciles
       them using LWW semantics.
    4. ``format_edit_summary()`` produces a human-readable changelog.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, cast

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditOperation:
    """A single edit applied to a plan document.

    Attributes:
        op_type: The kind of mutation — insert, delete, or update.
        path: Dot-separated path into the plan dict (e.g. "stages.0.name").
        value: New value for insert/update; ``None`` for delete.
        author: Identifier of the agent or user who created the operation.
        timestamp: POSIX timestamp when the operation was created.
        op_id: Globally unique identifier for this operation.
    """

    op_type: Literal["insert", "delete", "update"]
    path: str
    value: Any | None
    author: str
    timestamp: float = field(default_factory=time.time)
    op_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class PlanVersion:
    """An immutable snapshot of the plan at a specific version.

    Attributes:
        version: Monotonically increasing version counter.
        ops: Operations applied since the previous version.
        snapshot: Full plan dict at this version.
    """

    version: int
    ops: list[EditOperation]
    snapshot: dict[str, Any]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _resolve_path(data: dict[str, Any], path: str) -> tuple[Any, str]:
    """Walk *data* along dot-separated *path*, returning (parent, final_key).

    Supports integer keys for list indexing (e.g. ``"stages.0.name"``).

    Raises:
        KeyError: If an intermediate segment is missing.
        IndexError: If a list index is out of range.
    """
    parts = path.split(".")
    current: Any = data
    for part in parts[:-1]:
        current = cast("Any", current[int(part)]) if isinstance(current, list) else current[part]
    final_key: str = parts[-1]
    return current, final_key


def _get_value(data: dict[str, Any], path: str) -> Any:
    """Return the value at *path* inside *data*."""
    parent, key = _resolve_path(data, path)
    if isinstance(parent, list):
        return cast("Any", parent[int(key)])
    return parent[key]  # type: ignore[index]


def _set_value(data: dict[str, Any], path: str, value: Any) -> None:
    """Set the value at *path* inside *data*."""
    parent, key = _resolve_path(data, path)
    if isinstance(parent, list):
        parent[int(key)] = value
    else:
        parent[key] = value


def _delete_value(data: dict[str, Any], path: str) -> None:
    """Remove the value at *path* inside *data*."""
    parent, key = _resolve_path(data, path)
    if isinstance(parent, list):
        del parent[int(key)]
    else:
        del parent[key]


# ---------------------------------------------------------------------------
# CollaborativePlan
# ---------------------------------------------------------------------------


class CollaborativePlan:
    """Mutable plan document that tracks versioned edit operations.

    Args:
        initial_plan: The starting plan dictionary (deep-copied internally).
    """

    def __init__(self, initial_plan: dict[str, Any]) -> None:
        self._snapshot: dict[str, Any] = copy.deepcopy(initial_plan)
        self._version: int = 0
        self._history: list[PlanVersion] = [
            PlanVersion(version=0, ops=[], snapshot=copy.deepcopy(initial_plan)),
        ]
        self._authors: set[str] = set()

    # -- mutators -----------------------------------------------------------

    def apply_op(self, op: EditOperation) -> bool:
        """Apply a single edit operation to the current snapshot.

        Returns:
            ``True`` if the operation was applied successfully,
            ``False`` if it failed (e.g. path not found for delete/update).
        """
        try:
            if op.op_type == "insert":
                _set_value(self._snapshot, op.path, op.value)
            elif op.op_type == "update":
                # Verify path exists before overwriting.
                _get_value(self._snapshot, op.path)
                _set_value(self._snapshot, op.path, op.value)
            elif op.op_type == "delete":
                _delete_value(self._snapshot, op.path)
            else:  # pragma: no cover — exhaustive literal
                return False
        except (KeyError, IndexError, TypeError):
            return False

        self._version += 1
        self._authors.add(op.author)
        self._history.append(
            PlanVersion(
                version=self._version,
                ops=[op],
                snapshot=copy.deepcopy(self._snapshot),
            ),
        )
        return True

    # -- queries ------------------------------------------------------------

    def get_snapshot(self) -> dict[str, Any]:
        """Return a deep copy of the current plan state."""
        return copy.deepcopy(self._snapshot)

    def get_version(self) -> int:
        """Return the current version number."""
        return self._version

    def get_ops_since(self, version: int) -> list[EditOperation]:
        """Return all operations applied after *version*."""
        ops: list[EditOperation] = []
        for pv in self._history:
            if pv.version > version:
                ops.extend(pv.ops)
        return ops

    def get_authors(self) -> list[str]:
        """Return a sorted list of all authors who have contributed edits."""
        return sorted(self._authors)


# ---------------------------------------------------------------------------
# Merge helper
# ---------------------------------------------------------------------------


def merge_concurrent_ops(
    ops_a: list[EditOperation],
    ops_b: list[EditOperation],
) -> list[EditOperation]:
    """Merge two concurrent operation lists using last-writer-wins.

    For each ``path``, only the operation with the latest ``timestamp`` is
    kept.  When timestamps are equal, ``ops_b`` wins (deterministic tiebreak).

    Operations targeting different paths are both preserved.

    Returns:
        A merged list of operations sorted by timestamp.
    """
    by_path: dict[str, EditOperation] = {}

    for op in ops_a:
        by_path[op.path] = op

    for op in ops_b:
        existing = by_path.get(op.path)
        if existing is None or op.timestamp >= existing.timestamp:
            by_path[op.path] = op

    return sorted(by_path.values(), key=lambda o: o.timestamp)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def format_edit_summary(ops: list[EditOperation]) -> str:
    """Produce a human-readable summary of a list of edit operations.

    Example output::

        - [update] stages.0.name = "build" (by alice @ 1712700000.0)
        - [delete] stages.1 (by bob @ 1712700001.0)
    """
    if not ops:
        return "No edits."

    lines: list[str] = []
    for op in ops:
        if op.op_type == "delete":
            lines.append(f"- [{op.op_type}] {op.path} (by {op.author} @ {op.timestamp})")
        else:
            lines.append(f"- [{op.op_type}] {op.path} = {op.value!r} (by {op.author} @ {op.timestamp})")
    return "\n".join(lines)
