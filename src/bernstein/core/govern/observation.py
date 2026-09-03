"""The observation envelope for ``govern discover`` (#5082).

``inventory_models.Surface`` models one surface as a single flat value with a
single evidence reference, because that is what the posture diff in
``govern plan`` needs: one surface really is one value there. ``govern
discover`` observes whole entities with many attributes at once, and a
collector that collected three of an entity's five attributes and failed on
two has nowhere to put that fact in a ``Surface``.

This module holds the model every collector emits instead: one
:class:`ObservationEnvelope` per entity, carrying the collected payload, the
moment of observation, and an ``errors`` map that names why each missing
field is absent. A partial observation is still worth having, provided the
reader can tell "not collected, here's why" from "collected, value is empty"
-- that distinction is what makes a later query ("which targets are missing
X") trustworthy instead of ambiguous.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

from bernstein.core.govern.identity_join import entity_id_for

#: Length of the short-hash suffix that disambiguates a colliding entity id,
#: in the house shape ``<entity_id>-<6-hex>``.
_COLLISION_HASH_LEN = 6


@dataclass(frozen=True, slots=True)
class ObservationEnvelope:
    """One collector's complete observation of one entity.

    Attributes:
        entity_id: Stable id derived from the canonical join key field and
            normalised key via ``identity_join.entity_id_for``, so the same
            entity observed under two hostnames carries one id across passes.
        entity_class: What kind of entity this is (e.g. ``host``,
            ``mcp_server``, ``model_endpoint``). It describes the envelope;
            the canonical join key field names the entity-id domain.
        payload: The fields the collector did collect. A field absent here
            is "not collected"; "collected, value is empty" is a present
            field whose value is empty, and the two are never confused.
        observed_at: When the observation was taken, ISO-8601.
        evidence_ref: Reference to the collection evidence, in the house
            shape ``Surface`` established.
        errors: Sub-probe id to the reason that probe's fields are missing
            from ``payload``. Keyed by sub-probe (matching #5081's probe
            ids) rather than by output field: one probe often produces
            several fields, and the reason a probe failed covers every
            field it would have produced. Which fields a probe produces is
            #5081's declaration, not this map's; a reader who needs the
            field-level view asks the probe registry. Every value must be
            a non-empty reason string -- a bare marker cannot say why a
            field is absent, and the distinction is the point of the map.
    """

    entity_id: str
    entity_class: str
    payload: Mapping[str, Any]
    observed_at: str
    evidence_ref: str
    errors: Mapping[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        if not self.entity_id or not self.entity_id.strip():
            raise ValueError("entity_id must be a non-empty string")
        if not self.entity_class or not self.entity_class.strip():
            raise ValueError("entity_class must be a non-empty string")
        for sub_probe, reason in self.errors.items():
            text = str(reason).strip()
            if not text:
                raise ValueError(f"error for sub-probe {sub_probe!r} must be a non-empty reason string")
            if text.lower() in {"true", "false"}:
                raise ValueError(f"error for sub-probe {sub_probe!r} must name the cause, not mark it")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        object.__setattr__(self, "errors", MappingProxyType(dict(self.errors)))

    @classmethod
    def for_entity(
        cls,
        *,
        entity_class: str,
        key_field: str,
        normalised_key: str,
        payload: Mapping[str, Any],
        observed_at: str,
        evidence_ref: str,
        errors: Mapping[str, str] | None = None,
    ) -> ObservationEnvelope:
        """Build the envelope for an entity keyed on *normalised_key*.

        The id is a pure function of the canonical join key field and the
        normalised key, matching the identity graph. A hostname is a payload
        field, not the key, so it can change without changing the entity id.
        """
        return cls(
            entity_id=entity_id_for(key_field, normalised_key),
            entity_class=entity_class,
            payload=payload,
            observed_at=observed_at,
            evidence_ref=evidence_ref,
            errors=errors or {},
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical serialization."""
        return {
            "entity_id": self.entity_id,
            "entity_class": self.entity_class,
            "payload": dict(self.payload),
            "observed_at": self.observed_at,
            "evidence_ref": self.evidence_ref,
            "errors": dict(self.errors),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ObservationEnvelope:
        """Rebuild an envelope from a serialized dict."""
        return cls(
            entity_id=str(raw["entity_id"]),
            entity_class=str(raw["entity_class"]),
            payload={str(k): v for k, v in raw.get("payload", {}).items()},
            observed_at=str(raw["observed_at"]),
            evidence_ref=str(raw["evidence_ref"]),
            errors={str(k): str(v) for k, v in raw.get("errors", {}).items()},
        )


@dataclass(frozen=True, slots=True)
class ObservationLedger:
    """In-memory ingestion and lookup over observation envelopes.

    The ledger is to envelopes what ``Inventory`` is to surfaces: a frozen
    snapshot a pass produced. Envelopes with a non-empty ``errors`` map are
    ingested like any other -- partial data is stored, not rejected.
    Byte-identical repeats are deduplicated; distinct envelopes sharing an
    entity id get disambiguated keys, never silently-shadowing one another.
    Persistence across runs is #5083's, not this one's.
    """

    envelopes: tuple[ObservationEnvelope, ...] = ()

    def ingest(self, envelope: ObservationEnvelope) -> ObservationLedger:
        """Return a new ledger with *envelope* ingested.

        Ingestion never rejects a partial observation: the whole point of
        the errors map is that a collector which failed on some probes
        still reports what it saw.
        """
        if envelope in self.envelopes:
            return self
        return ObservationLedger(envelopes=(*self.envelopes, envelope))

    def key_for(self, envelope: ObservationEnvelope) -> str:
        """Return *envelope*'s lookup key, disambiguated on collision.

        An id only one envelope holds keys plainly. When several envelopes
        share an id, each key carries a short hash of that envelope's
        canonical serialization, so both stay reachable and the keys do
        not depend on ingestion order.
        """
        collisions = sum(1 for e in self.envelopes if e.entity_id == envelope.entity_id)
        if collisions <= 1:
            return envelope.entity_id
        digest = hashlib.sha256(_canonical_bytes(envelope)).hexdigest()
        return f"{envelope.entity_id}-{digest[:_COLLISION_HASH_LEN]}"

    def keys_for(self, entity_id: str) -> tuple[str, ...]:
        """Return every lookup key holding an envelope with *entity_id*."""
        return tuple(self.key_for(e) for e in self.envelopes if e.entity_id == entity_id)

    def get(self, key: str) -> ObservationEnvelope | None:
        """Look up an envelope by its plain or disambiguated key.

        A collided id is not itself a key: using one would return an
        arbitrary envelope and re-create the silent shadowing this ledger
        exists to prevent, so an ambiguous id raises instead.
        """
        matching = [e for e in self.envelopes if e.entity_id == key]
        if len(matching) > 1:
            msg = f"entity id {key!r} is held by {len(matching)} envelopes; use one of {self.keys_for(key)}"
            raise ValueError(msg)
        for envelope in self.envelopes:
            if self.key_for(envelope) == key:
                return envelope
        return None

    def entity_ids(self) -> frozenset[str]:
        """Return the set of all entity ids, duplicates collapsed."""
        return frozenset(e.entity_id for e in self.envelopes)


def _canonical_bytes(envelope: ObservationEnvelope) -> bytes:
    """Canonical JSON of *envelope*, so a digest is order-independent."""
    return json.dumps(
        envelope.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = [
    "ObservationEnvelope",
    "ObservationLedger",
]
