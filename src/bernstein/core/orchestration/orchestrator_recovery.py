"""Orchestrator WAL recovery: crash-safe replay from write-ahead log.

Extracted from orchestrator.py as part of ORCH-009 decomposition.
Functions here operate on an Orchestrator instance passed as the first
argument, keeping the Orchestrator class as the public facade.
"""

from __future__ import annotations

import logging
from typing import Any

from bernstein.core.wal import WALRecovery

logger = logging.getLogger(__name__)


def recover_from_wal(orch: Any) -> list[tuple[str, Any]]:
    """Check WAL files from previous runs for uncommitted entries.

    Scans all WAL files in ``.sdd/runtime/wal/`` (excluding the current
    run) for entries written with ``committed=False`` -- these represent
    task claims where the agent was never successfully spawned (crash
    between claim and spawn).

    Each uncommitted entry is logged for operator awareness and an
    acknowledgement entry is written to the current run's WAL so the
    recovery is itself auditable.

    Args:
        orch: The orchestrator instance.

    Returns:
        List of (run_id, WALEntry) tuples for all uncommitted entries found.
    """
    sdd_dir = orch._workdir / ".sdd"
    uncommitted = WALRecovery.scan_all_uncommitted(
        sdd_dir,
        exclude_run_id=orch._run_id,
    )
    if not uncommitted:
        return []

    logger.warning(
        "WAL recovery: found %d uncommitted entries from previous run(s)",
        len(uncommitted),
    )
    for run_id, entry in uncommitted:
        logger.info(
            "WAL uncommitted [run=%s seq=%d]: %s %s",
            run_id,
            entry.seq,
            entry.decision_type,
            entry.inputs,
        )
        # Record acknowledgement in current run's WAL for auditability
        try:
            orch._wal_writer.write_entry(
                decision_type="wal_recovery_ack",
                inputs={
                    "original_run_id": run_id,
                    "original_seq": entry.seq,
                    "original_decision_type": entry.decision_type,
                    "original_inputs": entry.inputs,
                },
                output={"action": "acknowledged"},
                actor="orchestrator",
                committed=True,
            )
        except OSError:
            logger.debug("WAL write failed for recovery ack (run=%s seq=%d)", run_id, entry.seq)

    orch._recorder.record(
        "wal_recovery",
        uncommitted_count=len(uncommitted),
        run_ids=sorted({r for r, _ in uncommitted}),
    )
    return uncommitted
