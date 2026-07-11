"""Auto-seal a verification-evidence bundle at task completion (issue #2362, AC1).

The orchestrator completion path (``_reap_and_cleanup_session``) calls
:func:`seal_evidence_on_completion` for every task that transitions to the
terminal completed state, right before its worktree is reclaimed. The gate
turns the declared producers into a signed, spine-anchored, audit-chained
bundle -- the proof-of-done receipt the operator later inspects.

The wiring is deliberately fail-open. Evidence sealing must NEVER block,
delay-fatally, or fail a task completion:

* a task that declares no producers is a zero-touch no-op (no gate runs, no
  directory is created);
* any error raised while running or sealing the producers is caught, logged
  with a sanitised warning, and swallowed so the completion proceeds unchanged.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from bernstein.core.evidence.bundle import parse_producers, run_evidence_gate
from bernstein.core.security.sanitize import sanitize_log

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.evidence.bundle import EvidenceBundle
    from bernstein.core.tasks.models import Task

logger = logging.getLogger(__name__)

__all__ = ["seal_evidence_on_completion"]


def seal_evidence_on_completion(
    workdir: Path,
    task: Task,
    *,
    timestamp: int | None = None,
) -> EvidenceBundle | None:
    """Seal an evidence bundle for a completing task, fail-open (AC1).

    Runs the task's declared evidence producers through
    :func:`bernstein.core.evidence.bundle.run_evidence_gate`, sealing the
    outputs into a signed, spine-anchored bundle mirrored in the HMAC audit
    chain. The gate verdict is intentionally ignored here: a *required*
    producer failure is recorded in the sealed bundle for the reviewer, but it
    does not retroactively fail a task the janitor already accepted.

    Args:
        workdir: The durable project root the bundle is anchored under (the
            orchestrator work dir, not the ephemeral session worktree).
        task: The completing task; its ``evidence_producers`` drive the gate.
        timestamp: Seal timestamp; defaults to the wall clock.

    Returns:
        The sealed :class:`EvidenceBundle`, or ``None`` when the task declared
        no producers or the gate raised.
    """
    producer_specs = task.evidence_producers
    if not producer_specs:
        # Zero-touch no-op: tasks that never opted in pay no cost and leave no
        # trace, so existing completions are byte-for-byte unchanged.
        return None

    try:
        producers = parse_producers(producer_specs)
        bundle, _gate_passed = run_evidence_gate(
            workdir=workdir,
            task_id=task.id,
            producers=producers,
            timestamp=timestamp if timestamp is not None else int(time.time()),
        )
    except Exception as exc:  # fail-open: sealing must never fail a task completion
        logger.warning(
            "evidence: sealing bundle for task %s failed; completion proceeds unchanged: %s",
            task.id,
            sanitize_log(str(exc)),
        )
        return None
    return bundle
