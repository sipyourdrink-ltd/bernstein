"""Run scorecard: a six-section summary of a finished agent run.

The scorecard collapses every replay substrate the orchestrator already
records into one self-describing, deterministic summary so an operator
can answer "is this run replayable, safe, and intact?" from a single
artefact instead of walking the journal, the spine, the audit chain and
the provider-state substrate by hand.

Shape
-----
The scorecard has six named sections, each addressing one question a
human or tool asks first:

* ``trajectory`` - how many steps were recorded, what their shape is, and
  which journal ``seq`` each summary field came from.
* ``verification`` - whether the journal and spine re-verify, and at
  which step the first divergence (if any) was found.
* ``recovery`` - whether the journal required any tail-repair, how many
  rows were dropped, and which row marked the recovery.
* ``state_consistency`` - whether the recorded provider-state mutations
  agree with the journal's ``tool_call`` / ``tool_result`` rows.
* ``safety`` - capability declarations, refusal events and signature
  presence on the receipts that the run produced.
* ``replayability`` - whether the run was recorded with a known key
  scheme, what gateway mode was active, and which fixture files survive.

Every field is paired with a ``Citation`` (the ``seq`` of the journal
row that produced it, or ``None`` for fields that are not derived from
a single journal row). The citation is part of the public wire shape:
a verifier that disagrees on the source of a field can refuse the
scorecard the same way it refuses a run receipt whose embedded bytes
do not re-derive the binding block.

Determinism
-----------
The scorecard follows the same byte-deterministic convention every
bernstein receipt already uses: ``json.dumps(..., sort_keys=True,
separators=(",", ":"))`` and UTF-8. ``Scorecard.canonical_bytes`` returns
those bytes. Wall-clock fields (run start, run end) are kept on the
scorecard for human display but are **stripped** from the canonical
bytes, so a second build of the same recorded run produces a
byte-identical scorecard regardless of when the build runs. The
scorecard is not itself signed in this module - the signed envelope that
binds it to the journal and spine is the run receipt
(:mod:`bernstein.core.replay.run_receipt`); this module only defines the
summary shape and the build-from-journal mechanics.

Scope
-----
The scorecard is a *projection*, not a recomputation: it reads the
journal and named events, summarises them, and cites them. A field
whose cited journal event index is wrong can be detected by re-deriving
the value at that ``seq``; a field whose citation is missing is treated
as a bug, not as a permitted abbreviation.

Pure event-list projection (#5402)
----------------------------------
For callers that already hold an in-memory list of journal events
(typically :func:`bernstein.core.replay.journal.load_events`),
:func:`derive_scorecard` provides a pure projection onto a flat-counts
:class:`ScorecardProjection` document: the same input list yields the
same document, with no filesystem or clock reads outside the journal
itself, and a torn or truncated tail is surfaced as :class:`ScorecardError`
rather than silently undercounted. The projection folds ``tool_call``,
``task_verification_failed``, ``task_retried``, ``approval_gate``,
``approval_honoured`` and ``approval_overridden`` rows into operator-facing
counts (every count carries the event-index range it was computed
from); rows whose event type the projection does not fold are named on
the document in a stable, sorted order so a future journal vocabulary
cannot break the projection. The flat-counts :class:`ScorecardProjection`
co-exists with the six-section :class:`Scorecard` artifact above: the
projection is the at-a-glance view served to operators, while
:class:`Scorecard` is the citation-rich artifact that the run receipt
binds to the journal.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.replay.journal import load_events
from bernstein.core.replay.trajectory import (
    TRAJECTORY_SCHEMA_VERSION,
    trajectory_step_from_entry,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from bernstein.core.persistence.journal import JournalEntry, JournalReader
    from bernstein.core.replay.journal import JournalLoadResult

__all__ = [
    "APPROVAL_GATE_EVENT",
    "APPROVAL_HONOURED_EVENT",
    "APPROVAL_OVERRIDDEN_EVENT",
    "SCORECARD_FILENAME",
    "SCORECARD_PROJECTION_SCHEMA_VERSION",
    "SCORECARD_SCHEMA_VERSION",
    "SCORECARD_TYPE",
    "SCORECARD_TYPE_VERSION",
    "TASK_RETRIED_EVENT",
    "TASK_VERIFICATION_FAILED_EVENT",
    "TOOL_CALL_EVENT",
    "Citation",
    "RecoverySection",
    "ReplayabilitySection",
    "SafetySection",
    "Scorecard",
    "ScorecardError",
    "ScorecardProjection",
    "StateConsistencySection",
    "TrajectorySection",
    "VerificationSection",
    "build_scorecard",
    "derive_scorecard",
    "derive_scorecard_from_path",
]

# ---------------------------------------------------------------------------
# Wire-format identifiers
# ---------------------------------------------------------------------------

#: Scorecard schema version. Bumped on any change to the field set or the
#: canonical encoding. The version is part of every emitted scorecard so an
#: operator can tell a v1 scorecard from a v2 one without consulting the
#: changelog. Mirrors the pattern :data:`RUN_RECEIPT_SCHEMA_VERSION` uses
#: for the run receipt.
SCORECARD_SCHEMA_VERSION: str = "1.0.0"

#: Type version integer. Distinct from :data:`SCORECARD_SCHEMA_VERSION`
#: (which is the semver-like wire string) so a programmatic consumer that
#: wants to branch on a major shape change can compare integers without
#: parsing the string. Mirrors the :data:`JOURNAL_SCHEMA_VERSION` /
#: :data:`TRAJECTORY_SCHEMA_VERSION` discipline.
SCORECARD_TYPE_VERSION: int = 1

#: Scorecard type URL. Versioned so a future v2 scorecard can co-exist with
#: a v1 one in the same run directory without a naming collision; mirrors
#: :data:`RUN_RECEIPT_TYPE` from :mod:`bernstein.core.replay.run_receipt`.
SCORECARD_TYPE: str = "https://bernstein.run/attestations/scorecard/v1"

#: Scorecard filename inside ``.sdd/runs/<run_id>/`` (next to the run
#: receipt). Mirrors :data:`RUN_RECEIPT_FILENAME`.
SCORECARD_FILENAME: str = "scorecard.json"

#: Sections in canonical order. Anything not in this tuple is dropped from
#: :meth:`Scorecard.canonical_bytes` so the wire format cannot drift by
#: accident. The order also drives the validation in
#: :func:`_check_section_completeness` - a section that is ``None`` is
#: rejected up front, so an empty section must be present as an empty
#: dataclass, not as a missing key.
SECTION_NAMES: tuple[str, ...] = (
    "trajectory",
    "verification",
    "recovery",
    "state_consistency",
    "safety",
    "replayability",
)

# ---------------------------------------------------------------------------
# Pure event-list projection constants (#5402)
# ---------------------------------------------------------------------------

#: Schema version of the flat-counts :class:`ScorecardProjection` document
#: returned by :func:`derive_scorecard`. Integer (not a semver string) so a
#: programmatic consumer that branches on a major shape change can compare
#: integers without parsing a string. Distinct from
#: :data:`SCORECARD_SCHEMA_VERSION`, which is the semver string carried by
#: the six-section :class:`Scorecard` artifact.
SCORECARD_PROJECTION_SCHEMA_VERSION: int = 1

#: Journal event recorded for a tool invocation an agent dispatched
#: (issue #1799; the row carries the serialised tool call under
#: ``tool_call``). Counted toward the projection's *tool_calls* total.
TOOL_CALL_EVENT: str = "tool_call"

#: Journal event recorded when a task's verification gate refused the
#: work the agent produced. The projection folds each of these into
#: *verifier_failures*, and the following ``task_retried`` row into
#: *recoveries* (a failed action followed by a repaired retry).
TASK_VERIFICATION_FAILED_EVENT: str = "task_verification_failed"

#: Journal event recorded when coordination handed a task back to the
#: pool for another attempt. Counted both in *retries* and, when
#: preceded by a ``task_verification_failed`` for the same task, in
#: *recoveries*.
TASK_RETRIED_EVENT: str = "task_retried"

#: Journal event recorded for an approval gate that was *encountered*
#: during a run. An encountered gate is one the run reached, regardless
#: of how it was resolved.
APPROVAL_GATE_EVENT: str = "approval_gate"

#: Journal event recorded when an approval gate was *honoured* (the
#: agent's action was approved, the gate did not block). Distinct from
#: an encountered gate so a run that encountered a gate and overrode it
#: shows both numbers, not one folded into the other.
APPROVAL_HONOURED_EVENT: str = "approval_honoured"

#: Journal event recorded when an approval gate was *overridden* (the
#: agent proceeded without approval, or the gate was bypassed by an
#: operator action). Carried as a separate field on the projection so a
#: reader can see the override rate at a glance.
APPROVAL_OVERRIDDEN_EVENT: str = "approval_overridden"

#: Closed set of event types the projection looks at. Any other row is
#: folded into ``ignored_event_types`` so the document names what it
#: skipped and a future journal vocabulary cannot break the projection.
_FOLDED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        TOOL_CALL_EVENT,
        TASK_VERIFICATION_FAILED_EVENT,
        TASK_RETRIED_EVENT,
        APPROVAL_GATE_EVENT,
        APPROVAL_HONOURED_EVENT,
        APPROVAL_OVERRIDDEN_EVENT,
    }
)


# ---------------------------------------------------------------------------
# Citation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Citation:
    """Provenance pointer from a scorecard field back to a journal row.

    The citation is the load-bearing piece of the scorecard's "every
    field traces to a journal event" contract. A field that is *not*
    derived from a single journal row (run-level constants, computed
    totals) carries ``journal_event_index=None``; a field that is
    derived from a specific row carries that row's ``seq``.

    Attributes:
        journal_event_index: ``seq`` of the journal row that produced the
            cited value, or ``None`` for a field whose value is not
            derived from a single row.
        step_hash: ``step_hash`` of that row, copied verbatim - never
            recomputed, the same way
            :func:`bernstein.core.replay.trajectory.trajectory_step_from_entry`
            copies it. Lets a verifier confirm the cited row is the row
            the scorecard claims without reading the journal.
        section: Name of the scorecard section that contains the field
            ("trajectory", "verification", ...). Redundant with the
            embedding point but makes a single citation row useful
            when the scorecard is flattened for grep.
        field: Name of the field inside the section.
    """

    journal_event_index: int | None
    step_hash: str | None
    section: str
    field: str

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly wire shape of the citation.

        ``step_hash`` is omitted (not nulled) when ``None`` so a citation
        for a run-level field serialises identically regardless of
        whether a sentinel was assigned. Same omission discipline as
        :meth:`TrajectoryStep.to_dict` uses for the ``effort`` field.
        """
        row: dict[str, Any] = {
            "section": self.section,
            "field": self.field,
        }
        if self.journal_event_index is not None:
            row["journal_event_index"] = self.journal_event_index
        if self.step_hash is not None:
            row["step_hash"] = self.step_hash
        return row

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Citation:
        """Inverse of :meth:`to_dict`."""
        index = raw.get("journal_event_index")
        return cls(
            journal_event_index=int(index) if index is not None else None,
            step_hash=raw.get("step_hash"),
            section=str(raw["section"]),
            field=str(raw["field"]),
        )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrajectorySection:
    """Step-level shape summary of the recorded run.

    Attributes:
        step_count: Number of trajectory rows the journal projected.
        first_step_index: ``index`` of the first projected row, or
            ``None`` for an empty trajectory.
        last_step_index: ``index`` of the last projected row, or
            ``None``.
        first_step_hash: ``step_hash`` of the first row, or ``None``.
        last_step_hash: ``step_hash`` of the last row, or ``None``.
        schema_version: Trajectory row schema version the projection was
            built against. Mirrors
            :data:`bernstein.core.replay.trajectory.TRAJECTORY_SCHEMA_VERSION`.
        citations: One :class:`Citation` per non-None field, mapping
            that field back to the journal row it came from.
    """

    step_count: int
    first_step_index: int | None
    last_step_index: int | None
    first_step_hash: str | None
    last_step_hash: str | None
    schema_version: int = TRAJECTORY_SCHEMA_VERSION
    citations: tuple[Citation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the wire shape. ``None`` fields are omitted."""
        row: dict[str, Any] = {"step_count": self.step_count}
        if self.first_step_index is not None:
            row["first_step_index"] = self.first_step_index
        if self.last_step_index is not None:
            row["last_step_index"] = self.last_step_index
        if self.first_step_hash is not None:
            row["first_step_hash"] = self.first_step_hash
        if self.last_step_hash is not None:
            row["last_step_hash"] = self.last_step_hash
        row["schema_version"] = self.schema_version
        return row

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TrajectorySection:
        """Inverse of :meth:`to_dict`.

        ``None`` fields are recovered by key absence - mirrors the
        omission discipline :meth:`to_dict` uses, so a round trip
        yields an equal instance whose canonical bytes are
        byte-identical to the source.
        """
        return cls(
            step_count=int(raw.get("step_count", 0)),
            first_step_index=(int(raw["first_step_index"]) if "first_step_index" in raw else None),
            last_step_index=(int(raw["last_step_index"]) if "last_step_index" in raw else None),
            first_step_hash=raw.get("first_step_hash"),
            last_step_hash=raw.get("last_step_hash"),
            schema_version=int(raw.get("schema_version", TRAJECTORY_SCHEMA_VERSION)),
            citations=(),
        )


@dataclass(frozen=True, slots=True)
class VerificationSection:
    """Outcome of re-verifying the run's substrates from their own bytes.

    Attributes:
        journal_ok: ``True`` only when the journal chain re-derives from
            genesis to the recorded head.
        journal_head: Recomputed journal head, or ``""`` for an empty
            journal.
        journal_steps: Number of journal rows walked.
        divergent_step: 0-based index of the first divergent row, or
            ``None`` when the chain verified.
        spine_ok: ``True`` only when the lineage spine re-derives.
        spine_head: Recomputed spine head, or ``""`` for an empty spine.
        spine_entries: Number of spine entries walked.
        citations: One :class:`Citation` per journal-derived field.
    """

    journal_ok: bool
    journal_head: str
    journal_steps: int
    divergent_step: int | None
    spine_ok: bool
    spine_head: str
    spine_entries: int
    citations: tuple[Citation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the wire shape."""
        row: dict[str, Any] = {
            "journal_ok": self.journal_ok,
            "journal_head": self.journal_head,
            "journal_steps": self.journal_steps,
            "spine_ok": self.spine_ok,
            "spine_head": self.spine_head,
            "spine_entries": self.spine_entries,
        }
        if self.divergent_step is not None:
            row["divergent_step"] = self.divergent_step
        return row

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> VerificationSection:
        """Inverse of :meth:`to_dict`."""
        return cls(
            journal_ok=bool(raw.get("journal_ok", False)),
            journal_head=str(raw.get("journal_head", "")),
            journal_steps=int(raw.get("journal_steps", 0)),
            divergent_step=(int(raw["divergent_step"]) if "divergent_step" in raw else None),
            spine_ok=bool(raw.get("spine_ok", False)),
            spine_head=str(raw.get("spine_head", "")),
            spine_entries=int(raw.get("spine_entries", 0)),
            citations=(),
        )


@dataclass(frozen=True, slots=True)
class RecoverySection:
    """Whether the run's journal required any tail-repair or recovery.

    Attributes:
        repaired: ``True`` if the journal was opened in any mode that
            dropped a trailing row.
        dropped_rows: Number of journal rows the recovery dropped, or
            ``0`` for an intact journal.
        first_recoverable_seq: ``seq`` of the last validated row after
            recovery, or ``None`` when no recovery was needed.
        recovery_event_index: ``seq`` of the journal row that marked
            the recovery, or ``None`` when the journal was intact.
        citations: One :class:`Citation` per journal-derived field.
    """

    repaired: bool
    dropped_rows: int
    first_recoverable_seq: int | None
    recovery_event_index: int | None
    citations: tuple[Citation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the wire shape."""
        row: dict[str, Any] = {
            "repaired": self.repaired,
            "dropped_rows": self.dropped_rows,
        }
        if self.first_recoverable_seq is not None:
            row["first_recoverable_seq"] = self.first_recoverable_seq
        if self.recovery_event_index is not None:
            row["recovery_event_index"] = self.recovery_event_index
        return row

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RecoverySection:
        """Inverse of :meth:`to_dict`."""
        return cls(
            repaired=bool(raw.get("repaired", False)),
            dropped_rows=int(raw.get("dropped_rows", 0)),
            first_recoverable_seq=(int(raw["first_recoverable_seq"]) if "first_recoverable_seq" in raw else None),
            recovery_event_index=(int(raw["recovery_event_index"]) if "recovery_event_index" in raw else None),
            citations=(),
        )


@dataclass(frozen=True, slots=True)
class StateConsistencySection:
    """Whether recorded provider-state mutations agree with the journal.

    Attributes:
        mutation_count: Number of distinct provider-state mutations the
            run recorded.
        disagreement_count: Number of mutations whose recorded effect
            disagrees with the ``tool_call`` / ``tool_result`` rows of
            the cited journal step.
        last_mutation_event_index: ``seq`` of the journal row that
            recorded the most recent mutation, or ``None`` for a run
            that never mutated provider state.
        citations: One :class:`Citation` per journal-derived field.
    """

    mutation_count: int
    disagreement_count: int
    last_mutation_event_index: int | None
    citations: tuple[Citation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the wire shape."""
        row: dict[str, Any] = {
            "mutation_count": self.mutation_count,
            "disagreement_count": self.disagreement_count,
        }
        if self.last_mutation_event_index is not None:
            row["last_mutation_event_index"] = self.last_mutation_event_index
        return row

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> StateConsistencySection:
        """Inverse of :meth:`to_dict`."""
        return cls(
            mutation_count=int(raw.get("mutation_count", 0)),
            disagreement_count=int(raw.get("disagreement_count", 0)),
            last_mutation_event_index=(
                int(raw["last_mutation_event_index"]) if "last_mutation_event_index" in raw else None
            ),
            citations=(),
        )


@dataclass(frozen=True, slots=True)
class SafetySection:
    """Capability declarations, refusals, and signature presence.

    Attributes:
        capability_declared: ``True`` if the run recorded at least one
            capability declaration.
        refusal_count: Number of refusal events the run recorded (MCP
            ``MandateRefused``, payment refusals, etc.).
        run_receipt_signed: ``True`` if a signed run receipt exists for
            this run.
        citations: One :class:`Citation` per journal-derived field.
    """

    capability_declared: bool
    refusal_count: int
    run_receipt_signed: bool
    citations: tuple[Citation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the wire shape."""
        return {
            "capability_declared": self.capability_declared,
            "refusal_count": self.refusal_count,
            "run_receipt_signed": self.run_receipt_signed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SafetySection:
        """Inverse of :meth:`to_dict`."""
        return cls(
            capability_declared=bool(raw.get("capability_declared", False)),
            refusal_count=int(raw.get("refusal_count", 0)),
            run_receipt_signed=bool(raw.get("run_receipt_signed", False)),
            citations=(),
        )


@dataclass(frozen=True, slots=True)
class ReplayabilitySection:
    """Whether the run is replayable from the artefacts on disk.

    Attributes:
        recorded: ``True`` if the gateway recorded this run.
        key_scheme: The replay key scheme the fixtures were recorded
            under, or ``""`` for a non-recorded run.
        gateway_mode: The gateway mode the run ran in ("record" /
            "replay" / "passthrough"), or ``""``.
        fixture_present: ``True`` if the recorded fixture file exists.
        citations: One :class:`Citation` per journal-derived field.
    """

    recorded: bool
    key_scheme: str
    gateway_mode: str
    fixture_present: bool
    citations: tuple[Citation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the wire shape."""
        return {
            "recorded": self.recorded,
            "key_scheme": self.key_scheme,
            "gateway_mode": self.gateway_mode,
            "fixture_present": self.fixture_present,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ReplayabilitySection:
        """Inverse of :meth:`to_dict`."""
        return cls(
            recorded=bool(raw.get("recorded", False)),
            key_scheme=str(raw.get("key_scheme", "")),
            gateway_mode=str(raw.get("gateway_mode", "")),
            fixture_present=bool(raw.get("fixture_present", False)),
            citations=(),
        )


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scorecard:
    """The full six-section scorecard for a finished run.

    Attributes:
        run_id: Run the scorecard summarises.
        trajectory: Step-level shape summary.
        verification: Re-derivation outcomes for the journal and spine.
        recovery: Tail-repair summary.
        state_consistency: Provider-state vs journal agreement.
        safety: Capability / refusal / signature summary.
        replayability: Recording / key-scheme / fixture summary.
        schema_version: Scorecard schema version (semver-like string).
        type_version: Scorecard type version (integer). See
            :data:`SCORECARD_TYPE_VERSION`.
        scorecard_type: Scorecard type URL.
        wall_clock_start: Run start as unix epoch seconds, or ``None``
            when unknown. Excluded from :meth:`canonical_bytes`.
        wall_clock_end: Run end as unix epoch seconds, or ``None``.
            Excluded from :meth:`canonical_bytes`.
    """

    run_id: str
    trajectory: TrajectorySection
    verification: VerificationSection
    recovery: RecoverySection
    state_consistency: StateConsistencySection
    safety: SafetySection
    replayability: ReplayabilitySection
    schema_version: str = SCORECARD_SCHEMA_VERSION
    type_version: int = SCORECARD_TYPE_VERSION
    scorecard_type: str = SCORECARD_TYPE
    wall_clock_start: float | None = None
    wall_clock_end: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the wire shape of the scorecard.

        Wall-clock fields are **omitted** here for symmetry with
        :meth:`canonical_bytes`, which strips them so two builds of the
        same recorded run produce byte-identical artefacts. Callers
        that need them for display must keep their own copy.
        """
        return {
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "type_version": self.type_version,
            "scorecard_type": self.scorecard_type,
            "trajectory": self.trajectory.to_dict(),
            "verification": self.verification.to_dict(),
            "recovery": self.recovery.to_dict(),
            "state_consistency": self.state_consistency.to_dict(),
            "safety": self.safety.to_dict(),
            "replayability": self.replayability.to_dict(),
        }

    def citations(self) -> list[Citation]:
        """Return the flat list of every citation in the scorecard.

        Order is section order, then field order, so two independent
        builds of the same run produce the same flat citation list.
        """
        out: list[Citation] = []
        out.extend(self.trajectory.citations)
        out.extend(self.verification.citations)
        out.extend(self.recovery.citations)
        out.extend(self.state_consistency.citations)
        out.extend(self.safety.citations)
        out.extend(self.replayability.citations)
        return out

    def citations_by_section(self) -> dict[str, list[Citation]]:
        """Group :meth:`citations` by section name."""
        grouped: dict[str, list[Citation]] = {name: [] for name in SECTION_NAMES}
        for cite in self.citations():
            grouped[cite.section].append(cite)
        return grouped

    def canonical_bytes(self) -> bytes:
        """Return the canonical UTF-8 bytes of the scorecard.

        Sorted keys, compact separators, no wall-clock - so two builds
        of the same recorded run produce byte-identical bytes. The
        encoding contract mirrors
        :meth:`bernstein.core.replay.trajectory.TrajectoryStep.canonical_bytes`
        and :func:`bernstein.core.persistence.journal.canonical_step_payload`,
        and is identical to the convention used by
        :func:`bernstein.core.replay.run_receipt._canonical_json_bytes`
        (canonical JSON, sorted keys, ``(",", ":")`` separators, UTF-8)
        so the whole receipt family canonicalises identically.
        """
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Scorecard:
        """Inverse of :meth:`to_dict`.

        Wall-clock fields (``wall_clock_start``, ``wall_clock_end``)
        are recovered from ``raw`` when present and default to
        ``None`` otherwise - matching the omission discipline in
        :meth:`to_dict` and :meth:`canonical_bytes`. A round trip
        through ``Scorecard -> to_dict -> from_dict -> canonical_bytes``
        yields bytes byte-identical to the original
        :meth:`canonical_bytes` output.
        """
        return cls(
            run_id=str(raw["run_id"]),
            trajectory=TrajectorySection.from_dict(raw["trajectory"]),
            verification=VerificationSection.from_dict(raw["verification"]),
            recovery=RecoverySection.from_dict(raw["recovery"]),
            state_consistency=StateConsistencySection.from_dict(raw["state_consistency"]),
            safety=SafetySection.from_dict(raw["safety"]),
            replayability=ReplayabilitySection.from_dict(raw["replayability"]),
            schema_version=str(raw.get("schema_version", SCORECARD_SCHEMA_VERSION)),
            type_version=int(raw.get("type_version", SCORECARD_TYPE_VERSION)),
            scorecard_type=str(raw.get("scorecard_type", SCORECARD_TYPE)),
            wall_clock_start=(float(raw["wall_clock_start"]) if "wall_clock_start" in raw else None),
            wall_clock_end=(float(raw["wall_clock_end"]) if "wall_clock_end" in raw else None),
        )


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _entry_citation(
    section: str,
    field: str,
    entry: JournalEntry | None,
) -> Citation:
    """Build a citation for a field sourced from one journal entry.

    A ``None`` entry produces a citation with no ``journal_event_index``
    and no ``step_hash`` - used for run-level fields that are not
    derived from a single row.
    """
    if entry is None:
        return Citation(
            journal_event_index=None,
            step_hash=None,
            section=section,
            field=field,
        )
    return Citation(
        journal_event_index=entry.seq,
        step_hash=entry.step_hash,
        section=section,
        field=field,
    )


def _trajectory_section(
    entries: list[JournalEntry],
) -> TrajectorySection:
    """Project the journal onto the trajectory section.

    Mirrors :func:`bernstein.core.replay.trajectory.project_trajectory`
    but builds the summary fields directly, so a single pass over the
    journal produces both the section and the per-field citations.
    """
    if not entries:
        return TrajectorySection(
            step_count=0,
            first_step_index=None,
            last_step_index=None,
            first_step_hash=None,
            last_step_hash=None,
            citations=(),
        )
    first = entries[0]
    last = entries[-1]
    first_step = trajectory_step_from_entry(first)
    last_step = trajectory_step_from_entry(last)
    citations = (
        _entry_citation("trajectory", "first_step_index", first),
        _entry_citation("trajectory", "last_step_index", last),
        _entry_citation("trajectory", "first_step_hash", first),
        _entry_citation("trajectory", "last_step_hash", last),
    )
    return TrajectorySection(
        step_count=len(entries),
        first_step_index=first_step.index,
        last_step_index=last_step.index,
        first_step_hash=first_step.step_hash,
        last_step_hash=last_step.step_hash,
        schema_version=first_step.schema_version,
        citations=citations,
    )


def _verification_section(
    entries: list[JournalEntry],
    *,
    journal_ok: bool,
    journal_head: str,
    divergent_step: int | None,
    spine_ok: bool,
    spine_head: str,
    spine_entries: int,
) -> VerificationSection:
    """Build the verification section with citations to the cited rows.

    The journal-derived fields are cited to the first and last journal
    entries (the rows whose ``step_hash`` forms the head and whose
    ``prev_hash`` is genesis). For an empty journal both are ``None``
    citations; for a divergent chain, the ``divergent_step`` index
    points into the journal's own ``seq`` numbering and is not
    separately cited (its meaning is "the seq at which the chain
    first failed to re-derive").
    """
    citations: list[Citation] = []
    if entries:
        citations.append(_entry_citation("verification", "journal_head", entries[-1]))
        citations.append(_entry_citation("verification", "journal_steps", entries[-1]))
    else:
        citations.append(_entry_citation("verification", "journal_head", None))
        citations.append(_entry_citation("verification", "journal_steps", None))
    return VerificationSection(
        journal_ok=journal_ok,
        journal_head=journal_head,
        journal_steps=len(entries),
        divergent_step=divergent_step,
        spine_ok=spine_ok,
        spine_head=spine_head,
        spine_entries=spine_entries,
        citations=tuple(citations),
    )


def _recovery_section(
    entries: list[JournalEntry],
    *,
    repaired: bool,
    dropped_rows: int,
    first_recoverable_seq: int | None,
) -> RecoverySection:
    """Build the recovery section.

    ``recovery_event_index`` is the ``seq`` of the last validated row -
    the row at which recovery stopped - and is therefore the same as
    ``first_recoverable_seq`` for a non-empty journal. A ``None``
    ``first_recoverable_seq`` produces a ``None`` citation, used both
    for an empty journal and for one whose repair found nothing to
    keep.
    """
    if first_recoverable_seq is not None and entries:
        cited_entry = next(
            (e for e in entries if e.seq == first_recoverable_seq),
            None,
        )
    else:
        cited_entry = None
    citations = (_entry_citation("recovery", "first_recoverable_seq", cited_entry),)
    return RecoverySection(
        repaired=repaired,
        dropped_rows=dropped_rows,
        first_recoverable_seq=first_recoverable_seq,
        recovery_event_index=first_recoverable_seq,
        citations=citations,
    )


def _state_consistency_section(
    entries: list[JournalEntry],
    *,
    mutation_count: int,
    disagreement_count: int,
    last_mutation_event_index: int | None,
) -> StateConsistencySection:
    """Build the state-consistency section.

    ``last_mutation_event_index`` cites the journal row that recorded
    the most recent mutation; ``None`` for a run that never mutated
    provider state. The citation is ``None`` in that case - the field
    is a count, not a journal-derived value.
    """
    cited_entry: JournalEntry | None = None
    if last_mutation_event_index is not None:
        cited_entry = next(
            (e for e in entries if e.seq == last_mutation_event_index),
            None,
        )
    citations = (
        _entry_citation(
            "state_consistency",
            "last_mutation_event_index",
            cited_entry,
        ),
    )
    return StateConsistencySection(
        mutation_count=mutation_count,
        disagreement_count=disagreement_count,
        last_mutation_event_index=last_mutation_event_index,
        citations=citations,
    )


def _safety_section(
    *,
    capability_declared: bool,
    refusal_count: int,
    run_receipt_signed: bool,
) -> SafetySection:
    """Build the safety section.

    No field is derived from a single journal row (capability /
    refusal / signature presence are run-level facts), so the
    citation list is empty by design - the absence of a citation is
    itself a signal that the field is not journal-derived.
    """
    return SafetySection(
        capability_declared=capability_declared,
        refusal_count=refusal_count,
        run_receipt_signed=run_receipt_signed,
        citations=(),
    )


def _replayability_section(
    *,
    recorded: bool,
    key_scheme: str,
    gateway_mode: str,
    fixture_present: bool,
) -> ReplayabilitySection:
    """Build the replayability section.

    Same shape as :func:`_safety_section`: every field is a run-level
    fact, not a journal-derived value, so the citation list is empty.
    """
    return ReplayabilitySection(
        recorded=recorded,
        key_scheme=key_scheme,
        gateway_mode=gateway_mode,
        fixture_present=fixture_present,
        citations=(),
    )


def _check_section_completeness(scorecard: Scorecard) -> None:
    """Refuse a scorecard that is missing a section.

    The section set is fixed by :data:`SECTION_NAMES`; every member must
    be present on a built scorecard, even if its field values are
    empty / ``None`` / ``False``. A section that is ``None`` indicates
    a builder bug: the projection tried to look at a substrate that
    the run did not record and returned ``None`` instead of a zeroed
    section. Such a scorecard would be unverifiable (a verifier
    refusing the missing section would have nothing to compare against)
    so the build is refused up front.
    """
    sections: dict[str, Any] = {
        "trajectory": scorecard.trajectory,
        "verification": scorecard.verification,
        "recovery": scorecard.recovery,
        "state_consistency": scorecard.state_consistency,
        "safety": scorecard.safety,
        "replayability": scorecard.replayability,
    }
    for name in SECTION_NAMES:
        if sections[name] is None:
            raise ValueError(f"scorecard section {name!r} is missing; refusing to build")


def build_scorecard(
    run_id: str,
    *,
    reader: JournalReader,
    journal_ok: bool = True,
    journal_head: str = "",
    divergent_step: int | None = None,
    spine_ok: bool = True,
    spine_head: str = "",
    spine_entries: int = 0,
    repaired: bool = False,
    dropped_rows: int = 0,
    first_recoverable_seq: int | None = None,
    mutation_count: int = 0,
    disagreement_count: int = 0,
    last_mutation_event_index: int | None = None,
    capability_declared: bool = False,
    refusal_count: int = 0,
    run_receipt_signed: bool = False,
    recorded: bool = False,
    key_scheme: str = "",
    gateway_mode: str = "",
    fixture_present: bool = False,
    wall_clock_start: float | None = None,
    wall_clock_end: float | None = None,
) -> Scorecard:
    """Build the six-section scorecard for one run.

    The reader is the only required input; every other field defaults
    to the "empty run" value, so a scorecard can be built for a run
    that never recorded a spine entry, never mutated provider state,
    or was never gateway-recorded. The empty defaults are picked so
    that :func:`_check_section_completeness` succeeds: an empty
    section is still a section, never a missing section.

    Args:
        run_id: Run the scorecard summarises.
        reader: A :class:`JournalReader` over the run's journal. The
            reader's entries are walked exactly once to populate
            ``trajectory`` and the citation rows for the other
            journal-derived sections.
        journal_ok: Whether the journal chain re-verified.
        journal_head: Recomputed journal head (``""`` for empty).
        divergent_step: 0-based index of first divergent row, or
            ``None``.
        spine_ok: Whether the lineage spine re-verified.
        spine_head: Recomputed spine head (``""`` for empty).
        spine_entries: Number of spine entries walked.
        repaired: Whether the journal required tail-repair.
        dropped_rows: Number of rows the repair dropped.
        first_recoverable_seq: ``seq`` of the last validated row after
            repair, or ``None``.
        mutation_count: Number of distinct provider-state mutations.
        disagreement_count: Mutations whose effect disagreed with the
            journal ``tool_call`` / ``tool_result`` rows.
        last_mutation_event_index: ``seq`` of the most recent mutation
            event, or ``None``.
        capability_declared: Whether the run recorded a capability
            declaration.
        refusal_count: Number of refusal events the run recorded.
        run_receipt_signed: Whether a signed run receipt exists.
        recorded: Whether the gateway recorded the run.
        key_scheme: Replay key scheme the fixtures were recorded
            under, or ``""``.
        gateway_mode: Gateway mode the run ran in, or ``""``.
        fixture_present: Whether the recorded fixture file exists.
        wall_clock_start: Run start as unix epoch seconds, or
            ``None``. Display only; stripped from
            :meth:`Scorecard.canonical_bytes`.
        wall_clock_end: Run end as unix epoch seconds, or ``None``.

    Returns:
        The built :class:`Scorecard`. Callers that need the canonical
        bytes for hashing call :meth:`Scorecard.canonical_bytes`.
    """
    entries: list[JournalEntry] = list(_iter_entries(reader))
    scorecard = Scorecard(
        run_id=run_id,
        trajectory=_trajectory_section(entries),
        verification=_verification_section(
            entries,
            journal_ok=journal_ok,
            journal_head=journal_head,
            divergent_step=divergent_step,
            spine_ok=spine_ok,
            spine_head=spine_head,
            spine_entries=spine_entries,
        ),
        recovery=_recovery_section(
            entries,
            repaired=repaired,
            dropped_rows=dropped_rows,
            first_recoverable_seq=first_recoverable_seq,
        ),
        state_consistency=_state_consistency_section(
            entries,
            mutation_count=mutation_count,
            disagreement_count=disagreement_count,
            last_mutation_event_index=last_mutation_event_index,
        ),
        safety=_safety_section(
            capability_declared=capability_declared,
            refusal_count=refusal_count,
            run_receipt_signed=run_receipt_signed,
        ),
        replayability=_replayability_section(
            recorded=recorded,
            key_scheme=key_scheme,
            gateway_mode=gateway_mode,
            fixture_present=fixture_present,
        ),
        wall_clock_start=wall_clock_start,
        wall_clock_end=wall_clock_end,
    )
    _check_section_completeness(scorecard)
    return scorecard


def _iter_entries(reader: JournalReader) -> Iterable[JournalEntry]:
    """Yield every entry from *reader* in seq order.

    Defensive wrapper around :meth:`JournalReader.entries` so the
    projection can materialise the list exactly once without holding
    the reader open across the multi-section build.
    """
    yield from reader.entries()


# ---------------------------------------------------------------------------
# Pure event-list projection types and helpers (#5402)
# ---------------------------------------------------------------------------


class ScorecardError(ValueError):
    """Raised when a journal cannot be turned into a scorecard projection."""


@dataclass(frozen=True, slots=True)
class _Count:
    """A single number with the event-index range it was computed from.

    Attributes:
        count: The reported number.
        first_index: 0-based index of the first journal row that
            contributed to *count*. ``None`` when *count* is zero (the
            range is empty by definition).
        last_index: 0-based index of the last journal row that
            contributed to *count*. ``None`` when *count* is zero.
    """

    count: int
    first_index: int | None
    last_index: int | None

    @classmethod
    def empty(cls) -> _Count:
        return cls(count=0, first_index=None, last_index=None)


@dataclass(frozen=True, slots=True)
class ScorecardProjection:
    """The operator-facing numbers projected from a sealed journal (#5402).

    Renamed from ``Scorecard`` (the flat-counts design from the #5402
    slice) so it can co-exist in the same module with the six-section
    :class:`Scorecard` artifact from #5413. The two serve different
    audiences: this one is the at-a-glance counts view served to
    operators, while :class:`Scorecard` is the citation-rich six-section
    document that the run receipt binds to the journal.

    Every number in this document is paired with the event-index range
    the projection walked, so a reader can go back to the events behind
    the figure and verify the count.

    Attributes:
        schema_version: The projection schema version. See
            :data:`SCORECARD_PROJECTION_SCHEMA_VERSION`.
        run_id: The run whose journal was projected.
        event_count: Total number of journal rows the projection
            considered.
        tool_calls: Count of ``tool_call`` rows, with the
            event-index range they occupied.
        retries: Count of ``task_retried`` rows, with the
            event-index range.
        recoveries: Count of retries that immediately followed a
            ``task_verification_failed`` for the same task. A recovery
            is a *failed action followed by a repaired retry*, not a
            raw retry count.
        verifier_failures: Count of ``task_verification_failed``
            rows, with the event-index range.
        verifier_coverage: Fraction of claimed tasks that reached a
            verifier verdict (success or failure). Carries the
            event-index range of the underlying claim set.
        approval_gates_encountered: Count of ``approval_gate`` rows,
            with the event-index range.
        approval_gates_honoured: Count of ``approval_honoured`` rows,
            with the event-index range. Distinct from
            *approval_gates_encountered* so a run that encountered a
            gate and overrode it shows both numbers, not one folded
            into the other.
        approval_gates_overridden: Count of ``approval_overridden``
            rows, with the event-index range. Visible on its own so
            an override rate is computable at a glance.
        ignored_event_types: Event types seen in the journal that the
            projection does not fold. Names are sorted so the document
            is deterministic.
    """

    schema_version: int
    run_id: str
    event_count: int
    tool_calls: _Count
    retries: _Count
    recoveries: _Count
    verifier_failures: _Count
    verifier_coverage: _Count
    approval_gates_encountered: _Count
    approval_gates_honoured: _Count
    approval_gates_overridden: _Count
    ignored_event_types: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-shaped projection served to operators."""
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "event_count": self.event_count,
            "tool_calls": _count_to_dict(self.tool_calls),
            "retries": _count_to_dict(self.retries),
            "recoveries": _count_to_dict(self.recoveries),
            "verifier_failures": _count_to_dict(self.verifier_failures),
            "verifier_coverage": _count_to_dict(self.verifier_coverage),
            "approval_gates": {
                "encountered": _count_to_dict(self.approval_gates_encountered),
                "honoured": _count_to_dict(self.approval_gates_honoured),
                "overridden": _count_to_dict(self.approval_gates_overridden),
            },
            "ignored_event_types": list(self.ignored_event_types),
        }


def _count_to_dict(count: _Count) -> dict[str, Any]:
    """Render a :class:`_Count` to its public dict shape.

    A count with a positive value carries a non-empty
    ``event_index_range``; a count of zero carries an explicit
    ``null`` for both ends so consumers can branch on the field
    without guessing what absence means.
    """
    if count.count == 0:
        return {
            "count": 0,
            "event_index_range": {"first": None, "last": None},
        }
    return {
        "count": count.count,
        "event_index_range": {
            "first": count.first_index,
            "last": count.last_index,
        },
    }


def _build_count(positions: Sequence[int]) -> _Count:
    """Wrap the positions of one event class into a :class:`_Count`.

    Args:
        positions: Ordered 0-based indices of journal rows that
            contribute to the count. Empty when the count is zero.

    Returns:
        A :class:`_Count` whose ``count`` matches ``len(positions)``
        and whose range spans the first and last positions. A
        zero-count yields :meth:`_Count.empty` so the range is
        unambiguously empty rather than ``[None, None]``.
    """
    if not positions:
        return _Count.empty()
    return _Count(
        count=len(positions),
        first_index=positions[0],
        last_index=positions[-1],
    )


def _claimant_task_ids(events: Sequence[Mapping[str, Any]]) -> set[str]:
    """Return the set of task ids ever claimed by an agent in *events*.

    The projection's *verifier_coverage* is the fraction of claimed
    tasks that reached a verifier verdict, so the claim set has to be
    collected from ``task_claimed`` rows before the verdicts are
    folded.
    """
    claimed: set[str] = set()
    for row in events:
        if str(row.get("event", "")) != "task_claimed":
            continue
        task_id = row.get("task_id")
        if isinstance(task_id, str) and task_id:
            claimed.add(task_id)
    return claimed


def _verdict_task_ids(events: Sequence[Mapping[str, Any]]) -> set[str]:
    """Return task ids that reached a verifier verdict in *events*.

    A "verdict" is either ``task_verification_failed`` (the gate
    refused the work) or ``task_completed`` (the gate accepted it).
    Both rows name a task id and both are projectable here.
    """
    verdicts: set[str] = set()
    for row in events:
        event = str(row.get("event", ""))
        if event not in (TASK_VERIFICATION_FAILED_EVENT, "task_completed"):
            continue
        task_id = row.get("task_id")
        if isinstance(task_id, str) and task_id:
            verdicts.add(task_id)
    return verdicts


def derive_scorecard(
    events: Sequence[Mapping[str, Any]],
    *,
    run_id: str = "",
) -> ScorecardProjection:
    """Fold a list of journal rows into a :class:`ScorecardProjection`.

    The function is a *pure* projection: given the same list of
    rows, it returns the same document. It does not read the
    filesystem, the clock, or the process environment; the caller
    hands it the events :func:`load_events` produced.

    Args:
        events: Journal rows in append order, as returned by
            :func:`bernstein.core.replay.journal.load_events`.
        run_id: Optional run id recorded on the projection. When
            ``""`` the field is left empty so the function still
            works for an inline sequence detached from a run.

    Returns:
        A :class:`ScorecardProjection` whose every count is paired
        with the event-index range it came from.

    Raises:
        ScorecardError: The input carries no event rows at all.
    """
    if not events:
        raise ScorecardError("cannot derive a scorecard from an empty journal")

    tool_call_positions: list[int] = []
    retry_positions: list[int] = []
    verifier_failure_positions: list[int] = []
    approval_encountered_positions: list[int] = []
    approval_honoured_positions: list[int] = []
    approval_overridden_positions: list[int] = []

    # ``last_event_per_task`` tracks the most recent event on a task
    # id, so a retry can be paired with the verification failure it
    # repaired. A recovery is a retry whose immediate predecessor on
    # the same task was a ``task_verification_failed``.
    last_event_per_task: dict[str, str] = {}
    recovery_positions: list[int] = []

    ignored: set[str] = set()

    for index, row in enumerate(events):
        event = str(row.get("event", ""))
        if event not in _FOLDED_EVENT_TYPES:
            # Record-but-do-not-fold every other event type so a
            # future journal vocabulary cannot break the projection:
            # operators see what was skipped, in a stable order.
            if event:
                ignored.add(event)
            continue

        task_id_raw = row.get("task_id")
        task_id = task_id_raw if isinstance(task_id_raw, str) else ""

        if event == TOOL_CALL_EVENT:
            tool_call_positions.append(index)
        elif event == TASK_VERIFICATION_FAILED_EVENT:
            verifier_failure_positions.append(index)
        elif event == TASK_RETRIED_EVENT:
            retry_positions.append(index)
            if task_id and last_event_per_task.get(task_id) == TASK_VERIFICATION_FAILED_EVENT:
                recovery_positions.append(index)
        elif event == APPROVAL_GATE_EVENT:
            approval_encountered_positions.append(index)
        elif event == APPROVAL_HONOURED_EVENT:
            approval_honoured_positions.append(index)
        elif event == APPROVAL_OVERRIDDEN_EVENT:
            approval_overridden_positions.append(index)

        if task_id:
            last_event_per_task[task_id] = event

    claimed = _claimant_task_ids(events)
    verdicts = _verdict_task_ids(events)
    if claimed:
        coverage_count = len(claimed & verdicts)
        # The coverage denominator is the set of *distinct* claimed
        # tasks; we report it as a single value, with the index
        # range spanning the first and last claim of any claimed
        # task. Sorting the list keeps the range stable across
        # Python runs.
        first_claim = next(
            (
                i
                for i, row in enumerate(events)
                if str(row.get("event", "")) == "task_claimed"
                and isinstance(row.get("task_id"), str)
                and row.get("task_id") in claimed
            ),
            None,
        )
        last_claim = next(
            (
                i
                for i in range(len(events) - 1, -1, -1)
                if str(events[i].get("event", "")) == "task_claimed"
                and isinstance(events[i].get("task_id"), str)
                and events[i].get("task_id") in claimed
            ),
            None,
        )
        verifier_coverage = _Count(
            count=coverage_count,
            first_index=first_claim,
            last_index=last_claim,
        )
    else:
        verifier_coverage = _Count.empty()

    return ScorecardProjection(
        schema_version=SCORECARD_PROJECTION_SCHEMA_VERSION,
        run_id=run_id,
        event_count=len(events),
        tool_calls=_build_count(tool_call_positions),
        retries=_build_count(retry_positions),
        recoveries=_build_count(recovery_positions),
        verifier_failures=_build_count(verifier_failure_positions),
        verifier_coverage=verifier_coverage,
        approval_gates_encountered=_build_count(approval_encountered_positions),
        approval_gates_honoured=_build_count(approval_honoured_positions),
        approval_gates_overridden=_build_count(approval_overridden_positions),
        ignored_event_types=tuple(sorted(ignored)),
    )


def derive_scorecard_from_path(path: Any) -> ScorecardProjection:
    """Load *path* and fold it into a :class:`ScorecardProjection`.

    Convenience wrapper around :func:`load_events` and
    :func:`derive_scorecard`. The path is read in tolerant mode so
    the torn-tail detection below can do its job; a strict reader
    would refuse the journal before the projection has a chance to
    name the cause.

    Args:
        path: A filesystem path to a ``journal.jsonl`` file.

    Returns:
        A :class:`ScorecardProjection` derived from the loaded events.

    Raises:
        ScorecardError: The journal does not exist, has no
            parseable events, or the tolerant reader had to discard
            rows at the tail (a torn write, not corruption).
    """
    from pathlib import Path

    journal_path = Path(path)
    if not journal_path.is_file():
        raise ScorecardError(f"journal not found at {journal_path}")
    loaded: JournalLoadResult = load_events(journal_path)
    if loaded.discarded_line_indices:
        joined = ", ".join(str(i) for i in loaded.discarded_line_indices)
        raise ScorecardError(
            f"refusing to scorecard {journal_path}: reader discarded physical line(s) "
            f"{joined}; the journal tail is torn or truncated and any number reported "
            "from it would be a silent undercount"
        )
    run_id = journal_path.parent.name
    return derive_scorecard(loaded.events, run_id=run_id)
