"""Cache-window fan-out scheduling, capability-gated + default off (#2354).

Prompt-cache hits are an order of magnitude cheaper than a fresh prompt but
expire on short TTLs. When M workers share a prompt prefix, dispatching them
concurrently makes them race to write the cache M times; issuing one *warm-up*
call first primes the cache so the M workers all hit it inside the TTL window.

Two guards keep this safe:

* **Capability-gated.** A warm-up only helps on an adapter whose upstream
  documents a prompt-cache window. The capability comes from
  :data:`bernstein.adapters._contract.CACHE_WINDOW_CAPABILITY_MATRIX` -- the
  single source of truth -- never from probing.
* **Conservative default off.** Even a capable adapter needs an explicit
  ``enabled`` opt-in. With the default (off) the fan-out issues no warm-up and
  assumes no hits, so turning the feature on is always a deliberate operator
  choice.

:func:`plan_cache_fanout` produces a pure plan; :func:`execute_cache_fanout`
drives a caller-supplied warm-up and worker callable against the plan so the
one-warm-up-plus-M-hits contract is testable against any adapter shim.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.adapters._contract import CacheWindowCapability, cache_window_capability

if TYPE_CHECKING:
    from collections.abc import Callable

#: Reasons a fan-out plan issues no warm-up.
REASON_ACTIVE = "cache_window_active"
REASON_DISABLED = "cache_window_disabled"
REASON_NO_CACHE_WINDOW = "adapter_no_cache_window"
REASON_NO_WORKERS = "no_workers"


def _prefix_hash(prefix: str) -> str:
    return "sha256:" + hashlib.sha256(prefix.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheFanoutPlan:
    """A deterministic plan for a shared-prefix worker fan-out."""

    adapter: str
    worker_count: int
    prefix_hash: str
    enabled: bool
    capability: str
    warmup_calls: int
    fanout_calls: int
    cache_hits_expected: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "worker_count": self.worker_count,
            "prefix_hash": self.prefix_hash,
            "enabled": self.enabled,
            "capability": self.capability,
            "warmup_calls": self.warmup_calls,
            "fanout_calls": self.fanout_calls,
            "cache_hits_expected": self.cache_hits_expected,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CacheFanoutResult:
    """Observed outcome of driving a :class:`CacheFanoutPlan`."""

    warmup_calls_made: int
    worker_calls_made: int
    cache_hits: int


def plan_cache_fanout(
    *,
    adapter: str,
    worker_count: int,
    prefix: str,
    enabled: bool = False,
) -> CacheFanoutPlan:
    """Plan a shared-prefix fan-out of ``worker_count`` workers (AC4).

    Only when the adapter is cache-window capable *and* ``enabled`` is true
    (conservative default off) does the plan issue one warm-up call and expect
    ``worker_count`` cache hits. Otherwise it issues no warm-up and expects no
    hits, and the ``reason`` records why.

    Args:
        adapter: Registry name (or session-namespace form) of the adapter.
        worker_count: Number of workers sharing ``prefix``.
        prefix: The shared prompt prefix (hashed into the plan, never stored).
        enabled: Operator opt-in; the conservative default is ``False``.

    Returns:
        A :class:`CacheFanoutPlan`.
    """
    capability = cache_window_capability(adapter)
    capable = capability is CacheWindowCapability.SUPPORTED
    fanout = max(0, worker_count)
    prefix_hash = _prefix_hash(prefix)

    if fanout == 0:
        reason = REASON_NO_WORKERS
        warmup = 0
        hits = 0
    elif not capable:
        reason = REASON_NO_CACHE_WINDOW
        warmup = 0
        hits = 0
    elif not enabled:
        reason = REASON_DISABLED
        warmup = 0
        hits = 0
    else:
        reason = REASON_ACTIVE
        warmup = 1
        hits = fanout

    return CacheFanoutPlan(
        adapter=adapter,
        worker_count=fanout,
        prefix_hash=prefix_hash,
        enabled=enabled,
        capability=str(capability),
        warmup_calls=warmup,
        fanout_calls=fanout,
        cache_hits_expected=hits,
        reason=reason,
    )


def execute_cache_fanout(
    plan: CacheFanoutPlan,
    *,
    warmup_call: Callable[[], None],
    worker_call: Callable[[int], bool],
) -> CacheFanoutResult:
    """Drive *plan*: issue the warm-up (if any) then the M worker calls.

    ``warmup_call`` is invoked ``plan.warmup_calls`` times (0 or 1) before any
    worker; ``worker_call(i)`` is invoked ``plan.fanout_calls`` times and
    returns whether that call was a cache hit. The warm-up strictly precedes
    the fan-out so a capable + enabled plan primes the cache once and every
    worker hits it.

    Returns:
        A :class:`CacheFanoutResult` with the observed call counts and the
        number of cache hits reported by the workers.
    """
    warmup_made = 0
    for _ in range(plan.warmup_calls):
        warmup_call()
        warmup_made += 1

    worker_made = 0
    cache_hits = 0
    for i in range(plan.fanout_calls):
        hit = worker_call(i)
        worker_made += 1
        if hit:
            cache_hits += 1

    return CacheFanoutResult(
        warmup_calls_made=warmup_made,
        worker_calls_made=worker_made,
        cache_hits=cache_hits,
    )


__all__ = [
    "REASON_ACTIVE",
    "REASON_DISABLED",
    "REASON_NO_CACHE_WINDOW",
    "REASON_NO_WORKERS",
    "CacheFanoutPlan",
    "CacheFanoutResult",
    "execute_cache_fanout",
    "plan_cache_fanout",
]
