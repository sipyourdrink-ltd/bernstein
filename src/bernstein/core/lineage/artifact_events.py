"""Artifact production events, journaled beside the spine (issue #2559).

The spine records *that* an artifact write happened. Nothing downstream could
see it: no event said "artifact X was produced", so a goal that depends on
another goal's output either polled on a cron guess or was hand-wired as a run
dependency. This module closes that gap at the one place every in-process
artifact write already passes through --
:func:`bernstein.adapters.base.record_artifact_write` -- so there is no
per-adapter opt-in to forget.

The event is a projection, not a report
---------------------------------------

:func:`project_production_event` is a **pure function of one spine entry**.
Nothing else feeds it: no wall clock, no environment, no ambient state. That is
the whole design, and it buys three things at once.

* **Replayable fan-out.** Replaying a spine reproduces the identical intended
  event set, entry for entry, hash for hash. A firing that was dropped,
  duplicated or altered is a *detectable divergence naming the offending
  entry hash* (:func:`compare_fanout`), not a mystery to reconstruct from logs.
* **Tamper visibility.** The ``verified`` flag on a replayed event is the
  per-entry integrity verdict recomputed from the entry itself
  (:func:`bernstein.core.lineage.spine.verify_entry`). Flip one byte of a spine
  row -- payload or HMAC tag -- and the event for that artifact replays as
  ``verified: false`` while the row journaled at production time still says
  ``true``. The disagreement is the finding.
* **No trusted emitter.** A consumer never has to believe the journal. It
  re-derives the event set from the spine and compares; the journal is a
  convenience index, and the spine stays the only thing that has to be trusted.

Fail-open at the boundary
-------------------------

Recording into the spine is fail-closed -- provenance is a hard requirement.
Emitting the event is deliberately **fail-open**: a full disk, a read-only
journal or a dead subscriber must never turn a successful artifact write into a
failed one. The write is the fact; the event is the notification. See
:func:`emit_production_event`, which never raises, and the projection above,
which can rebuild anything a failed emit dropped.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import (
    ARTIFACT_ATTEMPT_STEP_PREFIX,
    JOURNAL_SEAL_STEP_PREFIX,
    LineageSpine,
    SpineEntry,
    verify_entry,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ARTIFACT_EVENTS_FILENAME",
    "ARTIFACT_EVENT_VERSION",
    "ARTIFACT_PRODUCED_EVENT",
    "ArtifactProductionEvent",
    "FanoutDivergence",
    "append_production_event",
    "compare_fanout",
    "emit_production_event",
    "load_production_events",
    "observed_artifact_keys",
    "project_production_event",
    "project_production_events",
    "replay_production_events",
]

#: Wire-format version stamped into every journaled event. ``load`` skips a row
#: whose version it does not know rather than guessing at its shape.
ARTIFACT_EVENT_VERSION = 1

#: Typed event name on the SSE bus and in the journal.
ARTIFACT_PRODUCED_EVENT = "artifact.produced"

#: Journal file, written beside ``spine.jsonl`` inside the same per-run
#: directory. Co-located on purpose: the events are meaningless without the
#: spine they project from, so the two travel together in any run archive.
ARTIFACT_EVENTS_FILENAME = "artifact-events.jsonl"

#: Divergence kinds reported by :func:`compare_fanout`.
DIVERGENCE_DROPPED = "dropped"
DIVERGENCE_DUPLICATED = "duplicated"
DIVERGENCE_UNEXPECTED = "unexpected"
DIVERGENCE_ALTERED = "altered"


@dataclass(frozen=True, slots=True)
class ArtifactProductionEvent:
    """One ``artifact.produced`` event.

    Every field except :attr:`verified` is copied verbatim from the spine entry
    it projects, so an event is comparable to a replayed one by value.
    :attr:`verified` is the per-entry integrity verdict *at the time the event
    was produced*: ``True`` at emission (the entry was just written and read
    back through the same key), recomputed on replay.
    """

    uri: str
    entry_hash: str
    content_hash: str
    actor: str
    model: str
    step_id: str
    run_id: str
    timestamp: int
    verified: bool
    v: int = ARTIFACT_EVENT_VERSION

    @property
    def is_journal_seal(self) -> bool:
        """Whether this event records the run's own journal seal.

        The seal is a spine entry like any other -- it is journaled and it
        replays -- but it is the run recording *itself*, not a deliverable the
        fleet produced. Consumers that answer "what did this run produce" filter
        it out; the conformance property (one event per spine entry) does not,
        because an exception at the boundary is exactly the opt-in gap this
        module exists to close.
        """
        return self.step_id.startswith(JOURNAL_SEAL_STEP_PREFIX)

    @property
    def is_attempt(self) -> bool:
        """Whether this event records a declared output that did *not* land.

        An attempt record is a spine entry like any other -- journaled, replayed,
        tamper-evident -- so the conformance property (one event per spine entry)
        holds over it and the fan-out stays exact. But it is the record of an
        absence, so every consumer answering "what was produced" filters it out,
        exactly as it filters the run's journal seal (issue #2559).
        """
        return self.step_id.startswith(ARTIFACT_ATTEMPT_STEP_PREFIX)

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical payload dict (the SSE ``data`` body)."""
        return {
            "actor": self.actor,
            "content_hash": self.content_hash,
            "entry_hash": self.entry_hash,
            "model": self.model,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "timestamp": self.timestamp,
            "uri": self.uri,
            "v": self.v,
            "verified": self.verified,
        }

    def canonical_bytes(self) -> bytes:
        """Return the canonical JSON bytes of the event.

        Sorted keys and minimal separators, so two hosts projecting the same
        spine entry serialise byte-identical rows.
        """
        return json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_payload(cls, row: dict[str, Any]) -> ArtifactProductionEvent:
        """Rebuild an event from a journaled row.

        Raises:
            ValueError: When a required field is missing or mistyped, or the
                row carries an unknown wire-format version.
        """
        version = int(row["v"])
        if version != ARTIFACT_EVENT_VERSION:
            msg = f"unsupported artifact event version {version!r}"
            raise ValueError(msg)
        return cls(
            uri=str(row["uri"]),
            entry_hash=str(row["entry_hash"]),
            content_hash=str(row["content_hash"]),
            actor=str(row["actor"]),
            model=str(row["model"]),
            step_id=str(row["step_id"]),
            run_id=str(row["run_id"]),
            timestamp=int(row["timestamp"]),
            verified=bool(row["verified"]),
            v=version,
        )


