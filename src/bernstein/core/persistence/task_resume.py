"""Per-task resume checkpoints - pick up paused/killed/crashed tasks.

This module is the storage layer for ``bernstein resume <task-id>``. It is
deliberately narrow: a single JSON file per task captured after every
successful step transition, recording everything we need to rebuild the run
context without re-walking the trace from scratch.

Layout::

    .sdd/runtime/checkpoints/<task-id>/checkpoint.json

Schema (versioned via :data:`SCHEMA_VERSION`):

* ``task_id`` - identifies the task this checkpoint belongs to.
* ``last_completed_step_id`` - last step the orchestrator confirmed done.
* ``trace_cursor`` - byte offset into ``.sdd/traces/<task-id>.jsonl``
  marking the last replayed event.
* ``scratchpad_path`` / ``scratchpad_sha256`` - pointer + content hash for
  the recovered scratchpad. The hash is recorded so a downstream resume
  can detect post-checkpoint scratchpad tampering.
* ``adapter`` / ``adapter_session_id`` - adapter name and the session
  identifier the adapter handed us when the task was first spawned.
* ``worktree_path`` - absolute path to the preserved worktree (``v1``
  scope is local-only, so this is intentionally a literal path).
* ``resume_count`` - how many times this task has been resumed; the CLI
  bumps it before re-spawning so flaky tasks are visible in the dashboard.
* ``stall_reason`` - *optional*. Set when the checkpoint was written at an
  automatic stall-kill boundary (heartbeat staleness or identical-progress
  detection) rather than after a normal step completion; ``None`` for the
  latter. Value is a :class:`~bernstein.core.orchestration.supervisor_receipt.StallReason`
  string.
* ``merge_cursor`` - *optional* streaming-merge cursor handed in by
  :mod:`bernstein.core.streaming_merge`. Coordination with the merge
  pipeline is a follow-up; we capture the shape now so adopters do not
  have to migrate later.
* ``meta`` - adapter-opaque key/value bag (model, role, etc.). Adapters
  with a ``resume()`` capability can stash anything they need here.
* ``created_at`` / ``updated_at`` - UTC ISO-8601 timestamps.

The on-disk write is atomic: we write to ``checkpoint.json.tmp`` then
``os.replace`` so a kill during persist cannot leave a partial file.

Out of scope (per spec):
    * Cross-machine resume (paths are local).
    * Distributed checkpoint storage (no S3 / object-store backend).
    * Resume across role-definition changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

__all__ = [
    "CHECKPOINTS_SUBDIR",
    "CHECKPOINT_FILENAME",
    "SCHEMA_VERSION",
    "CheckpointCorruptError",
    "CheckpointMissingError",
    "TaskResumeCheckpoint",
    "bump_resume_count",
    "checkpoint_dir_for",
    "checkpoint_path_for",
    "load_checkpoint",
    "save_checkpoint",
    "scratchpad_sha256",
]


SCHEMA_VERSION: int = 1
"""On-disk schema version. Bump on any backwards-incompatible change."""

CHECKPOINTS_SUBDIR: str = ".sdd/runtime/checkpoints"
"""Sub-path under the workdir where per-task checkpoint folders live."""

CHECKPOINT_FILENAME: str = "checkpoint.json"
"""File name written inside each task's checkpoint directory."""


class CheckpointMissingError(FileNotFoundError):
    """Raised when ``bernstein resume`` finds no checkpoint for the task."""


class CheckpointCorruptError(ValueError):
    """Raised when a checkpoint file exists but cannot be parsed."""


class TaskResumeCheckpoint(BaseModel):
    """Pydantic model for the per-task resume checkpoint.

    Strict by design: unknown fields are rejected so a drift between
    writer and reader fails fast instead of silently dropping data.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    task_id: str = Field(..., min_length=1)
    last_completed_step_id: str = ""
    trace_cursor: int = Field(default=0, ge=0)
    scratchpad_path: str | None = None
    scratchpad_sha256: str | None = None
    adapter: str = ""
    adapter_session_id: str = ""
    worktree_path: str | None = None
    resume_count: int = Field(default=0, ge=0)
    stall_reason: str | None = None
    merge_cursor: dict[str, Any] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def touch(self) -> None:
        """Stamp ``updated_at`` with the current UTC time."""
        self.updated_at = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def checkpoint_dir_for(workdir: Path, task_id: str) -> Path:
    """Return the directory where ``task_id``'s checkpoint should live."""
    return workdir / CHECKPOINTS_SUBDIR / task_id


