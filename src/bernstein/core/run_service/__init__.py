"""Detached run service: submit a goal, disconnect, reattach later (#2352).

A run used to be coupled to the invoking terminal and one machine's uptime.
This package lets an operator submit a goal, drop the terminal, and reattach
from another shell later -- built entirely on the durable work ledger
(:mod:`bernstein.core.persistence.work_ledger`) so the daemon owns *execution*
while the ledger owns *state*.

The pieces:

* :class:`RunService` -- open a run, and record every lifecycle boundary
  (submit / detach / reattach / daemon restart / complete) as a signed receipt
  in the HMAC audit chain.
* :func:`prove_continuity` / :class:`ContinuityProof` -- the reattach artefact:
  a deterministic proof that the current ledger head extends the head the
  operator last saw, so nothing happened off the record while they were away.
* :mod:`supervisor` -- the detached background process that advances the run
  by projecting the ledger frontier, resumable after a hard kill with zero
  lost completed tasks.
* :func:`verify_run` -- offline re-verification of the audit chain, the ledger
  chain, and every continuity boundary.

Off-host execution (the ``ssh`` sandbox backend and hosted sandbox backends)
is a documented follow-on; this package ships the single-host detached path.
"""

from __future__ import annotations

from bernstein.core.run_service.descriptor import (
    RunDescriptor,
    RunDescriptorError,
    goal_digest,
)
from bernstein.core.run_service.paths import RunServicePathError
from bernstein.core.run_service.receipts import (
    CONTINUITY_TRANSITIONS,
    LIFECYCLE_TRANSITIONS,
    TRANSITION_COMPLETED,
    TRANSITION_DAEMON_RESTARTED,
    TRANSITION_DETACHED,
    TRANSITION_REATTACHED,
    TRANSITION_SUBMITTED,
    ContinuityProof,
    prove_continuity,
)
from bernstein.core.run_service.service import (
    AttachResult,
    RunHandle,
    RunService,
    RunServiceError,
)
from bernstein.core.run_service.supervisor import (
    SupervisorStatus,
    advance_run,
    serve_run,
    spawn_detached,
    stop_supervisor,
    supervisor_status,
)
from bernstein.core.run_service.verify import RunVerification, verify_run

__all__ = [
    "CONTINUITY_TRANSITIONS",
    "LIFECYCLE_TRANSITIONS",
    "TRANSITION_COMPLETED",
    "TRANSITION_DAEMON_RESTARTED",
    "TRANSITION_DETACHED",
    "TRANSITION_REATTACHED",
    "TRANSITION_SUBMITTED",
    "AttachResult",
    "ContinuityProof",
    "RunDescriptor",
    "RunDescriptorError",
    "RunHandle",
    "RunService",
    "RunServiceError",
    "RunServicePathError",
    "RunVerification",
    "SupervisorStatus",
    "advance_run",
    "goal_digest",
    "prove_continuity",
    "serve_run",
    "spawn_detached",
    "stop_supervisor",
    "supervisor_status",
    "verify_run",
]