# ---------------------------------------------------------------------------
# Projection: spine entry -> event
# ---------------------------------------------------------------------------


def project_production_event(entry: SpineEntry, *, run_id: str, verified: bool) -> ArtifactProductionEvent:
    """Project one spine entry onto its production event. Pure.

    Args:
        entry: The spine entry that was appended.
        run_id: The run whose spine carries the entry.
        verified: The per-entry integrity verdict to stamp on the event.

    Returns:
        The event. Two callers holding the same entry and the same verdict
        derive byte-identical events.
    """
    return ArtifactProductionEvent(
        uri=entry.artifact_path,
        entry_hash=entry.entry_hash,
        content_hash=entry.content_hash,
        actor=entry.actor,
        model=entry.model,
        step_id=entry.step_id,
        run_id=run_id,
        timestamp=entry.timestamp,
        verified=verified,
    )


def project_production_events(
    entries: Iterable[SpineEntry],
    *,
    run_id: str,
    hmac_key: bytes,
) -> list[ArtifactProductionEvent]:
    """Project a whole spine onto its intended event set, in append order.

    The integrity verdict is recomputed per entry, so an entry whose payload or
    HMAC tag was altered projects with ``verified=False`` while its neighbours
    stay ``True``.
    """
    return [project_production_event(entry, run_id=run_id, verified=verify_entry(entry, hmac_key)) for entry in entries]


