"""Grant sweep: verify that the revoked set is absent from the current state.

This module runs on every reconcile execution and checks that the revoked grant
set, derived from the grant chain via compute_grant_sets(), truly contains no
grants that are still active/present.  A revoked grant found present is a
measured, failed audit finding, journaled with the decision record that
authorized the reconcile run.

The core function `sweep_grants()` returns a finding dict when a revoked grant
is detected in the active state, or ``None`` when the sweep passes cleanly.
"""

from __future__ import annotations

import time
from typing import Any

from bernstein.core.identity.grants import GrantChainResult, compute_grant_sets


def sweep_grants(
    result: GrantChainResult,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Reconcile-time grant sweep.

    Derives the revoked and approved sets from ``result`` and checks whether
    any grant that is marked revoked is still present in the active grant
    surface.  This prevents the revoked set from drifting from the chain
    record it is supposed to enforce.

    Args:
        result: A verified ``GrantChainResult`` from a reconcile run.
        now: Epoch seconds for expiry checking; defaults to ``time.time()``.

    Returns:
        A finding dict with ``severity`` = ``"critical"``, ``category`` =
        ``"grant-sweep"``, and a ``summary`` describing the violation when a
        revoked grant is found present.  Returns ``None`` when the sweep passes
        (no revoked-grants-present condition).
    """
    if not result.valid:
        # Cannot check a chain that did not verify; skip rather than fail.
        return None

    revoked, approved = compute_grant_sets(result, now=now)

    # If any approved key also appears in the revoked set, that means a grant
    # that was revoked is still present in the active set.  This is the exact
    # drift the sweep is designed to catch.
    violations = revoked & approved

    if not violations:
        return None

    # Build a human-readable summary of the violations.
    # Each violation is a (task_id, secret_name) tuple.
    violation_summaries: list[str] = []
    for task_id, secret_name in sorted(violations):
        violation_summaries.append(f"task_id={task_id}, secret_name={secret_name}")

    summary = (
        f"Revoked grant(s) found present in the active set: "
        f"{', '.join(violation_summaries)}. "
        "The revoked set must exactly match the chain state; "
        "a revoked grant appearing in the approved set is a governance "
        "drift that must be resolved."
    )

    return {
        "severity": "critical",
        "category": "grant-sweep",
        "summary": summary,
        "failure_scenario": (
            "A grant was revoked in the chain but is still counted in the "
            "approved set, meaning the revocation has not been properly "
            "enforced on reconcile runs."
        ),
        "direction": "certifies-falsely",
        "baseline": "regression",
        "timestamp": int(time.time()),
    }
