"""Data sensitivity with lineage-propagated classification (issue #5042).

:mod:`bernstein.core.lineage.provenance` propagates *trust* inward: the
effective trust of an artefact is the **minimum** ``trust_class`` over its
lineage closure, fail-closed to the lowest class. This module is that file
mirrored on the opposite axis, with the propagation rule inverted.

An operator classifies a source; the classification is recorded inside the
signed, HMAC-chained lineage entry as ``sensitivity``. The *effective*
sensitivity of any artefact is the **maximum** sensitivity class over its
lineage closure -- a deterministic projection of the signed graph, recomputable
offline by any verifier holding the log. An agent that reads a confidential
document and writes a summary produces an artefact whose closure still reaches
the classified source, so the summary is confidential too.

Sensitivity classes, from least to most sensitive:

    public < internal < confidential < restricted

Absence fails closed to the **highest** class, not the lowest: an unlabelled
artefact of unknown origin is not assumed harmless. That is the same posture as
the taint projection, taken at the opposite end of the order.

Two properties fall out of anchoring the class on the graph rather than on a
label attached to a file:

* **It cannot be dropped by copying.** Re-saving, summarising or transforming
  an artefact produces a new lineage entry naming the old one as a parent, so
  the closure still reaches the classified source. Stripping the classification
  means breaking that parent edge, which fails the signature, HMAC and
  anchoring checks the lineage gate already enforces.
* **The verdict explains itself.** It names the closure member that raised the
  level and the path through the graph that reaches it, so a classification is
  a walk an auditor can follow rather than a claim they have to accept.

Strip the lineage graph and this collapses entirely -- exactly as
:mod:`~bernstein.core.lineage.provenance` says of the trust direction.

This module is the projection only. Enforcement at a read boundary, the
operator-controlled source map and the reporting CLI are separate surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from bernstein.core.lineage.entry import LineageEntry, entry_hash
from bernstein.core.lineage.provenance import resolve_artefact_tip

if TYPE_CHECKING:
    from collections.abc import Sequence


class SensitivityClass(StrEnum):
    """Operator data classification of a source / artefact."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


# Higher rank == more sensitive. The ordering is total and fixed; the
# sensitivity projection takes the maximum rank over the closure.
_SENSITIVITY_RANK: dict[SensitivityClass, int] = {
    SensitivityClass.PUBLIC: 0,
    SensitivityClass.INTERNAL: 1,
    SensitivityClass.CONFIDENTIAL: 2,
    SensitivityClass.RESTRICTED: 3,
}

#: The highest sensitivity class. Used as the fail-closed default when no
#: classification is reachable (no record means most sensitive).
HIGHEST_SENSITIVITY_CLASS: SensitivityClass = SensitivityClass.RESTRICTED


def sensitivity_rank(sc: SensitivityClass) -> int:
    """Return the total-order rank of *sc* (higher == more sensitive)."""
    return _SENSITIVITY_RANK[sc]


def max_sensitivity_class(a: SensitivityClass, b: SensitivityClass) -> SensitivityClass:
    """Return the more sensitive of *a* and *b* (maximum rank wins)."""
    return a if _SENSITIVITY_RANK[a] >= _SENSITIVITY_RANK[b] else b


# ---------------------------------------------------------------------------
# Deterministic sensitivity projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SensitivityVerdict:
    """Result of projecting sensitivity over an artefact's lineage closure.

    Every field is a pure function of the log, so two independent verifiers
    holding the same log compute an identical verdict with no live process.

    Attributes:
        target: The entry hash whose sensitivity was computed.
        sensitivity: Effective sensitivity class (maximum over the closure).
            ``restricted`` when no classification is reachable (fail closed).
        resolved: False when ``target`` is not present in the log at all.
        closure: Sorted tuple of every entry hash reachable from ``target``
            (including ``target``) over ``parent_hashes`` edges.
        sensitivity_records: Sorted tuple of ``(entry_hash, sensitivity)`` for
            every classified entry found in the closure -- the signed labels
            the verdict projected from.
        raised_by: Entry hash of the nearest closure member carrying the
            effective class, or ``None`` when the class came from the
            fail-closed default rather than from a recorded label.
        path: The walk from ``target`` to ``raised_by`` over ``parent_hashes``
            edges, ``target`` first. Empty when ``raised_by`` is ``None``. This
            is the answer to "why is this classified", not just "it is".
    """

    target: str
    sensitivity: SensitivityClass
    resolved: bool
    closure: tuple[str, ...]
    sensitivity_records: tuple[tuple[str, str], ...]
    raised_by: str | None
    path: tuple[str, ...]