def replay_production_events(
    lineage_root: Path,
    *,
    run_id: str,
    hmac_key: bytes,
) -> list[ArtifactProductionEvent]:
    """Re-derive the intended event set for ``run_id`` from its spine alone.

    Reads no journal. This is the side of :func:`compare_fanout` that cannot be
    forged by writing rows into the event log.
    """
    spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=hmac_key)
    return project_production_events(spine.iter_entries(), run_id=run_id, hmac_key=hmac_key)


# ---------------------------------------------------------------------------
# Journal IO
# ---------------------------------------------------------------------------


def events_path(lineage_root: Path, run_id: str) -> Path:
    """Return the event-journal path for ``run_id``.

    Validated through :class:`LineageSpine` so a ``run_id`` that would escape
    its per-run directory is refused here exactly as it is for the spine.
    """
    return LineageSpine(lineage_root, run_id=run_id, hmac_key=b"").run_dir / ARTIFACT_EVENTS_FILENAME


def append_production_event(lineage_root: Path, *, run_id: str, event: ArtifactProductionEvent) -> None:
    """Append one event to the run's journal.

    Raises on IO failure; :func:`emit_production_event` is the fail-open wrapper
    the write boundary uses.
    """
    path = events_path(lineage_root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        fh.write(event.canonical_bytes() + b"\n")
        fh.flush()
        os.fsync(fh.fileno())


def load_production_events(lineage_root: Path, *, run_id: str) -> list[ArtifactProductionEvent]:
    """Return the journaled events for ``run_id`` in append order.

    A malformed or unknown-version row is skipped with a debug log rather than
    raising: the journal is an index, and a corrupt index must not stop a
    verifier from reaching the spine, which is the thing that actually decides.
    :func:`compare_fanout` reports the resulting gap as a divergence.
    """
    path = events_path(lineage_root, run_id)
    if not path.is_file():
        return []
    out: list[ArtifactProductionEvent] = []
    for line in path.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("artifact events: skipping malformed row in %s", path)
            continue
        if not isinstance(row, dict):
            logger.debug("artifact events: skipping non-object row in %s", path)
            continue
        try:
            out.append(ArtifactProductionEvent.from_payload(row))
        except (KeyError, TypeError, ValueError):
            logger.debug("artifact events: skipping row with bad shape in %s", path)
            continue
    return out


def emit_production_event(
    lineage_root: Path,
    *,
    run_id: str,
    entry: SpineEntry,
    publish: Any = None,
) -> ArtifactProductionEvent | None:
    """Journal (and optionally publish) the event for a just-written entry.

    **Never raises.** The spine write already succeeded when this runs; a
    failure to journal or to publish would otherwise turn a recorded artifact
    into a failed task, which inverts the priority between the two. The spine
    keeps the fact, and :func:`replay_production_events` rebuilds any event a
    failed emit dropped, so the fail-open path loses notification latency and
    nothing else.

    Args:
        lineage_root: ``.sdd/lineage`` root.
        run_id: The run whose spine carries ``entry``.
        entry: The entry just appended.
        publish: Optional one-argument callable handed the event for bus
            delivery. Its exceptions are swallowed on the same terms.

    Returns:
        The event, or ``None`` when projection itself failed.
    """
    try:
        event = project_production_event(entry, run_id=run_id, verified=True)
    except Exception as exc:  # pragma: no cover - defensive; projection is total
        logger.debug("artifact events: projection failed for run %s: %s", run_id, exc)
        return None
    try:
        append_production_event(lineage_root, run_id=run_id, event=event)
    except Exception as exc:
        logger.debug("artifact events: journaling failed for run %s: %s", run_id, exc)
    if publish is not None:
        try:
            publish(event)
        except Exception as exc:
            logger.debug("artifact events: publishing failed for run %s: %s", run_id, exc)
    return event


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


def observed_artifact_keys(lineage_root: Path, *, run_id: str) -> tuple[str, ...]:
    """Return the artifact keys a run is *observed* to have produced.

    Sorted and deduplicated. Two kinds of entry are excluded:

    * the run's own journal seal -- it records the run recording itself, not a
      deliverable, and counting it as production would put
      ``.sdd/runs/<id>/journal.jsonl`` into every completion diff as an
      undeclared write;
    * artifact **attempt** records -- they are keyed by a declared output that
      did *not* land, so counting one as production would let the record of a
      missing output satisfy its own declaration and quietly erase the finding
      (issue #2559).

    An empty tuple means "this run's spine carries no produced artifact", which
    is a genuine observation. It is **not** the same as having no observation at
    all -- callers that cannot observe pass ``None`` rather than an empty tuple,
    because a diff computed against an absent observation manufactures findings
    out of ignorance.
    """
    return tuple(
        sorted(
            {
                e.uri
                for e in load_production_events(lineage_root, run_id=run_id)
                if not e.is_journal_seal and not e.is_attempt
            }
        )
    )


# ---------------------------------------------------------------------------
# Fan-out divergence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FanoutDivergence:
    """One disagreement between the journaled fan-out and the replayed one.

    Attributes:
        kind: ``dropped`` (the spine implies an event the journal lacks),
            ``duplicated`` (the journal fired one entry more than once),
            ``unexpected`` (the journal carries an entry the spine does not),
            or ``altered`` (same entry, different payload -- the signature of a
            tampered spine row or an edited journal).
        entry_hash: The offending spine entry hash. Always populated, so every
            divergence names the exact entry a reviewer must go look at.
        detail: Human-readable specifics.
    """

    kind: str
    entry_hash: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "entry_hash": self.entry_hash, "detail": self.detail}


