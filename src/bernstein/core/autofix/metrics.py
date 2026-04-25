"""Prometheus counters for the autofix daemon.

The autofix daemon exports two counters via the existing observability
stack:

* ``autofix_attempts_total{repo,outcome,classifier}`` — increments
  once per dispatched attempt.  ``outcome`` is one of ``success``,
  ``failed``, ``cost_capped``, ``needs_human``.  ``classifier`` is
  one of ``security``, ``flaky``, ``config``.
* ``autofix_cost_usd_total{repo}`` — increments by the per-attempt
  spend in USD.

Both metrics live in the dedicated registry exposed by
:mod:`bernstein.core.observability.prometheus` so the existing
``/metrics`` endpoint surfaces them automatically.
"""

from __future__ import annotations

from bernstein.core.observability.prometheus import Counter, registry

#: Counter incremented once per dispatched attempt.  ``outcome`` is the
#: terminal status of the attempt and ``classifier`` is the failure
#: bucket that drove the routing decision.
autofix_attempts_total: Counter = Counter(
    "autofix_attempts_total",
    "Autofix attempts dispatched per repo, outcome, and classifier.",
    labelnames=["repo", "outcome", "classifier"],
    registry=registry,
)

#: Counter incremented by the USD spend of each successful attempt.
#: ``cost_capped`` attempts also increment this with the spend
#: incurred before the cap fired.
autofix_cost_usd_total: Counter = Counter(
    "autofix_cost_usd_total",
    "Cumulative USD spend by the autofix daemon, per repo.",
    labelnames=["repo"],
    registry=registry,
)


__all__ = [
    "autofix_attempts_total",
    "autofix_cost_usd_total",
]
