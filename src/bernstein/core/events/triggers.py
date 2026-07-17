"""Composable trigger semantics as deterministic folds over the feed (#2548).

Every evaluator here is a *pure fold* over a projected window (a list of
:class:`bernstein.core.events.grammar.CanonicalEvent`). No wall clock, no shared
mutable state, no LLM in the loop: given the same slice, every threshold,
absence, sequence, and compound fire decision is re-derived identically. That is
what lets a fire decision be replayed from the chain slice alone.

Semantics
---------
* Threshold - N matching events within a window (a span of chain positions),
  with ``for_each`` bucketing on grammar labels (resource, actor, or label).
* Absence - after event A, expect B within N positions, else a violation whose
  bounds are two named chain positions (the negative-proof inputs).
* Sequence - ordered pairs keyed on *lineage descent* through the related-
  resource edges, not wall-clock order: the later event must provably descend
  from the earlier one.
* Compound - any / all / count over a set of label conditions.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from bernstein.core.events.grammar import CanonicalEvent

#: Event attributes usable as ``for_each`` bucket dimensions. Restricted to
#: grammar labels and identifiers so bucketing stays a pure function of the
#: projected event, never of the (undisclosed) payload.
_BUCKET_DIMS = ("resource_id", "actor", "label")


def _matches(label: str, pattern: str) -> bool:
    """Return whether ``label`` matches the glob ``pattern`` (case-sensitive)."""
    return fnmatchcase(label, pattern)


def _bucket_key(event: CanonicalEvent, dims: Sequence[str]) -> tuple[str, ...]:
    """Return the bucket key for ``event`` over the requested dimensions."""
    key: list[str] = []
    for dim in dims:
        if dim not in _BUCKET_DIMS:
            raise ValueError(f"unsupported for_each dimension: {dim!r}; allowed: {_BUCKET_DIMS}")
        key.append(str(getattr(event, dim)))
    return tuple(key)


# ---------------------------------------------------------------------------
# Lineage descent
# ---------------------------------------------------------------------------


def _build_ancestor_index(events: Iterable[CanonicalEvent]) -> dict[str, set[str]]:
    """Return ``resource_id -> {immediate ancestor resource ids}`` over a window.

    Edges are aggregated across every event: an event's
    ``related_resource_ids`` are the immediate ancestors of its resource.
    """
    index: dict[str, set[str]] = {}
    for event in events:
        bucket = index.setdefault(event.resource_id, set())
        bucket.update(event.related_resource_ids)
    return index


def resource_descends(
    child_resource: str,
    ancestor_resource: str,
    ancestor_index: dict[str, set[str]],
) -> bool:
    """Return whether ``child_resource`` descends from ``ancestor_resource``.

    Descent is reachability along child -> parent edges in the lineage graph. A
    resource trivially descends from itself (the same lineage line). Two
    resources with no connecting path do not descend, however their events are
    ordered in wall-clock time.
    """
    if child_resource == ancestor_resource:
        return True
    seen: set[str] = set()
    frontier: deque[str] = deque([child_resource])
    while frontier:
        node = frontier.popleft()
        if node in seen:
            continue
        seen.add(node)
        for parent in ancestor_index.get(node, ()):
            if parent == ancestor_resource:
                return True
            if parent not in seen:
                frontier.append(parent)
    return False


# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    """N matching events within a window, optionally bucketed.

    Attributes:
        label: Glob matched against the canonical label.
        count: The count that trips the rule.
        window: Maximum span, in chain positions, over which the count
            accumulates (events ``e`` count while ``cur.position - e.position <
            window``).
        for_each: Bucket dimensions from ``resource_id`` / ``actor`` / ``label``;
            each bucket counts independently.
    """

    label: str
    count: int
    window: int
    for_each: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ThresholdFire:
    """A threshold crossing.

    Attributes:
        bucket: The bucket key that crossed (empty tuple when unbucketed).
        matched_hmacs: The HMACs of the ``count`` events that formed the
            crossing, in chain order.
        trigger_position: The position of the event that completed the crossing.
    """

    bucket: tuple[str, ...]
    matched_hmacs: tuple[str, ...]
    trigger_position: int


def evaluate_threshold(rule: ThresholdRule, events: Sequence[CanonicalEvent]) -> list[ThresholdFire]:
    """Fold ``events`` in order, emitting one fire per fresh threshold crossing.

    A crossing is emitted the moment a bucket's in-window matching count reaches
    ``rule.count``; it is not re-emitted while the count stays at or above the
    threshold, so a burst produces exactly one fire per bucket per crossing.
    """
    if rule.count <= 0:
        raise ValueError("threshold count must be positive")
    if rule.window <= 0:
        raise ValueError("threshold window must be positive")

    windows: dict[tuple[str, ...], deque[CanonicalEvent]] = {}
    at_or_above: set[tuple[str, ...]] = set()
    fires: list[ThresholdFire] = []

    for event in events:
        if not _matches(event.label, rule.label):
            continue
        key = _bucket_key(event, rule.for_each)
        bucket = windows.setdefault(key, deque())
        bucket.append(event)
        while bucket and event.position - bucket[0].position >= rule.window:
            bucket.popleft()

        if len(bucket) >= rule.count:
            if key not in at_or_above:
                at_or_above.add(key)
                matched = tuple(e.hmac for e in list(bucket)[-rule.count :])
                fires.append(ThresholdFire(bucket=key, matched_hmacs=matched, trigger_position=event.position))
        else:
            at_or_above.discard(key)

    return fires


# ---------------------------------------------------------------------------
# Absence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AbsenceExpectation:
    """After event A, expect event B within a window, else a violation.

    Attributes:
        after: Glob matched against the label of the anchoring event A.
        expect: Glob matched against the label of the expected event B.
        within: Window span, in chain positions, after A within which B must
            appear.
        require_descent: When true, B satisfies the expectation only if it
            descends from A (a semantic milestone of the same lineage line).
    """

    after: str
    expect: str
    within: int
    require_descent: bool = False


@dataclass(frozen=True, slots=True)
class AbsenceViolation:
    """A negative-proof: no matching B between two named chain positions.

    Attributes:
        after_hmac: HMAC of the anchoring event A - the lower named position.
        to_hmac: HMAC of the last event inside the observed window - the upper
            named position between which no B exists.
        expect: The expectation's ``expect`` glob, echoed for the receipt.
    """

    after_hmac: str
    to_hmac: str
    expect: str


def evaluate_absence(expectation: AbsenceExpectation, events: Sequence[CanonicalEvent]) -> list[AbsenceViolation]:
    """Emit a violation for every A whose expected B never arrived in-window.

    A violation is asserted only when the observed slice actually covers the
    window (the slice extends ``within`` positions past A, or ends), so the
    negative proof is bounded by two chain positions the verifier can check.
    """
    if expectation.within <= 0:
        raise ValueError("absence window must be positive")

    ancestor_index = _build_ancestor_index(events)
    violations: list[AbsenceViolation] = []

    for idx, anchor in enumerate(events):
        if not _matches(anchor.label, expectation.after):
            continue
        upper_position = anchor.position + expectation.within
        satisfied = False
        last_in_window = anchor
        for candidate in events[idx + 1 :]:
            if candidate.position > upper_position:
                break
            last_in_window = candidate
            if not _matches(candidate.label, expectation.expect):
                continue
            if expectation.require_descent and not resource_descends(
                candidate.resource_id, anchor.resource_id, ancestor_index
            ):
                continue
            satisfied = True
            break
        if not satisfied:
            violations.append(
                AbsenceViolation(after_hmac=anchor.hmac, to_hmac=last_in_window.hmac, expect=expectation.expect)
            )
    return violations


# ---------------------------------------------------------------------------
# Sequence (lineage descent)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SequenceRule:
    """An ordered pair keyed on lineage descent, not wall-clock order.

    Attributes:
        earlier: Glob for the earlier event A.
        later: Glob for the later event B.
    """

    earlier: str
    later: str


@dataclass(frozen=True, slots=True)
class SequenceFire:
    """A sequence match.

    Attributes:
        earlier_hmac: HMAC of the earlier event A.
        later_hmac: HMAC of the later event B, which descends from A.
    """

    earlier_hmac: str
    later_hmac: str


def evaluate_sequence(rule: SequenceRule, events: Sequence[CanonicalEvent]) -> list[SequenceFire]:
    """Emit one fire per later-event B that provably descends from an earlier A.

    For each B matching ``later``, the earliest A (by chain position) matching
    ``earlier`` from which B descends fires the rule. Two unrelated events in the
    right wall-clock order never fire, because no lineage path connects them.
    """
    ancestor_index = _build_ancestor_index(events)
    earlier_events = [e for e in events if _matches(e.label, rule.earlier)]
    fires: list[SequenceFire] = []

    for later in events:
        if not _matches(later.label, rule.later):
            continue
        for earlier in earlier_events:
            if earlier.position >= later.position:
                break
            if resource_descends(later.resource_id, earlier.resource_id, ancestor_index):
                fires.append(SequenceFire(earlier_hmac=earlier.hmac, later_hmac=later.hmac))
                break
    return fires


# ---------------------------------------------------------------------------
# Compound
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompoundRule:
    """Boolean composition over a set of label conditions.

    Attributes:
        mode: ``any`` (at least one), ``all`` (every one), or ``count`` (at
            least ``threshold`` conditions satisfied).
        conditions: Label globs, each satisfied by any matching event.
        threshold: Minimum satisfied conditions for ``count`` mode.
    """

    mode: Literal["any", "all", "count"]
    conditions: tuple[str, ...]
    threshold: int = 1


@dataclass(frozen=True, slots=True)
class CompoundFire:
    """A compound satisfaction.

    Attributes:
        satisfied_conditions: The conditions that matched, in declaration order.
        matched_hmacs: One representative event HMAC per satisfied condition (the
            first matching event), in declaration order.
    """

    satisfied_conditions: tuple[str, ...]
    matched_hmacs: tuple[str, ...]


def evaluate_compound(rule: CompoundRule, events: Sequence[CanonicalEvent]) -> CompoundFire | None:
    """Return a fire when the compound rule is satisfied, else ``None``."""
    if not rule.conditions:
        raise ValueError("compound rule needs at least one condition")

    satisfied: list[str] = []
    representatives: list[str] = []
    for condition in rule.conditions:
        first = next((e for e in events if _matches(e.label, condition)), None)
        if first is not None:
            satisfied.append(condition)
            representatives.append(first.hmac)

    n = len(satisfied)
    if rule.mode == "all":
        ok = n == len(rule.conditions)
    elif rule.mode == "any":
        ok = n >= 1
    else:  # count
        ok = n >= rule.threshold

    if not ok:
        return None
    return CompoundFire(satisfied_conditions=tuple(satisfied), matched_hmacs=tuple(representatives))


__all__ = [
    "AbsenceExpectation",
    "AbsenceViolation",
    "CompoundFire",
    "CompoundRule",
    "SequenceFire",
    "SequenceRule",
    "ThresholdFire",
    "ThresholdRule",
    "evaluate_absence",
    "evaluate_compound",
    "evaluate_sequence",
    "evaluate_threshold",
    "resource_descends",
]