def compare_fanout(
    journaled: Sequence[ArtifactProductionEvent],
    replayed: Sequence[ArtifactProductionEvent],
) -> list[FanoutDivergence]:
    """Compare a journaled fan-out against the one the spine implies.

    An intact run yields an empty list: the journal is exactly the projection.
    Every difference is attributed to a specific ``entry_hash``, so "a firing
    went missing" becomes a named entry rather than an investigation.

    Returns:
        Divergences ordered by kind then entry hash, so two verifiers comparing
        the same pair of sequences produce identical output.
    """
    divergences: list[FanoutDivergence] = []

    journaled_by_hash: dict[str, list[ArtifactProductionEvent]] = {}
    for event in journaled:
        journaled_by_hash.setdefault(event.entry_hash, []).append(event)
    replayed_by_hash = {event.entry_hash: event for event in replayed}

    for entry_hash, seen in journaled_by_hash.items():
        if len(seen) > 1:
            divergences.append(
                FanoutDivergence(
                    kind=DIVERGENCE_DUPLICATED,
                    entry_hash=entry_hash,
                    detail=f"journal fired this entry {len(seen)} times; the spine implies exactly one",
                )
            )

    for expected in replayed:
        seen_list = journaled_by_hash.get(expected.entry_hash)
        if not seen_list:
            divergences.append(
                FanoutDivergence(
                    kind=DIVERGENCE_DROPPED,
                    entry_hash=expected.entry_hash,
                    detail=f"spine entry for {expected.uri!r} produced no journaled event",
                )
            )
            continue
        if seen_list[0].to_payload() != expected.to_payload():
            divergences.append(
                FanoutDivergence(
                    kind=DIVERGENCE_ALTERED,
                    entry_hash=expected.entry_hash,
                    detail=(
                        "journaled event does not match the event the spine projects "
                        f"(journaled verified={seen_list[0].verified}, replayed verified={expected.verified})"
                    ),
                )
            )

    for entry_hash, seen in journaled_by_hash.items():
        if entry_hash not in replayed_by_hash:
            divergences.append(
                FanoutDivergence(
                    kind=DIVERGENCE_UNEXPECTED,
                    entry_hash=entry_hash,
                    detail=f"journaled event for {seen[0].uri!r} has no matching spine entry",
                )
            )

    divergences.sort(key=lambda d: (d.kind, d.entry_hash))
    return divergences
