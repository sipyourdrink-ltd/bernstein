"""Tip + fork analysis over a lineage log (ADR-009 §6).

A *tip* is an entry that no other entry uses as a parent. For a healthy
artefact there is exactly one open tip; siblings sharing a parent with
different content_hash are *forks* and must be resolved by a merge entry
(`parent_hashes` of length >= 2) before the lineage gate will pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from bernstein.core.lineage.entry import LineageEntry, entry_hash

if TYPE_CHECKING:
    from collections.abc import Iterable


class TipSet(TypedDict):
    """`{"open": [...], "merged": [...]}` per artefact path.

    `open`   - entries with no descendant.
    `merged` - entries that were the parent of a merge entry (resolved forks).
    """

    open: list[str]
    merged: list[str]


@dataclass(frozen=True, slots=True)
class Fork:
    """A point where two-or-more siblings share a parent with distinct content."""

    artefact_path: str
    parent_hash: str
    child_hashes: tuple[str, ...]


def _group_by_path(entries: Iterable[LineageEntry]) -> dict[str, list[LineageEntry]]:
    out: dict[str, list[LineageEntry]] = {}
    for e in entries:
        out.setdefault(e.artefact_path, []).append(e)
    return out


def compute_tips(entries: Iterable[LineageEntry]) -> dict[str, TipSet]:
    """Return open + merged tips per artefact path.

    Algorithm: an entry is a tip if no other entry in the same artefact lists
    it as a parent. An entry is "merged" if it appears in `parent_hashes` of
    a merge entry (an entry with >= 2 parents).
    """
    by_path = _group_by_path(entries)
    result: dict[str, TipSet] = {}
    for path, group in by_path.items():
        hashes: list[str] = [entry_hash(e) for e in group]
        referenced_as_parent: set[str] = set()
        merged_parents: set[str] = set()
        for e in group:
            referenced_as_parent.update(e.parent_hashes)
            if len(e.parent_hashes) >= 2:
                merged_parents.update(e.parent_hashes)
        open_tips = [h for h in hashes if h not in referenced_as_parent]
        merged_hashes = [h for h in hashes if h in merged_parents]
        result[path] = TipSet(open=open_tips, merged=merged_hashes)
    return result


def detect_forks(entries: Iterable[LineageEntry]) -> list[Fork]:
    """Return all unresolved forks across the log.

    A fork (per ADR-009 §6.1) is: 2+ entries share the SAME single parent_hash
    on the same artefact_path and have DIFFERENT content_hash.
    Idempotent re-records (same parent + same content) do NOT count.
    """
    by_path = _group_by_path(entries)
    forks: list[Fork] = []
    for path, group in by_path.items():
        # Group children by their single parent (skip merge entries and genesis).
        by_parent: dict[str, list[LineageEntry]] = {}
        for e in group:
            if len(e.parent_hashes) != 1:
                continue
            by_parent.setdefault(e.parent_hashes[0], []).append(e)
        for parent_hash, children in by_parent.items():
            # Two distinct content hashes IS the fork condition, and it already implies two
            # children: a single child contributes one hash, so the set can never reach two
            # without them. The separate `len(children) < 2` guard that used to sit here was
            # therefore unreachable as a decision -- mutating it to `< 1` changed no behaviour,
            # which is exactly how the mutation gate reported it: a survivor that could not be
            # killed, because there was no observable difference to write a test against.
            distinct_content = {c.content_hash for c in children}
            if len(distinct_content) < 2:
                continue
            child_hashes = tuple(entry_hash(c) for c in children)
            forks.append(Fork(artefact_path=path, parent_hash=parent_hash, child_hashes=child_hashes))
    return forks


__all__ = ["Fork", "TipSet", "compute_tips", "detect_forks"]
