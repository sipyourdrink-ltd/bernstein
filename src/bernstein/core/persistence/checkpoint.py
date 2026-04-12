"""Orchestrator checkpoint/restore for long-running plans.

Provides durable checkpointing so that 50+ task plans spanning hours
can be resumed after reboots or crashes. Checkpoints capture the full
orchestrator state — task graph, agent sessions, cost accumulator, and
WAL position — as a single atomic JSON file in ``.sdd/``.

Storage: .sdd/runtime/checkpoints/checkpoint-{id}.json
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckpointMetadata:
    """Immutable metadata describing a single checkpoint.

    Attributes:
        checkpoint_id: Unique identifier for this checkpoint.
        created_at: ISO-8601 timestamp of checkpoint creation.
        bernstein_version: Version of Bernstein that created this checkpoint.
        task_count: Total number of tasks in the plan.
        completed_count: Number of tasks completed at checkpoint time.
        failed_count: Number of tasks that had failed at checkpoint time.
        cost_usd: Cumulative cost in USD at checkpoint time.
        plan_file: Path to the plan YAML file, if applicable.
    """

    checkpoint_id: str
    created_at: str
    bernstein_version: str
    task_count: int
    completed_count: int
    failed_count: int
    cost_usd: float
    plan_file: str | None


@dataclass(frozen=True)
class Checkpoint:
    """Immutable snapshot of full orchestrator state.

    Attributes:
        metadata: Checkpoint identification and summary statistics.
        task_graph: Serialized task dependency graph.
        agent_sessions: List of active/completed agent session records.
        cost_accumulator: Per-model cost breakdown.
        wal_position: Sequence number of the last committed WAL entry.
    """

    metadata: CheckpointMetadata
    task_graph: dict[str, Any]
    agent_sessions: list[dict[str, Any]]
    cost_accumulator: dict[str, float]
    wal_position: int


def create_checkpoint(
    metadata: CheckpointMetadata,
    task_graph: dict[str, Any],
    agent_sessions: list[dict[str, Any]],
    cost_accumulator: dict[str, float],
    wal_position: int,
) -> Checkpoint:
    """Create a new checkpoint from orchestrator state.

    Args:
        metadata: Checkpoint identification and summary statistics.
        task_graph: Serialized task dependency graph.
        agent_sessions: List of active/completed agent session records.
        cost_accumulator: Per-model cost breakdown.
        wal_position: Sequence number of the last committed WAL entry.

    Returns:
        A frozen Checkpoint instance.
    """
    return Checkpoint(
        metadata=metadata,
        task_graph=task_graph,
        agent_sessions=agent_sessions,
        cost_accumulator=cost_accumulator,
        wal_position=wal_position,
    )


def save_checkpoint(checkpoint: Checkpoint, output_dir: Path) -> Path:
    """Persist a checkpoint as a JSON file.

    The file is written atomically (write-to-temp then rename) to avoid
    partial writes on crash.

    Args:
        checkpoint: The checkpoint to save.
        output_dir: Directory to write the checkpoint file into.

    Returns:
        Path to the written checkpoint file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"checkpoint-{checkpoint.metadata.checkpoint_id}.json"
    target = output_dir / filename
    tmp = target.with_suffix(".json.tmp")

    payload = _checkpoint_to_dict(checkpoint)
    data = json.dumps(payload, indent=2, sort_keys=True)

    tmp.write_text(data, encoding="utf-8")
    tmp.replace(target)  # replace() works on Windows; rename() fails if target exists

    logger.info(
        "Saved checkpoint %s (%d/%d tasks) to %s",
        checkpoint.metadata.checkpoint_id,
        checkpoint.metadata.completed_count,
        checkpoint.metadata.task_count,
        target,
    )
    return target


def load_checkpoint(checkpoint_path: Path) -> Checkpoint | None:
    """Load a checkpoint from a JSON file.

    Args:
        checkpoint_path: Path to the checkpoint JSON file.

    Returns:
        Checkpoint if the file is valid, None if missing or corrupt.
    """
    if not checkpoint_path.is_file():
        logger.warning("Checkpoint file not found: %s", checkpoint_path)
        return None

    try:
        raw = checkpoint_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return _checkpoint_from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Corrupt checkpoint %s: %s", checkpoint_path, exc)
        return None


def list_checkpoints(checkpoint_dir: Path) -> list[CheckpointMetadata]:
    """List available checkpoints sorted by creation time (oldest first).

    Args:
        checkpoint_dir: Directory containing checkpoint JSON files.

    Returns:
        List of CheckpointMetadata sorted ascending by created_at.
    """
    if not checkpoint_dir.is_dir():
        return []

    results: list[CheckpointMetadata] = []
    for path in checkpoint_dir.glob("checkpoint-*.json"):
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            meta_dict = data["metadata"]
            results.append(CheckpointMetadata(**meta_dict))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping corrupt checkpoint %s: %s", path, exc)

    results.sort(key=lambda m: m.created_at)
    return results


def validate_checkpoint(checkpoint: Checkpoint) -> list[str]:
    """Validate checkpoint integrity.

    Checks internal consistency: counts vs. actual data, non-negative
    values, ISO timestamp format, and WAL position validity.

    Args:
        checkpoint: The checkpoint to validate.

    Returns:
        List of validation error messages. Empty list means valid.
    """
    errors: list[str] = []
    meta = checkpoint.metadata

    # checkpoint_id must be non-empty
    if not meta.checkpoint_id:
        errors.append("checkpoint_id is empty")

    # created_at must be valid ISO-8601
    try:
        datetime.fromisoformat(meta.created_at)
    except (ValueError, TypeError):
        errors.append(f"created_at is not valid ISO-8601: {meta.created_at!r}")

    # bernstein_version must be non-empty
    if not meta.bernstein_version:
        errors.append("bernstein_version is empty")

    # Non-negative counts
    if meta.task_count < 0:
        errors.append(f"task_count is negative: {meta.task_count}")
    if meta.completed_count < 0:
        errors.append(f"completed_count is negative: {meta.completed_count}")
    if meta.failed_count < 0:
        errors.append(f"failed_count is negative: {meta.failed_count}")

    # completed + failed should not exceed total
    if meta.completed_count + meta.failed_count > meta.task_count:
        errors.append(
            f"completed_count ({meta.completed_count}) + failed_count "
            f"({meta.failed_count}) exceeds task_count ({meta.task_count})"
        )

    # Non-negative cost
    if meta.cost_usd < 0:
        errors.append(f"cost_usd is negative: {meta.cost_usd}")

    # WAL position must be non-negative
    if checkpoint.wal_position < 0:
        errors.append(f"wal_position is negative: {checkpoint.wal_position}")

    return errors


# ---------------------------------------------------------------------------
# Internal serialization helpers
# ---------------------------------------------------------------------------


def _checkpoint_to_dict(checkpoint: Checkpoint) -> dict[str, Any]:
    """Serialize a Checkpoint to a plain dict for JSON encoding."""
    return asdict(checkpoint)


def _checkpoint_from_dict(data: dict[str, Any]) -> Checkpoint:
    """Deserialize a Checkpoint from a plain dict.

    Raises:
        KeyError: If required keys are missing.
        TypeError: If values have wrong types.
    """
    meta_dict = data["metadata"]
    metadata = CheckpointMetadata(**meta_dict)
    return Checkpoint(
        metadata=metadata,
        task_graph=data["task_graph"],
        agent_sessions=data["agent_sessions"],
        cost_accumulator=data["cost_accumulator"],
        wal_position=data["wal_position"],
    )
