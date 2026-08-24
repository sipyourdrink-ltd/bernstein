"""Deterministic plan rendering with SHA-256 hash binding (#3839 slice 3).

Produces a canonical text representation of a :class:`TaskPlan` and a
stable SHA-256 digest. The rendering is *order-invariant*: tasks are
sorted by id before serialisation, so the same plan always yields the
same hash regardless of the order tasks appear in ``plan.task_estimates``.

``bernstein.core.security.plan_approval`` binds the digest into the
approval decision: ``create_plan`` stores it on the plan, and
``PlanStore.verify_rendering_hash`` recomputes it before a plan is
approved or rejected, so a plan edited after it was rendered for review
is refused instead of silently promoting tasks the operator never saw.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.cost.profile_report import canonical_json_bytes

if TYPE_CHECKING:
    from bernstein.core.tasks.models import TaskCostEstimate, TaskPlan


def _estimate_dict(e: TaskCostEstimate) -> dict[str, Any]:
    """Serialise a TaskCostEstimate to a canonical dict.

    Field types come from the :class:`TaskCostEstimate` dataclass contract
    and are not re-validated here; a caller that hands over something else
    is a type error at the boundary, not a runtime branch.
    """
    return {
        "task_id": e.task_id,
        "title": e.title,
        "role": e.role,
        "model": e.model,
        "estimated_tokens": e.estimated_tokens,
        "estimated_cost_usd": round(e.estimated_cost_usd, 6),
        "risk_level": e.risk_level,
        "risk_reasons": sorted(e.risk_reasons),
    }


@dataclass(frozen=True)
class PlanRendering:
    """Deterministic text rendering of a plan plus its SHA-256 hash.

    Attributes:
        text: The canonical human-readable text representation.
        rendering_hash: SHA-256 hex digest of the canonical JSON payload.
        journal_head: Optional Merkle head of the event journal at render time.
    """

    text: str
    rendering_hash: str
    journal_head: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "text": self.text,
            "rendering_hash": self.rendering_hash,
            "journal_head": self.journal_head,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PlanRendering:
        """Deserialise from a dict produced by :meth:`to_dict`."""
        return cls(
            text=d["text"],
            rendering_hash=d["rendering_hash"],
            journal_head=d.get("journal_head"),
        )


def compute_plan_rendering(
    plan: TaskPlan,
    journal_head: str | None = None,
) -> PlanRendering:
    """Produce a deterministic rendering of *plan* and compute its SHA-256.

    The text output is stable: tasks are always sorted by ``task_id``,
    risk reasons are sorted, and numeric values are rounded to a
    deterministic precision.  The hash is computed over the *canonical
    JSON payload* (not the human-readable text) so that format changes
    in the text do not break hash stability.

    Args:
        plan: The task execution plan to render.
        journal_head: Optional Merkle head of the event journal to bind into
            the hash. The approval gate renders without one, because the
            digest it stores has to stay reproducible from the plan alone;
            pass a head only where the caller also persists it and can
            supply the same value when the rendering is recomputed.

    Returns:
        A frozen :class:`PlanRendering` with text, hash, and journal head.
    """
    # Sort estimates by task_id for deterministic output.
    sorted_estimates = sorted(plan.task_estimates, key=lambda e: e.task_id)

    # Build the canonical payload (what gets hashed).
    payload: dict[str, Any] = {
        "plan_id": plan.id,
        "goal": plan.goal,
        "task_estimates": [_estimate_dict(e) for e in sorted_estimates],
        "total_estimated_cost_usd": round(plan.total_estimated_cost_usd, 6),
        "total_estimated_minutes": plan.total_estimated_minutes,
        "high_risk_tasks": sorted(plan.high_risk_tasks),
    }
    if journal_head is not None:
        payload["journal_head"] = journal_head

    rendering_hash = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    # Build the human-readable text.
    lines = [
        f"Plan: {plan.id}",
        f"Goal: {plan.goal}",
        "",
        "Tasks:",
    ]
    for e in sorted_estimates:
        reasons = "; ".join(sorted(e.risk_reasons)) if e.risk_reasons else ""
        lines.append(
            f"  - {e.task_id}: {e.title} | role={e.role} model={e.model} "
            f"tokens={e.estimated_tokens} cost=${e.estimated_cost_usd:.6f} "
            f"risk={e.risk_level}" + (f" reasons={reasons}" if reasons else "")
        )

    lines.append("")
    lines.append(f"Total cost: ${plan.total_estimated_cost_usd:.6f}")
    lines.append(f"Total minutes: {plan.total_estimated_minutes}")
    lines.append(f"High-risk tasks: {sorted(plan.high_risk_tasks)}")

    if journal_head is not None:
        lines.append(f"Journal head: {journal_head}")

    lines.append("")
    lines.append(f"Rendering hash: {rendering_hash}")

    text = "\n".join(lines)
    return PlanRendering(text=text, rendering_hash=rendering_hash, journal_head=journal_head)
