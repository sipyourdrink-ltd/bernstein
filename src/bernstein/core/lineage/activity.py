"""Active-set closure over a lineage receipt ledger (issue #4183).

A *receipt* is a lineage entry.  Given an append-only ledger of receipts
(each recording its direct dependencies as content-addressed references
in ``parent_hashes``) and a set of invalidation seeds, a receipt is
**active** iff no dependency path reaches it from any seed.  The ledger is
never mutated; activity is recomputed, not stored.

The dependency references already exist on :class:`LineageEntry` as the
content-addressed ``parent_hashes`` list -- this module adds no new field.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.lineage.entry import LineageEntry, entry_hash

if TYPE_CHECKING:
    from collections.abc import Sequence


def active_set(entries: Sequence[LineageEntry], seeds: frozenset[str]) -> frozenset[str]:
    """Return the entry hashes still active under ``seeds``.

    A receipt is inactive iff a dependency path reaches it from a seed
    (itself included -- a seeded entry is invalidated), or from an entry
    that is itself inactive.  Invalidation is transitive: seed a premise
    and its dependents, their dependents, and so on all drop out.

    **Unknown references are treated conservatively as inactive.**  An
    entry whose ``parent_hashes`` names an id absent from the ledger is
    not considered active, because its premise cannot be shown to exist
    (missing premise = not currently provable).  This matches the
    fail-closed posture already established across the lineage package:
    provenance projection degrades absent provenance to the lowest trust
    class (see :func:`bernstein.core.lineage.provenance.effective_trust`),
    and the gate verdict work (issue #4181) treats absent evidence as
    ``inconclusive``, never as a pass.  An active set that silently counts
    unverifiable premises would let the parent enforcement wiring treat an
    unprovable receipt as valid; failing closed costs a manual re-check,
    failing open costs trust.

    Determinism: the result is a pure function of ``(entries, seeds)`` --
    no wall clock, no local state, and permutation of the input sequence
    does not change the result.  Hostile input (reference cycles, unknown
    references, empty inputs) terminates with defined behaviour; cycles
    cannot arise in a valid content-addressed log but are guarded anyway.

    Args:
        entries: Every ledger entry available (order-independent).
        seeds: Content-addressed ids whose invalidation propagates to all
            transitively dependent receipts.  A seed need not be present in
            ``entries`` -- a revoked receipt absent from this snapshot
            still invalidates every entry that references it.

    Returns:
        Entry hashes (``sha256:...``) of receipts with no dependency path
        to any seed.  Deterministic and order-independent.
    """
    by_hash = {entry_hash(e): e for e in entries}

    # Reverse adjacency: parent id -> child entry hashes that reference it.
    # Built for every referenced id, including ones absent from the ledger,
    # so an unknown parent (or an absent seed) still invalidates its
    # dependents instead of raising KeyError.
    children_of: dict[str, set[str]] = {}
    for e in entries:
        eh = entry_hash(e)
        for p in e.parent_hashes:
            children_of.setdefault(p, set()).add(eh)

    # Invalidation frontier: seeds themselves plus every entry that
    # references an id absent from the ledger (conservative inactive).
    inactive: set[str] = set()
    frontier = set(seeds)
    for e in entries:
        eh = entry_hash(e)
        if any(p not in by_hash for p in e.parent_hashes):
            frontier.add(eh)

    # Propagate invalidation along the reverse dependency graph.  ``seen``
    # guards against hostile cycles so the walk always terminates.
    seen: set[str] = set()
    while frontier:
        h = frontier.pop()
        if h in seen:
            continue
        seen.add(h)
        inactive.add(h)
        frontier.update(children_of.get(h, ()))

    return frozenset(eh for eh in by_hash if eh not in inactive)


__all__ = ["active_set"]
