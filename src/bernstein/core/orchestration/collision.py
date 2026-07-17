"""Deterministic concurrency-collision decisions for recipe fires.

Issue #2546. A registered recipe carries a concurrency cap and a collision
policy. When the supervisor is about to fire but a previous fire of the
same recipe is still running, it must not silently double-write. Instead it
evaluates a *pure* decision function and emits a collision receipt for
every overlap it considers, so a double-fire becomes a recorded decision
rather than an invisible race.

The decision is the crux of the guarantee: :func:`decide_collision` is a
deterministic function of ``(policy, running-fire state, cap)`` with a
stable receipt hash. Two operators over byte-identical running state record
the byte-identical receipt. Strip the receipt and the feature is just a
lock; keep it and every overlap is offline-verifiable.

Three strategies:

- ``ENQUEUE`` - defer the new fire behind the running one (do not dispatch a
  second graph now; the caller re-evaluates on the next tick).
- ``CANCEL_NEW`` - drop the new fire (never dispatch a second task graph
  while the previous is in flight).
- ``SUPERSEDE_WITH_HANDOFF`` - checkpoint the stale run and resume the new
  fire from the recorded checkpoint reference. When the checkpoint row is
  missing or tampered the handoff downgrades to a cold start, never a warm
  resume of an attacker-chosen session (the tamper posture carries over
  from :mod:`bernstein.core.tasks.checkpoint_retry`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "COLLISION_REV",
    "CollisionAction",
    "CollisionDecision",
    "CollisionPolicy",
    "RunningFireState",
    "decide_collision",
    "resolve_handoff_checkpoint",
]

#: Schema rev baked into the collision receipt. Bumping it changes every
#: receipt hash and is the single lever for evolving the decision contract.
COLLISION_REV = "1"


class CollisionPolicy(StrEnum):
    """Per-recipe concurrency-collision strategy (part of the canonical body)."""

    ENQUEUE = "enqueue"
    CANCEL_NEW = "cancel_new"
    SUPERSEDE_WITH_HANDOFF = "supersede_with_handoff"


class CollisionAction(StrEnum):
    """The resolved action a :class:`CollisionDecision` carries.

    ``DISPATCH`` means the new fire proceeds (no live collision, or a
    supersede handoff). ``ENQUEUE`` / ``CANCEL`` never dispatch a second
    graph; ``SUPERSEDE`` dispatches after checkpointing the stale run.
    """

    DISPATCH = "dispatch"
    ENQUEUE = "enqueue"
    CANCEL = "cancel"
    SUPERSEDE = "supersede"


@dataclass(frozen=True)
class RunningFireState:
    """The journal-derived state of a recipe's currently-running fire.

    Attributes:
        running: True when at least one prior fire of the recipe is still
            executing.
        running_count: How many fires of the recipe are in flight.
        fire_id: Identifier of the running fire (for the receipt).
        checkpoint_event_hash: The verified checkpoint reference of the
            running fire, or ``""`` when none exists / the checkpoint row
            failed chain verification. A supersede handoff resumes warm
            only when this is non-empty.
    """

    running: bool = False
    running_count: int = 0
    fire_id: str = ""
    checkpoint_event_hash: str = ""


@dataclass(frozen=True)
class CollisionDecision:
    """The deterministic outcome of a collision evaluation.

    Attributes:
        policy: The policy that produced the decision.
        action: The resolved :class:`CollisionAction`.
        dispatch: Whether the new fire's task graph is dispatched now.
        resume_from_checkpoint: The checkpoint event hash the new fire
            resumes from under a supersede handoff, or ``""``.
        warm_resume: True iff the supersede handoff resumes warm from a
            verified checkpoint; False means a cold start.
        canonical_bytes: The exact bytes the receipt hash is taken over.
        receipt_hash: SHA-256 of ``canonical_bytes``; the stable receipt id
            two operators over identical state both derive.
    """

    policy: CollisionPolicy
    action: CollisionAction
    dispatch: bool
    resume_from_checkpoint: str
    warm_resume: bool
    canonical_bytes: bytes
    receipt_hash: str


def decide_collision(
    *,
    policy: CollisionPolicy | str,
    running: RunningFireState,
    concurrency_cap: int = 1,
) -> CollisionDecision:
    """Return the deterministic collision decision for one fire attempt.

    PURE function: the outcome and its ``receipt_hash`` depend only on
    ``(policy, running, concurrency_cap)``. No wall clock, no host state.

    When the recipe is under its concurrency cap the new fire dispatches
    normally. At or over the cap the policy decides:

    - ``ENQUEUE`` -> defer (no dispatch);
    - ``CANCEL_NEW`` -> drop the new fire (no dispatch, so a supervisor tick
      over an unfinished previous fire never dispatches a second graph);
    - ``SUPERSEDE_WITH_HANDOFF`` -> dispatch, resuming from the running
      fire's verified checkpoint (warm) or cold when none is available.

    Args:
        policy: The recipe's collision policy.
        running: The running-fire state derived from the journal.
        concurrency_cap: Max concurrent fires allowed (default 1).

    Returns:
        A :class:`CollisionDecision` with a stable ``receipt_hash``.
    """
    resolved_policy = CollisionPolicy(policy)
    cap = max(1, concurrency_cap)
    collides = running.running and running.running_count >= cap

    resume_from = ""
    warm = False
    if not collides:
        action = CollisionAction.DISPATCH
        dispatch = True
    elif resolved_policy is CollisionPolicy.ENQUEUE:
        action = CollisionAction.ENQUEUE
        dispatch = False
    elif resolved_policy is CollisionPolicy.CANCEL_NEW:
        action = CollisionAction.CANCEL
        dispatch = False
    else:  # SUPERSEDE_WITH_HANDOFF
        action = CollisionAction.SUPERSEDE
        dispatch = True
        resume_from = running.checkpoint_event_hash
        warm = bool(running.checkpoint_event_hash)

    canonical_obj = {
        "rev": COLLISION_REV,
        "policy": str(resolved_policy),
        "action": str(action),
        "dispatch": dispatch,
        "concurrency_cap": cap,
        "running": running.running,
        "running_count": running.running_count,
        "fire_id": running.fire_id,
        "resume_from_checkpoint": resume_from,
        "warm_resume": warm,
    }
    canonical_bytes = json.dumps(canonical_obj, sort_keys=True, separators=(",", ":")).encode()
    receipt_hash = hashlib.sha256(canonical_bytes).hexdigest()
    return CollisionDecision(
        policy=resolved_policy,
        action=action,
        dispatch=dispatch,
        resume_from_checkpoint=resume_from,
        warm_resume=warm,
        canonical_bytes=canonical_bytes,
        receipt_hash=receipt_hash,
    )


def resolve_handoff_checkpoint(sdd_dir: Path, running_task_id: str) -> str:
    """Return the verified checkpoint event hash of *running_task_id*, or ``""``.

    Fail-closed bridge to :func:`bernstein.core.tasks.checkpoint_retry.latest_checkpoint`:
    a missing journal, an absent checkpoint row, or a checkpoint whose
    journal Merkle chain fails verification all resolve to ``""`` so a
    supersede handoff over a tampered checkpoint downgrades to a cold start
    rather than warm-resuming an attacker-chosen session.
    """
    from bernstein.core.tasks.checkpoint_retry import latest_checkpoint

    ref = latest_checkpoint(sdd_dir, running_task_id)
    return ref.event_hash if ref is not None else ""
