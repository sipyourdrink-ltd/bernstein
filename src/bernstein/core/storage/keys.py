"""Canonical sink key-naming helpers.

Bernstein's artifact layout is historically a set of well-known paths
under ``.sdd/``. When the persistence layer switches to pluggable
sinks (oai-003) the same logical layout must be reproducible on every
backend: a key like ``runtime/wal/run-123.wal.jsonl`` has to mean the
exact same artifact whether the sink is ``LocalFsSink`` or ``S3Sink``.

This module centralises the key-naming convention so callers never
hand-construct a path in one place and miss it in another. The keys
returned here are sink-neutral; any character a particular object
store disallows must be rejected at the sink boundary via
:func:`bernstein.core.storage.sink.normalise_key`.

The layout is::

    runtime/wal/{run_id}.wal.jsonl
    runtime/wal/{run_id}.wal.closed
    runtime/wal/uncommitted.idx.json
    runtime/checkpoints/{run_id}/{checkpoint_id}.json
    runtime/state.json
    tasks/{task_id}/output.json
    tasks/{task_id}/progress.jsonl
    audit/{YYYY-MM-DD}.jsonl
    audit/{YYYY-MM-DD}.jsonl.gz          # rotated + compressed
    metrics/{run_id}/{timestamp}.json
    cost/{run_id}/ledger.jsonl
"""

from __future__ import annotations

from bernstein.core.storage.sink import join_keys


def wal_key(run_id: str) -> str:
    """Key for the WAL JSONL file of *run_id*."""
    return join_keys("runtime", "wal", f"{run_id}.wal.jsonl")


def wal_closed_marker_key(run_id: str) -> str:
    """Key for the ``.closed`` sidecar of *run_id*'s WAL."""
    return join_keys("runtime", "wal", f"{run_id}.wal.closed")


def uncommitted_index_key() -> str:
    """Key for the shared uncommitted-WAL index."""
    return join_keys("runtime", "wal", "uncommitted.idx.json")


def checkpoint_key(run_id: str, checkpoint_id: str) -> str:
    """Key for a specific checkpoint file."""
    return join_keys("runtime", "checkpoints", run_id, f"{checkpoint_id}.json")


def state_key() -> str:
    """Key for the top-level runtime state file."""
    return join_keys("runtime", "state.json")


def task_output_key(task_id: str) -> str:
    """Key for a task's final output artifact."""
    return join_keys("tasks", task_id, "output.json")


def task_progress_key(task_id: str) -> str:
    """Key for the JSONL stream of a task's progress events."""
    return join_keys("tasks", task_id, "progress.jsonl")


def audit_log_key(date: str) -> str:
    """Key for the HMAC audit log of a specific calendar day.

    Args:
        date: ``YYYY-MM-DD`` formatted date string.
    """
    return join_keys("audit", f"{date}.jsonl")


def rotated_audit_key(date: str) -> str:
    """Key for a rotated + gzipped audit log."""
    return join_keys("audit", f"{date}.jsonl.gz")


def metrics_dump_key(run_id: str, timestamp: str) -> str:
    """Key for a metrics snapshot within *run_id*."""
    return join_keys("metrics", run_id, f"{timestamp}.json")


def cost_ledger_key(run_id: str) -> str:
    """Key for the cost ledger JSONL for *run_id*."""
    return join_keys("cost", run_id, "ledger.jsonl")


__all__ = [
    "audit_log_key",
    "checkpoint_key",
    "cost_ledger_key",
    "metrics_dump_key",
    "rotated_audit_key",
    "state_key",
    "task_output_key",
    "task_progress_key",
    "uncommitted_index_key",
    "wal_closed_marker_key",
    "wal_key",
]