def checkpoint_path_for(workdir: Path, task_id: str) -> Path:
    """Return the absolute path of ``task_id``'s checkpoint JSON file."""
    return checkpoint_dir_for(workdir, task_id) / CHECKPOINT_FILENAME


# ---------------------------------------------------------------------------
# Persist / load
# ---------------------------------------------------------------------------


def save_checkpoint(workdir: Path, checkpoint: TaskResumeCheckpoint) -> Path:
    """Atomically persist ``checkpoint`` for the task it identifies.

    Args:
        workdir: Project root (resolves to ``<workdir>/.sdd/runtime/...``).
        checkpoint: Populated :class:`TaskResumeCheckpoint`.

    Returns:
        Path to the written checkpoint file.
    """
    checkpoint.touch()
    target_dir = checkpoint_dir_for(workdir, checkpoint.task_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / CHECKPOINT_FILENAME

    payload = checkpoint.model_dump(mode="json")
    data = json.dumps(payload, indent=2, sort_keys=True)

    # Write to a sibling temp file then atomic replace. We use the same
    # directory so the rename stays on a single filesystem.
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=".checkpoint-",
        suffix=".tmp",
        dir=str(target_dir),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup of the temp file; never mask the original
        # exception with a cleanup error.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover -- defensive
            logger.debug("Failed to remove temp checkpoint at %s", tmp_path, exc_info=True)
        raise
    logger.debug("Saved resume checkpoint for task=%s to %s", checkpoint.task_id, target)
    return target


def load_checkpoint(workdir: Path, task_id: str) -> TaskResumeCheckpoint:
    """Load the resume checkpoint for ``task_id``.

    Args:
        workdir: Project root.
        task_id: Task identifier.

    Returns:
        Populated :class:`TaskResumeCheckpoint`.

    Raises:
        CheckpointMissingError: No checkpoint file on disk.
        CheckpointCorruptError: File exists but is unreadable or invalid.
    """
    path = checkpoint_path_for(workdir, task_id)
    if not path.is_file():
        raise CheckpointMissingError(
            f"No resume checkpoint for task {task_id!r} at {path}. "
            "Either the task has not produced a checkpoint yet or the "
            "directory was cleaned up. Run the task fresh with "
            f"`bernstein run` instead."
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckpointCorruptError(f"Checkpoint for task {task_id!r} is unreadable at {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckpointCorruptError(f"Checkpoint for task {task_id!r} at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CheckpointCorruptError(f"Checkpoint for task {task_id!r} at {path} is not a JSON object.")
    try:
        return TaskResumeCheckpoint.model_validate(data)
    except ValidationError as exc:
        raise CheckpointCorruptError(
            f"Checkpoint for task {task_id!r} at {path} failed schema validation: {exc}"
        ) from exc


def bump_resume_count(workdir: Path, task_id: str) -> TaskResumeCheckpoint:
    """Increment ``resume_count`` on disk and return the updated record.

    Used by ``bernstein resume`` immediately before re-spawning so the
    dashboard can flag flaky tasks even if the resume itself crashes.

    Raises:
        CheckpointMissingError / CheckpointCorruptError: Same contract as
        :func:`load_checkpoint`.
    """
    checkpoint = load_checkpoint(workdir, task_id)
    checkpoint.resume_count += 1
    save_checkpoint(workdir, checkpoint)
    return checkpoint


# ---------------------------------------------------------------------------
# Scratchpad helpers
# ---------------------------------------------------------------------------


def scratchpad_sha256(scratchpad_path: Path | None) -> str | None:
    """Return the SHA-256 of the scratchpad file, or ``None`` if absent.

    The hash is captured on every save so a downstream resume can detect
    post-checkpoint scratchpad tampering.
    """
    if scratchpad_path is None:
        return None
    try:
        data = scratchpad_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("scratchpad_sha256: read failed for %s: %s", scratchpad_path, exc)
        return None
    return hashlib.sha256(data).hexdigest()
