"""Declared-vs-produced output diff sealed at task completion (issue #2559).

``Task.evidence_producers`` declares how a task's work is *verified*.
``Task.declared_outputs`` declares what it is supposed to *leave behind*. This
module is the projection that compares the two sides at completion and turns
the comparison into a signed fact.

Three buckets, each answering a question the chain could not answer before:

``declared_and_produced``
    The intent was honoured. The produced key, canonical, ready to be looked up
    in the spine.

``declared_but_missing``
    A task declared an output and did not produce it. Without this bucket, a
    task that fails before producing its output is indistinguishable from a
    task that was never scheduled: both leave no artifact-keyed record at all.

``produced_but_undeclared``
    A write nobody declared. This is the classic symptom of an agent drifting
    off its brief, and today it is caught only when a human reviewer happens to
    notice it in the diff. Reviewer attention does not scale with fleet size; a
    signed finding does.

Determinism
-----------

The diff is a pure function of two string sequences. Both sides are
canonicalised, every bucket is sorted, and duplicates collapse -- so the same
declared set and the same produced set give a byte-identical result regardless
of declaration order, iteration order, or which host computed it. That is what
lets the diff be sealed into the evidence bundle's *signed* binding: the
verifier recomputes the bytes and compares.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.artifact_uri import (
    canonical_artifact_key,
    canonical_artifact_pattern,
    match_artifact_pattern,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["OutputDiff", "compute_output_diff"]


@dataclass(frozen=True, slots=True)
class OutputDiff:
    """The three-way declared-vs-produced comparison for one task.

    Every field is sorted and deduplicated, so the dataclass is its own
    canonical form and :meth:`to_dict` is stable across hosts and runs.
    """

    declared_and_produced: tuple[str, ...] = ()
    declared_but_missing: tuple[str, ...] = ()
    produced_but_undeclared: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Whether the diff carries nothing worth sealing.

        An empty diff is dropped from the evidence bundle's binding entirely so
        that a bundle for a task with no declared outputs canonicalises
        byte-for-byte identically to a pre-#2559 bundle, and every existing
        signature and spine anchor stays valid.
        """
        return not (self.declared_and_produced or self.declared_but_missing or self.produced_but_undeclared)

    @property
    def has_findings(self) -> bool:
        """Whether the diff carries something a reviewer or policy should act on."""
        return bool(self.declared_but_missing or self.produced_but_undeclared)

    def to_dict(self) -> dict[str, Any]:
        return {
            "declared_and_produced": list(self.declared_and_produced),
            "declared_but_missing": list(self.declared_but_missing),
            "produced_but_undeclared": list(self.produced_but_undeclared),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> OutputDiff:
        return cls(
            declared_and_produced=tuple(str(x) for x in row.get("declared_and_produced", ())),
            declared_but_missing=tuple(str(x) for x in row.get("declared_but_missing", ())),
            produced_but_undeclared=tuple(str(x) for x in row.get("produced_but_undeclared", ())),
        )


def compute_output_diff(declared: Sequence[str], produced: Sequence[str]) -> OutputDiff:
    """Compare declared output patterns against the artifact keys produced.

    Args:
        declared: Declared output keys or patterns, as carried by
            ``Task.declared_outputs``. Canonicalised here as well as at task
            construction so a caller passing raw operator input gets the same
            answer as one passing a normalised task.
        produced: Concrete artifact keys the task actually produced.

    Returns:
        The sorted, deduplicated :class:`OutputDiff`.

    Raises:
        bernstein.core.lineage.artifact_uri.ArtifactURIError: When either side
            contains something that is not a valid artifact key or pattern.
            Callers on the completion path are fail-open and swallow this; a
            malformed declaration must never fail a task that already
            completed.
    """
    declared_patterns = sorted({canonical_artifact_pattern(d) for d in declared})
    produced_keys = sorted({canonical_artifact_key(p) for p in produced})

    matched_patterns: set[str] = set()
    declared_and_produced: list[str] = []
    produced_but_undeclared: list[str] = []

    for key in produced_keys:
        covering = [p for p in declared_patterns if match_artifact_pattern(p, key)]
        if covering:
            matched_patterns.update(covering)
            declared_and_produced.append(key)
        else:
            produced_but_undeclared.append(key)

    declared_but_missing = [p for p in declared_patterns if p not in matched_patterns]

    return OutputDiff(
        declared_and_produced=tuple(declared_and_produced),
        declared_but_missing=tuple(declared_but_missing),
        produced_but_undeclared=tuple(produced_but_undeclared),
    )
