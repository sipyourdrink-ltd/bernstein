"""Bounded recursive task decomposition guard (#4179).

Enforces strict well-foundedness (strictly decreasing non-negative ranks)
on recursive task decomposition, refusing non-decreasing or exhausted rank
proposals with machine-readable codes and recording spawn-side refusal receipts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)

DEFAULT_ROOT_RANK = 5
ENV_MAX_DECOMPOSITION_DEPTH = "BERNSTEIN_MAX_DECOMPOSITION_DEPTH"


def get_default_root_rank() -> int:
    """Return configured or default root decomposition rank ceiling."""
    raw = os.environ.get(ENV_MAX_DECOMPOSITION_DEPTH, "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_ROOT_RANK


class DecompositionRefusalCode:
    """Reasons the decomposition rank guard refuses a further split."""

    DECOMPOSITION_RANK_EXHAUSTED = "DECOMPOSITION_RANK_EXHAUSTED"
    DECOMPOSITION_RANK_NON_DECREASING = "DECOMPOSITION_RANK_NON_DECREASING"


@dataclass(frozen=True)
class DecompositionRefusalReceipt:
    """Spawn-side refusal receipt for illegal or un-bounded task decomposition."""

    parent_task_id: str
    offending_child_id: str
    parent_rank: int
    child_rank: int
    reason_code: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_task_id": self.parent_task_id,
            "offending_child_id": self.offending_child_id,
            "parent_rank": self.parent_rank,
            "child_rank": self.child_rank,
            "reason_code": self.reason_code,
            "timestamp": self.timestamp,
        }

    def canonical_bytes(self) -> bytes:
        """Return deterministic JSON bytes excluding timestamp for audit verification."""
        d = {
            "parent_task_id": self.parent_task_id,
            "offending_child_id": self.offending_child_id,
            "parent_rank": self.parent_rank,
            "child_rank": self.child_rank,
            "reason_code": self.reason_code,
        }
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class DecompositionGuardVerdict:
    """Outcome of decomposition proposal evaluation."""

    accepted: bool
    reason_code: str | None = None
    receipt: DecompositionRefusalReceipt | None = None


def evaluate_decomposition_proposal(
    parent_task_id: str,
    parent_rank: int | None,
    child_proposals: list[dict[str, Any]],
    *,
    now_ts: float | None = None,
) -> tuple[DecompositionGuardVerdict, list[int]]:
    """Evaluate decomposition proposal against strict rank reduction.

    Args:
        parent_task_id: Identifier of the parent task being decomposed.
        parent_rank: Rank of the parent task, or None to use default root rank.
        child_proposals: List of proposed child task dicts (may contain 'rank').
        now_ts: Optional explicit timestamp for deterministic testing.

    Returns:
        Tuple of (verdict, assigned_child_ranks).
    """
    ts = now_ts if now_ts is not None else time.time()
    effective_parent_rank = parent_rank if parent_rank is not None else get_default_root_rank()

    if effective_parent_rank <= 0:
        receipt = DecompositionRefusalReceipt(
            parent_task_id=parent_task_id,
            offending_child_id=str(child_proposals[0].get("id", "child-0")) if child_proposals else "none",
            parent_rank=effective_parent_rank,
            child_rank=effective_parent_rank,
            reason_code=DecompositionRefusalCode.DECOMPOSITION_RANK_EXHAUSTED,
            timestamp=ts,
        )
        _logger.warning(
            "Refused task decomposition for %s: parent rank %d exhausted",
            parent_task_id,
            effective_parent_rank,
        )
        return DecompositionGuardVerdict(
            accepted=False,
            reason_code=DecompositionRefusalCode.DECOMPOSITION_RANK_EXHAUSTED,
            receipt=receipt,
        ), []

    assigned_ranks: list[int] = []
    default_child_rank = effective_parent_rank - 1

    for i, child in enumerate(child_proposals):
        child_id = str(child.get("id", f"child-{i}"))
        raw_child_rank = child.get("decomposition_rank")
        if raw_child_rank is not None and isinstance(raw_child_rank, int):
            proposed_rank = raw_child_rank
        else:
            proposed_rank = default_child_rank

        if proposed_rank >= effective_parent_rank:
            receipt = DecompositionRefusalReceipt(
                parent_task_id=parent_task_id,
                offending_child_id=child_id,
                parent_rank=effective_parent_rank,
                child_rank=proposed_rank,
                reason_code=DecompositionRefusalCode.DECOMPOSITION_RANK_NON_DECREASING,
                timestamp=ts,
            )
            _logger.warning(
                "Refused task decomposition for %s -> child %s: rank %d >= parent rank %d",
                parent_task_id,
                child_id,
                proposed_rank,
                effective_parent_rank,
            )
            return DecompositionGuardVerdict(
                accepted=False,
                reason_code=DecompositionRefusalCode.DECOMPOSITION_RANK_NON_DECREASING,
                receipt=receipt,
            ), []

        assigned_ranks.append(proposed_rank)

    return DecompositionGuardVerdict(accepted=True), assigned_ranks
