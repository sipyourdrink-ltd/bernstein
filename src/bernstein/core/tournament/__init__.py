"""Tournament runs: parallel attempts selected by deterministic evaluators (#2353).

For a hard task a single attempt often lands mediocre, and operators compensate
by re-running the task by hand and eyeballing diffs. A *tournament* fans out
``attempts`` independent siblings of one task in isolated worktrees and selects
the winner by scripted evaluators -- test pass rate, lint status, coverage
delta, mutation score, arbitrary commands -- with no model call in the decision
path.

The artefact the operator consumes *is* the proof of why one attempt won: a
signed, spine-anchored :class:`~bernstein.core.tournament.receipt.TournamentReceipt`
that names every attempt hash, every evaluator output, every score, the winner,
and the lineage edges (one ``chosen``, the rest ``sibling``). Selection is a
pure function of the evaluator outputs with a stable attempt-hash tie-break, so
replaying the run reproduces the identical decision and a tampered score or a
hand-picked winner diverges from the replay and fails ``verify``. Fan-out is
gated on the task's existing budget ceiling, so it never silently multiplies
cost.

Public surface lives in the sibling modules:

* :mod:`bernstein.core.tournament.spec` -- the declarative task-spec schema.
* :mod:`bernstein.core.tournament.evaluators` -- scripted evaluator outputs.
* :mod:`bernstein.core.tournament.scorer` -- the deterministic scorer + selection.
* :mod:`bernstein.core.tournament.receipt` -- the signed, spine-anchored receipt.
* :mod:`bernstein.core.tournament.runner` -- the budget-gated fan-out runner.
"""

from __future__ import annotations
