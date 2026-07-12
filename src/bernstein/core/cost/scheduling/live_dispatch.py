"""Live wiring for batch routing and cache-window fan-out (#2354).

The deterministic decision functions
(:func:`~bernstein.core.cost.scheduling.batch.route_batch` and
:func:`~bernstein.core.cost.scheduling.cache_window.plan_cache_fanout` /
:func:`~bernstein.core.cost.scheduling.cache_window.execute_cache_fanout`)
shipped in the cost policy layer, but nothing consulted them from a live run's
dispatch and fan-out paths. This module is that bridge:

* :func:`decide_batch_route` resolves the adapter a task would run on, derives
  batch eligibility from the same signal the provider-batch path already uses,
  and defers to ``route_batch`` so a batch-eligible task reaches the batch
  surface *only* on a batch-capable adapter. A non-eligible task never routes to
  batch; a batch-eligible task on an adapter with no batch surface is *refused*
  (routed interactively with a recorded reason), never faked.
* :func:`seal_batch_route` anchors that decision as a ``cost.batch_route`` audit
  event so the routing of a task is a verifiable receipt, not a log line.
* :func:`run_cache_window_fanout` drives ``plan_cache_fanout`` +
  ``execute_cache_fanout`` for a shared-prefix worker fan-out, issuing one
  warm-up call strictly before the M worker calls so a capable + enabled adapter
  primes the shared prompt cache once and every worker hits it.

Everything here is a pure function of its inputs plus the adapter capability map
(the single source of truth, never probed), so the routing verdict a live run
takes is the same verdict a verifier recomputes from the chain.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bernstein.core.cost.scheduling.batch import BatchRouteDecision, route_batch
from bernstein.core.cost.scheduling.cache_window import (
    CacheFanoutResult,
    execute_cache_fanout,
    plan_cache_fanout,
)
from bernstein.core.tasks.batch_router import BatchMode, classify_batch_mode

if TYPE_CHECKING:
    from collections.abc import Callable

    from bernstein.core.tasks.models import Task

logger = logging.getLogger(__name__)

#: Conservative fallback when no adapter can be resolved for a task. ``generic``
#: is not in the batch-capable set, so an unresolvable adapter never routes work
#: to a batch surface it may not have.
_DEFAULT_ADAPTER = "generic"


def resolve_task_adapter(orch: Any, task: Task) -> str:
    """Return the adapter registry name that would run *task*.

    Resolution mirrors the spawner: a per-role ``role_model_policy`` adapter pin
    wins, else the spawner's ``default_adapter_name`` (the active adapter), else
    a conservative :data:`_DEFAULT_ADAPTER` fallback that is never batch-capable.
    """
    spawner = getattr(orch, "_spawner", None)
    role_policy = getattr(spawner, "role_model_policy", None)
    if isinstance(role_policy, dict):
        entry = role_policy.get(task.role)
        if isinstance(entry, dict):
            adapter = entry.get("adapter")
            if isinstance(adapter, str) and adapter:
                return adapter
    default = getattr(spawner, "default_adapter_name", None)
    if isinstance(default, str) and default:
        return default
    return _DEFAULT_ADAPTER


def is_batch_eligible(task: Task) -> bool:
    """Return whether *task* is a batch candidate.

    Uses the same classifier the provider-batch path consults
    (:func:`~bernstein.core.tasks.batch_router.classify_batch_mode`) so the
    route gate admits exactly the tasks the batch surface would accept and
    refuses none it would.
    """
    return classify_batch_mode(task).mode is BatchMode.BATCH


def decide_batch_route(orch: Any, task: Task) -> BatchRouteDecision:
    """Return the live batch-route decision for *task* (#2354, AC3).

    Resolves the adapter, derives batch eligibility, and defers to the
    capability-gated :func:`route_batch`: a batch-eligible task routes ``batch``
    only on a batch-capable adapter; a non-eligible task always routes
    ``interactive``; a batch-eligible task on an incapable adapter is refused.
    """
    adapter = resolve_task_adapter(orch, task)
    eligible = is_batch_eligible(task)
    return route_batch(task_id=task.id, adapter=adapter, batch_eligible=eligible)


def seal_batch_route(orch: Any, decision: BatchRouteDecision) -> None:
    """Anchor a batch-route decision as a ``cost.batch_route`` audit event.

    Best-effort: the receipt is a provenance aid, so a missing workdir, missing
    HMAC key, or IO error is logged (exception type only, since this path
    touches the audit key) and never crashes the dispatch. The spawn proceeds on
    the route the decision already fixed.
    """
    workdir = getattr(orch, "_workdir", None)
    if workdir is None:
        return
    try:
        from bernstein.core.security.audit import load_or_create_audit_key
        from bernstein.core.security.audit_chain import (
            AuditChainStore,
            record_cost_batch_route,
        )

        hmac_key = load_or_create_audit_key()
        chain = AuditChainStore(workdir / ".sdd" / "audit", key=hmac_key)
        record_cost_batch_route(
            chain=chain,
            run_id=str(getattr(orch, "_run_id", "")),
            task_id=decision.task_id,
            adapter=decision.adapter,
            batch_eligible=decision.batch_eligible,
            adapter_capable=decision.adapter_capable,
            capability=decision.capability,
            route=decision.route,
            refused_reason=decision.refused_reason,
        )
    except Exception as exc:  # sealing must never crash a dispatch
        logger.warning("cost: failed to seal batch-route receipt: %s", type(exc).__name__)


def run_cache_window_fanout(
    *,
    adapter: str,
    prefix: str,
    worker_count: int,
    warmup_call: Callable[[], None],
    worker_call: Callable[[int], bool],
    enabled: bool,
) -> CacheFanoutResult:
    """Drive a shared-prefix fan-out through the cache window (#2354, AC4).

    Plans the fan-out from the adapter cache-window capability and the *enabled*
    opt-in, then executes it: a capable + enabled adapter issues exactly one
    warm-up call (priming the shared prefix) strictly before the ``worker_count``
    worker calls, so every worker hits the warm cache inside the TTL. An
    incapable adapter, a disabled window, or a zero-worker fan-out issues no
    warm-up and drives the workers directly, so turning the feature on is always
    a deliberate operator choice.

    Args:
        adapter: Registry name of the adapter the workers run on.
        prefix: The shared prompt prefix (hashed into the plan, never stored).
        worker_count: Number of workers sharing *prefix*.
        warmup_call: Invoked once (0 or 1 times) before any worker.
        worker_call: ``worker_call(i)`` dispatches worker ``i`` and returns
            whether it hit the warm cache.
        enabled: Operator opt-in; the conservative default upstream is off.

    Returns:
        The :class:`~bernstein.core.cost.scheduling.cache_window.CacheFanoutResult`
        with the observed warm-up / worker call counts and cache hits.
    """
    plan = plan_cache_fanout(
        adapter=adapter,
        worker_count=worker_count,
        prefix=prefix,
        enabled=enabled,
    )
    return execute_cache_fanout(plan, warmup_call=warmup_call, worker_call=worker_call)


__all__ = [
    "decide_batch_route",
    "is_batch_eligible",
    "resolve_task_adapter",
    "run_cache_window_fanout",
    "seal_batch_route",
]
