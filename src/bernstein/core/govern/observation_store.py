"""The persistent observation store for ``govern discover`` (#5083).

The :class:`~bernstein.core.govern.observation.ObservationLedger` from #5082
holds one pass's envelopes in memory. ``govern discover`` runs repeatedly
against the same targets, so existence in the inventory can only mean
"re-observed recently": without a persistent ``observed_at`` and a sweep, an
entity unplugged six months ago looks identical to one seen five minutes ago.

This module holds the store that gives those envelopes duration. Every
observation upserts under the stable entity id and refreshes
``observed_at``; a sweep moves entities not re-observed within a configurable
window to a ``tombstoned`` state -- nothing is ever hard-deleted, so "when did
we stop seeing X" and "did X come back" stay answerable from the journal alone.

Tombstones live as a ``state`` field on the same entity record rather than in
a physically separate file the way ``cache_eviction.TombstoneStore`` separates
its ``tombstones.jsonl``: the inventory selector (#5116) needs to query live
and tombstoned entities from one store, and a record-level state keeps that
one query. The trade-off is that a tombstoned record still occupies the same
file its live state did -- deliberate, since restoration is the common path
for flapping entities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JsonSchemaValidationError

from bernstein.core.govern.observation import ObservationEnvelope
from bernstein.core.persistence.atomic_write import write_atomic_text
from bernstein.core.persistence.file_locks import cross_process_lock

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Sentinel age for an envelope whose ``observed_at`` cannot be parsed.
#: Fail-closed: a sweep that cannot tell how old an observation is treats it
#: as older than any TTL, so it moves to the tombstone partition rather than
#: staying live on a guess.
_UNPARSEABLE_OBSERVED_AT = datetime.min.replace(tzinfo=UTC)

_ENTITY_ID_PATTERN = r"^entity:[0-9a-f]{32}$"
_ENTITY_ID_PREFIX = "entity:"


class ObservationStoreError(ValueError):
    """Raised when an observation record cannot be read or does not validate."""


class RecordState(StrEnum):
    """Lifecycle states of one entity's observation record."""

    LIVE = "live"
    TOMBSTONED = "tombstoned"


_RECORD_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["entity_id", "state", "envelope"],
    "properties": {
        "entity_id": {"type": "string", "pattern": _ENTITY_ID_PATTERN},
        "state": {"enum": [RecordState.LIVE, RecordState.TOMBSTONED]},
        "envelope": {"type": "object"},
    },
}

_RECORD_VALIDATOR: Any = Draft202012Validator(_RECORD_SCHEMA)


