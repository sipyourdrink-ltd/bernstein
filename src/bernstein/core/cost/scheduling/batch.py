"""Batch dispatch routing, capability-gated (#2354).

Batch endpoints offer large discounts for non-interactive work, but only some
providers expose one. Routing is therefore gated on the adapter batch
capability map declared in :mod:`bernstein.adapters._contract` -- the single
source of truth -- so batch-eligible work reaches a batch endpoint *only* when
the adapter actually has one, and an eligible task on a non-batch adapter is
*refused* (routed interactively with a recorded reason) rather than silently
faked into a batch path that does not exist.

The routing decision is a pure function of ``(task eligibility, adapter
capability)`` with no side effects, so it composes with the deterministic
dispatch policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from bernstein.adapters._contract import BatchDispatchCapability, batch_dispatch_capability

#: Recorded when a batch-eligible task cannot be routed to a batch endpoint
#: because the adapter exposes no batch surface. The task runs interactively;
#: the reason makes the refusal auditable rather than silent.
REFUSED_NO_BATCH_SURFACE = "adapter_no_batch_surface"

BatchRoute = Literal["batch", "interactive"]


@dataclass(frozen=True, slots=True)
class BatchRouteDecision:
    """How one task is routed with respect to batch dispatch."""

    task_id: str
    adapter: str
    batch_eligible: bool
    adapter_capable: bool
    capability: str
    route: BatchRoute
    refused_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "adapter": self.adapter,
            "batch_eligible": self.batch_eligible,
            "adapter_capable": self.adapter_capable,
            "capability": self.capability,
            "route": self.route,
            "refused_reason": self.refused_reason,
        }


def route_batch(*, task_id: str, adapter: str, batch_eligible: bool) -> BatchRouteDecision:
    """Route a task to the batch or interactive path (AC3).

    * A batch-eligible task on a batch-capable adapter routes to ``batch``.
    * A batch-eligible task on an adapter with no batch surface is *refused*:
      it routes ``interactive`` with :data:`REFUSED_NO_BATCH_SURFACE` recorded,
      never faked onto a non-existent batch endpoint.
    * A task that is not batch-eligible always routes ``interactive`` with no
      refusal reason -- it was simply never a batch candidate.

    Args:
        task_id: The task being routed.
        adapter: Registry name (or session-namespace form) of the adapter.
        batch_eligible: Whether policy marked the task batch-eligible.

    Returns:
        A :class:`BatchRouteDecision`.
    """
    capability = batch_dispatch_capability(adapter)
    capable = capability is BatchDispatchCapability.NATIVE

    if not batch_eligible:
        route: BatchRoute = "interactive"
        refused = ""
    elif capable:
        route = "batch"
        refused = ""
    else:
        route = "interactive"
        refused = REFUSED_NO_BATCH_SURFACE

    return BatchRouteDecision(
        task_id=task_id,
        adapter=adapter,
        batch_eligible=batch_eligible,
        adapter_capable=capable,
        capability=str(capability),
        route=route,
        refused_reason=refused,
    )


__all__ = [
    "REFUSED_NO_BATCH_SURFACE",
    "BatchRoute",
    "BatchRouteDecision",
    "route_batch",
]