def _index_by_hash(entries: Sequence[LineageEntry]) -> dict[str, LineageEntry]:
    return {entry_hash(e): e for e in entries}


def _unresolved(target: str) -> SensitivityVerdict:
    """Fail-closed verdict for a target that is not in the log."""
    return SensitivityVerdict(
        target=target,
        sensitivity=HIGHEST_SENSITIVITY_CLASS,
        resolved=False,
        closure=(),
        sensitivity_records=(),
        raised_by=None,
        path=(),
    )


def effective_sensitivity(target: str, entries: Sequence[LineageEntry]) -> SensitivityVerdict:
    """Project the maximum sensitivity class over *target*'s lineage closure.

    Args:
        target: Entry hash (``sha256:...``) to evaluate.
        entries: Every lineage entry available (order-independent).

    Returns:
        A :class:`SensitivityVerdict`. When ``target`` is absent from
        ``entries`` the verdict is fail-closed (``restricted``,
        ``resolved=False``).
    """
    index = _index_by_hash(entries)
    if target not in index:
        return _unresolved(target)

    # Breadth-first walk of parent_hashes, each level visited in sorted order
    # so both the discovery order and the recorded predecessor of every member
    # are functions of the graph alone. A cycle is impossible in a valid
    # content-addressed log (a parent's hash is fixed before a child can name
    # it), but ``seen`` guards against a malformed input regardless.
    seen: set[str] = set()
    depth: dict[str, int] = {target: 0}
    came_from: dict[str, str] = {}
    frontier: list[str] = [target]
    records: list[tuple[str, str]] = []
    while frontier:
        next_frontier: list[str] = []
        for h in sorted(frontier):
            if h in seen:
                continue
            seen.add(h)
            entry = index.get(h)
            if entry is None:
                # A dangling parent is caught by the gate; here we simply stop
                # walking that branch (its contribution cannot be attested).
                continue
            if entry.sensitivity is not None:
                records.append((h, entry.sensitivity))
            for parent in entry.parent_hashes:
                if parent not in seen and parent not in came_from:
                    came_from[parent] = h
                    depth[parent] = depth[h] + 1
                next_frontier.append(parent)
        frontier = next_frontier

    if not records:
        # No classification anywhere in the closure -> fail closed to highest.
        return SensitivityVerdict(
            target=target,
            sensitivity=HIGHEST_SENSITIVITY_CLASS,
            resolved=True,
            closure=tuple(sorted(seen)),
            sensitivity_records=(),
            raised_by=None,
            path=(),
        )

    effective = SensitivityClass.PUBLIC
    for _, sc in records:
        effective = max_sensitivity_class(effective, SensitivityClass(sc))

    # Blame the *nearest* member carrying the effective class, so the reported
    # explanation is the shortest one; ties on depth break on the entry hash so
    # the choice stays a pure function of the graph.
    raised_by = min(
        (h for h, sc in records if SensitivityClass(sc) is effective),
        key=lambda h: (depth.get(h, len(seen)), h),
    )

    walk: list[str] = [raised_by]
    cursor = raised_by
    while cursor != target:
        cursor = came_from[cursor]
        walk.append(cursor)
    walk.reverse()

    return SensitivityVerdict(
        target=target,
        sensitivity=effective,
        resolved=True,
        closure=tuple(sorted(seen)),
        sensitivity_records=tuple(sorted(records)),
        raised_by=raised_by,
        path=tuple(walk),
    )


def sensitivity_for_artefact(artefact_path: str, entries: Sequence[LineageEntry]) -> SensitivityVerdict:
    """Compute the sensitivity verdict for the current tip of *artefact_path*.

    An unknown path is fail-closed (``restricted``, ``resolved=False``).
    """
    tip = resolve_artefact_tip(artefact_path, entries)
    if tip is None:
        return _unresolved(artefact_path)
    return effective_sensitivity(tip, entries)


__all__ = [
    "HIGHEST_SENSITIVITY_CLASS",
    "SensitivityClass",
    "SensitivityVerdict",
    "effective_sensitivity",
    "max_sensitivity_class",
    "sensitivity_for_artefact",
    "sensitivity_rank",
]
