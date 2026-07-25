"""Artifact trigger source - fire on the fleet's own outputs (issue #2559).

Every existing trigger source watches something *outside* the fleet: a forge, a
chat workspace, a clock, a directory. Nothing watched what the fleet itself
produced, so a goal that depends on another goal's output had to poll on a cron
guess -- wasting a run when nothing changed, lagging a day when the upstream was
late -- or be hand-wired as a run dependency.

This source normalises ``artifact.produced`` events into the same
:class:`~bernstein.core.tasks.models.TriggerEvent` every other source emits, so
the existing rule matching in ``trigger_manager`` handles artifact rules with no
new matching engine.

Why the fire set is replayable
------------------------------

The intended fire set is computed by :func:`intended_fires`, a **pure function**
of ``(spine entries, patterns)``. Nothing about it depends on when the
orchestrator happened to be listening. So:

* replaying a run's spine reproduces the exact set of firings that run should
  have performed, in order;
* comparing that against the journaled events (:func:`fire_divergences`) turns a
  dropped or duplicated firing into a named ``entry_hash`` instead of an
  investigation;
* an event whose spine entry no longer verifies arrives with ``verified: false``
  and is refused by default, so a tampered chain cannot be used to *cause* work.

That last point is the reason this source exists in the lineage-facing layer
rather than being a thin bus subscriber: a firing is a decision, and a decision
taken off an unverified record is worse than a firing that never happened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.artifact_events import (
    ArtifactProductionEvent,
    compare_fanout,
    load_production_events,
    project_production_events,
    replay_production_events,
)
from bernstein.core.lineage.artifact_uri import ArtifactURIError, match_artifact_pattern
from bernstein.core.tasks.models import TriggerEvent

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from bernstein.core.lineage.artifact_events import FanoutDivergence
    from bernstein.core.lineage.spine import SpineEntry

__all__ = [
    "ARTIFACT_TRIGGER_SOURCE",
    "ArtifactSource",
    "fire_divergences",
    "intended_fires",
    "matches_any",
    "normalize_production_event",
]

#: ``TriggerEvent.source`` discriminator for artifact production.
ARTIFACT_TRIGGER_SOURCE = "artifact"


def matches_any(patterns: Sequence[str], uri: str) -> bool:
    """Whether ``uri`` is covered by any of ``patterns``.

    Uses the canonical artifact-key matcher, so ``pkg://pypi/bernstein/*``
    covers every version of one package and a malformed pattern simply matches
    nothing instead of raising into the orchestrator tick.
    """
    for pattern in patterns:
        try:
            if match_artifact_pattern(pattern, uri):
                return True
        except ArtifactURIError:
            continue
    return False


def normalize_production_event(event: ArtifactProductionEvent) -> TriggerEvent:
    """Convert one production event into a normalized :class:`TriggerEvent`.

    The artifact key rides in ``changed_files`` so the existing path-shaped rule
    filters in ``trigger_manager`` see it without a new matching engine, and the
    full projected payload rides in ``raw_payload`` so a rule that wants the
    provenance -- producing identity, model, entry hash, verification verdict --
    has it without a second lookup.
    """
    return TriggerEvent(
        source=ARTIFACT_TRIGGER_SOURCE,
        timestamp=float(event.timestamp),
        raw_payload=dict(event.to_payload()),
        sender=event.actor,
        changed_files=(event.uri,),
        message=f"artifact produced: {event.uri}",
        metadata={
            "uri": event.uri,
            "entry_hash": event.entry_hash,
            "content_hash": event.content_hash,
            "run_id": event.run_id,
            "model": event.model,
            "verified": event.verified,
        },
    )


def intended_fires(
    entries: Iterable[SpineEntry],
    *,
    run_id: str,
    hmac_key: bytes,
    patterns: Sequence[str] = (),
    require_verified: bool = True,
) -> list[ArtifactProductionEvent]:
    """Return the events a run's spine says should have fired, in order. Pure.

    Args:
        entries: The run's spine entries, in append order.
        run_id: The run the entries belong to.
        hmac_key: Audit-chain key, used to recompute each entry's integrity.
        patterns: Artifact key patterns to fire on. Empty means every produced
            artifact.
        require_verified: Drop entries whose integrity verdict is false. On by
            default: a firing caused by a tampered record is worse than a
            firing that never happened.

    Returns:
        The intended fire set. Two kinds of entry are excluded: the run's own
        journal seal -- it is the run recording itself, not an output anything
        should react to -- and artifact attempt records, which say a declared
        output did *not* land. A downstream goal fires when its upstream lands;
        firing it on the record of a failure to land would invert the meaning of
        the event (issue #2559).
    """
    events = project_production_events(entries, run_id=run_id, hmac_key=hmac_key)
    out: list[ArtifactProductionEvent] = []
    for event in events:
        if event.is_journal_seal or event.is_attempt:
            continue
        if require_verified and not event.verified:
            continue
        if patterns and not matches_any(patterns, event.uri):
            continue
        out.append(event)
    return out


def fire_divergences(
    lineage_root: Path,
    *,
    run_id: str,
    hmac_key: bytes,
) -> list[FanoutDivergence]:
    """Compare a run's journaled fan-out against the one its spine implies.

    An intact run yields an empty list. A dropped, duplicated or altered firing
    is reported as a divergence naming the offending spine ``entry_hash``, which
    is the difference between "a trigger did not fire" as a mystery and as a
    lookup.
    """
    journaled = load_production_events(lineage_root, run_id=run_id)
    replayed = replay_production_events(lineage_root, run_id=run_id, hmac_key=hmac_key)
    return compare_fanout(journaled, replayed)


class ArtifactSource:
    """Trigger source adapter for artifact production events.

    Stateless: the orchestrator hands it a raw event payload (from the SSE bus
    or read back from the journal) and gets a normalized
    :class:`TriggerEvent`. Any state a consumer needs is in the spine.
    """

    def normalize(self, raw_event: dict[str, Any]) -> TriggerEvent:
        """Normalize a raw ``artifact.produced`` payload.

        Raises:
            ValueError: When the payload is not a well-formed production event.
                A malformed event is refused rather than fired on a guess.
        """
        return normalize_production_event(ArtifactProductionEvent.from_payload(raw_event))