def observation_store_root(workdir: Path) -> Path:
    """Return the observation store root for *workdir*.

    The store sits under ``.sdd/govern/inventory/observations/`` beside the
    identity store from #5129, so every governance artifact stays under one
    root.
    """
    return Path(workdir) / ".sdd" / "govern" / "inventory" / "observations"


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """One entity's latest observation plus its lifecycle state.

    Attributes:
        entity_id: Stable entity id the envelope carries.
        state: ``live`` or ``tombstoned``. A tombstoned record is still
            readable -- tombstoning demotes, it never deletes.
        envelope: The most recent observation of this entity.
    """

    entity_id: str
    state: RecordState
    envelope: ObservationEnvelope

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "entity_id": self.entity_id,
            "state": str(self.state),
            "envelope": self.envelope.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ObservationRecord:
        """Rebuild a record from a serialized dict."""
        return cls(
            entity_id=str(raw["entity_id"]),
            state=RecordState(str(raw["state"])),
            envelope=ObservationEnvelope.from_dict(dict(raw["envelope"])),
        )


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One journaled transition of one entity's record.

    Every tombstone and restoration is journaled, so "when did we stop seeing
    X" and "did X come back" are answerable from the record alone, without
    diffing snapshots by hand.
    """

    entity_id: str
    transition: str
    swept_at: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "entity_id": self.entity_id,
            "transition": self.transition,
            "swept_at": self.swept_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> JournalEntry:
        """Rebuild a journal entry from its serialized row."""
        return cls(
            entity_id=str(raw["entity_id"]),
            transition=str(raw["transition"]),
            swept_at=str(raw["swept_at"]),
            reason=str(raw["reason"]),
        )


def _lock_path_for(path: Path) -> Path:
    """Return the sibling advisory lock path guarding ``path``."""
    return path.with_name(path.name + ".lock")


def _journal_row(entry: JournalEntry) -> str:
    """Canonical JSON of *entry*, so journal bytes are order-independent."""
    return json.dumps(
        entry.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _parse_observed_at(value: str) -> datetime:
    """Parse an envelope's ISO-8601 ``observed_at``.

    A value that does not parse reads as the sentinel minimum age, per the
    module's fail-closed rule.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _UNPARSEABLE_OBSERVED_AT
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class ObservationStore:
    """Entity-per-file persistence for observation envelopes.

    One JSON file per entity id under ``entities/``, plus an append-only
    ``journal.jsonl`` holding every tombstone and restoration. The shape
    follows the identity store from #5129: one file per entity, sorted-key
    JSON, and schema validation on load so a hand-edited file fails loudly
    rather than poisoning the inventory.

    Two overlapping passes over an unchanged fixture converge to the same
    bytes: records are keyed by entity id (duplicates cannot accumulate), the
    envelope is the last observation seen, and the journal only grows on
    actual transitions. Re-ingesting a byte-identical envelope leaves both
    the record and the journal untouched, so a repeated pass is not churn.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """Directory holding ``entities/`` and ``journal.jsonl``."""
        return self._root

    def entity_path(self, entity_id: str) -> Path:
        """Return the path of the file holding *entity_id*."""
        if not entity_id.startswith(_ENTITY_ID_PREFIX):
            raise ObservationStoreError(f"not an entity id: {entity_id!r}")
        return self._root / "entities" / f"{entity_id.removeprefix(_ENTITY_ID_PREFIX)}.json"

    # -- read -----------------------------------------------------------

    def load(self, entity_id: str) -> ObservationRecord:
        """Load and validate one entity's record.

        Raises:
            ObservationStoreError: The file is absent, is not JSON, or does
                not satisfy the record schema.
        """
        path = self.entity_path(entity_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ObservationStoreError(f"cannot read observation {entity_id}: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ObservationStoreError(f"observation {entity_id} is not valid JSON: {exc}") from exc
        self._validate(payload, entity_id)
        return ObservationRecord.from_dict(dict(payload))

    def load_or_none(self, entity_id: str) -> ObservationRecord | None:
        """Return the record for *entity_id*, or ``None`` if never observed."""
        try:
            return self.load(entity_id)
        except ObservationStoreError:
            return None

    def entity_ids(self) -> tuple[str, ...]:
        """Return every entity id in the store, sorted."""
        entities = self._root / "entities"
        if not entities.is_dir():
            return ()
        return tuple(sorted(f"entity:{p.stem}" for p in entities.glob("*.json")))

    def load_all(self) -> dict[str, ObservationRecord]:
        """Return ``entity_id -> record`` for every entity in the store."""
        return {entity_id: self.load(entity_id) for entity_id in self.entity_ids()}

    def journal(self) -> tuple[JournalEntry, ...]:
        """Return every journal entry, in the order recorded."""
        path = self._root / "journal.jsonl"
        if not path.is_file():
            return ()
        return tuple(
            JournalEntry.from_dict(json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    # -- write ----------------------------------------------------------

    def upsert(self, envelope: ObservationEnvelope) -> str:
        """Store *envelope* under its stable entity id, refreshing recency.

        A first sighting creates the record; a repeat refreshes
        ``observed_at`` under the same id instead of appending a second
        entry. An entity seen again while tombstoned moves back to ``live``,
        and that restoration is journaled.

        Returns:
            The transition this upsert caused: ``"created"``, ``"refreshed"``
            or ``"restored"``.
        """
        with cross_process_lock(_lock_path_for(self._root)):
            existing = self.load_or_none(envelope.entity_id)
            if existing is None:
                record = ObservationRecord(
                    entity_id=envelope.entity_id,
                    state=RecordState.LIVE,
                    envelope=envelope,
                )
                transition = "created"
            elif existing.state is RecordState.TOMBSTONED:
                record = ObservationRecord(
                    entity_id=envelope.entity_id,
                    state=RecordState.LIVE,
                    envelope=envelope,
                )
                transition = "restored"
                self._append_journal(
                    [
                        JournalEntry(
                            entity_id=envelope.entity_id,
                            transition="restore",
                            swept_at=envelope.observed_at,
                            reason="re-observed while tombstoned",
                        ),
                    ]
                )
            else:
                record = ObservationRecord(
                    entity_id=envelope.entity_id,
                    state=existing.state,
                    envelope=envelope,
                )
                transition = "refreshed"
            self._write_record(self.entity_path(envelope.entity_id), record)
            return transition

    def sweep(self, *, ttl_seconds: float, now: datetime | None = None) -> int:
        """Demote entities not re-observed within the TTL to tombstoned.

        Every moved entity is journaled with the transition and the reason,
        nothing is deleted, and the return value is the count moved -- the
        number a discovery run reports.
        """
        reference = datetime.now(tz=UTC) if now is None else now
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        moved: list[JournalEntry] = []
        demotions: list[tuple[Path, ObservationRecord]] = []
        with cross_process_lock(_lock_path_for(self._root)):
            for entity_id in self.entity_ids():
                record = self.load(entity_id)
                if record.state is not RecordState.LIVE:
                    continue
                age = (reference - _parse_observed_at(record.envelope.observed_at)).total_seconds()
                if age <= ttl_seconds:
                    continue
                demotions.append(
                    (
                        self.entity_path(entity_id),
                        ObservationRecord(
                            entity_id=entity_id,
                            state=RecordState.TOMBSTONED,
                            envelope=record.envelope,
                        ),
                    )
                )
                moved.append(
                    JournalEntry(
                        entity_id=entity_id,
                        transition="tombstone",
                        swept_at=reference.isoformat(),
                        reason=f"last observed {record.envelope.observed_at}, older than TTL {ttl_seconds}s",
                    )
                )
            if not moved:
                return 0
            for path, record in demotions:
                self._write_record(path, record)
            self._append_journal(moved)
        return len(moved)

    # -- internals --------------------------------------------------------

    def _write_record(self, path: Path, record: ObservationRecord) -> None:
        payload = record.to_dict()
        self._validate(payload, record.entity_id)
        _write_json(path, payload)

    def _append_journal(self, entries: Sequence[JournalEntry]) -> None:
        """Persist *entries* to the journal. Caller holds the store lock.

        Every row is serialised before any byte is written, then the journal
        is rewritten whole (prior rows plus the new ones) with one atomic
        replace, so an interrupted sweep cannot publish a prefix of the moved
        set. The shape copies ``cache_eviction.TombstoneStore._commit``
        (different subsystem, same crash-consistency problem).
        """
        rows = [_journal_row(entry) for entry in entries]
        if not rows:
            return
        self._root.mkdir(parents=True, exist_ok=True)
        journal_path = self._root / "journal.jsonl"
        existing = journal_path.read_text(encoding="utf-8") if journal_path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        write_atomic_text(journal_path, existing + "".join(row + "\n" for row in rows))

    def _validate(self, payload: Any, entity_id: str) -> None:
        raw = cast("list[JsonSchemaValidationError]", list(_RECORD_VALIDATOR.iter_errors(payload)))
        errors = sorted(raw, key=lambda e: list(e.path))
        if errors:
            first = errors[0]
            location = "/".join(str(p) for p in first.path) or "<root>"
            raise ObservationStoreError(f"observation {entity_id} invalid at {location}: {first.message}")


__all__ = [
    "JournalEntry",
    "ObservationRecord",
    "ObservationStore",
    "ObservationStoreError",
    "RecordState",
    "observation_store_root",
]
