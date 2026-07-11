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

Off-host execution ships too: :mod:`ssh_runner` runs each task of a detached
goal on another host over ssh in its own isolated remote git worktree, binding
that worktree in a signed ``run.ssh_task`` receipt; credentials flow through the
credential vault only, never the ledger or the receipts.
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
from bernstein.core.run_service.ssh_runner import (
    SSHBackendSpec,
    SSHRunnerError,
    SSHTaskReceipt,
    SSHTaskRunner,
    build_ssh_backend,
    read_ssh_spec,
    run_goal_on_ssh,
    write_ssh_spec,
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
    "SSHBackendSpec",
    "SSHRunnerError",
    "SSHTaskReceipt",
    "SSHTaskRunner",
    "SupervisorStatus",
    "advance_run",
    "build_ssh_backend",
    "goal_digest",
    "prove_continuity",
    "read_ssh_spec",
    "run_goal_on_ssh",
    "serve_run",
    "spawn_detached",
    "stop_supervisor",
    "supervisor_status",
    "verify_run",
    "write_ssh_spec",
]
