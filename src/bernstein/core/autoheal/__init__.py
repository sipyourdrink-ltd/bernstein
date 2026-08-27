"""Auto-heal v2 subpackage.

Modules
-------
``categorizer``
    Classify failing CI job names into safe / heuristic / risky / unknown.
    Supersedes ``scripts/auto_heal_categorize.py`` from v1.

``bandit``
    Multi-arm-bandit strategy selection (Thompson sampling). Persists
    successes / failures per repair strategy in
    ``.sdd/autoheal-bandit.json``.

``bayesian``
    Per-class Bayesian confidence: maintains a Beta prior per CI class
    name and updates it from observed heal outcomes.


``shadow_mode``
    Quarantines new repair strategies until they accumulate evidence.
    First 5 invocations log results without pushing; promotion requires
    >= 4/5 successes.

``audit_log``
    Append-only JSONL ledger at ``.sdd/autoheal-history.jsonl`` of every
    heal attempt (or shadow run). Operator-readable, sortable.

``kill_switch``
    File-based emergency disable: workflow first-thing checks
    ``.sdd/autoheal-disabled`` for an unexpired flag and bails if set.

``lineage_writer``
    Writes one ``ChildBody`` per heal action under
    ``.sdd/lineage/v2/children/`` so operators can replay heal history.

``idempotency``
    Content-hashed dedupe key over the proposed patch; prevents
    re-attempting identical patches within a 24h window.

``cordon``
    Cordon-zone enforcement: pre-commit hook that aborts if any staged
    file is outside the allowlist.

Public surface
--------------
This package is consumed by:

* ``scripts/auto_heal_v2_run.py`` (the workflow entry point)
* ``.github/workflows/auto-heal.yml``
* Unit tests under ``tests/unit/autoheal/``
"""

from __future__ import annotations

__all__: list[str] = [
    "AUTOHEAL_VERSION",
]

AUTOHEAL_VERSION = "2.0.0"
