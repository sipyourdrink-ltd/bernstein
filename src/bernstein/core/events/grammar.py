"""Canonical event grammar - a deterministic projection over the audit chain.

Eventing v2 (#2548). Bernstein historically recorded what happened in five
parallel event systems that never correlated: the SSE stream, ``TriggerEvent``,
the HMAC audit chain, the per-step journal, and the telemetry taxonomy. This
module defines one canonical grammar and the pure projection function that
derives a :class:`CanonicalEvent` from a single audit-chain entry, so the thing
operators query and act on is the chain itself rather than a sixth event bus.

Grammar
-------
A canonical event is the tuple

    (resource id, dot-delimited label path, related resource ids, payload digest)

plus the audit entry's own chain identity (``hmac`` / ``prev_hmac``) and its
ordering fields. Deriving it from an audit entry is a *pure function*: two hosts
holding the same on-disk chain bytes project byte-identical canonical events,
because every output field is either copied from the entry or a deterministic
hash of the entry's own bytes.

The related-resource edges are the lineage spine anchors carried inside the
entry's ``details`` (``lineage_parents`` / ``related_resource_ids`` plus the
standard context ids). Those edges are what the composable trigger layer walks
to decide "does this later event descend from that earlier one" without ever
consulting a wall clock.

Mapping tables
--------------
The audit chain already labels its entries with dot-delimited ``event_type``
strings, so they are canonical labels as-is. The other four taxonomies are
folded into the same grammar through the mapping tables below, so an operator
addressing ``task.completed`` reaches the SSE emission, the audit entry, and the
journal step under one label.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, cast

#: The genesis ``prev_hmac`` value used by the audit chain (see
#: :mod:`bernstein.core.security.audit`). Re-declared here to keep this module
#: import-light; a mismatch would surface immediately in the projection tests.
GENESIS_HMAC = "0" * 64

#: Grammar version stamped into a canonical event's serialised form. Bump only
#: on a wire-format change; verifiers reject unknown versions.
GRAMMAR_VERSION = 1

# ---------------------------------------------------------------------------
# Mapping tables: native taxonomy -> canonical dot-delimited label
# ---------------------------------------------------------------------------

#: SSE event-type identifiers already use dot-delimited paths, so the mapping is
#: mostly identity; it exists so a consumer can resolve an SSE emission to its
#: canonical label without hard-coding the string.
SSE_LABEL_MAP: dict[str, str] = {
    "task.created": "task.created",
    "task.claimed": "task.claimed",
    "task.completed": "task.completed",
    "task.failed": "task.failed",
    "task.retried": "task.retried",
    "agent.spawned": "agent.spawned",
    "agent.exited": "agent.exited",
    "gate.result": "gate.result",
    "cost.update": "cost.update",
    "merge.started": "merge.started",
    "merge.completed": "merge.completed",
    "run.started": "run.started",
    "run.completed": "run.completed",
    "heartbeat": "heartbeat",
}

#: Trigger sources are single tokens (``github``, ``slack``, ``cron`` ...); they
#: fold under the ``trigger.<source>`` namespace so an inbound trigger and the
#: task it spawns share one addressable prefix.
TRIGGER_SOURCE_LABEL_PREFIX = "trigger"

#: Journal steps carry a ``kind`` token; they fold under ``journal.step.<kind>``
#: so a semantic milestone (a step advancing) is addressable as a feed label.
JOURNAL_STEP_LABEL_PREFIX = "journal.step"

#: Keys inside an audit entry's ``details`` that name a lineage ancestor. Order
#: is fixed so projection stays deterministic; values are de-duplicated and
#: sorted in the output.
_ANCESTOR_LIST_KEYS = ("lineage_parents", "related_resource_ids")
_ANCESTOR_SCALAR_KEYS = (
    "parent_resource_id",
    "run_id",
    "task_id",
    "plan_id",
    "journal_entry_hash",
)


def canonical_label_for_sse(sse_type: str) -> str:
    """Return the canonical label for an SSE event-type identifier."""
    return SSE_LABEL_MAP.get(sse_type, sse_type)


def canonical_label_for_trigger_source(source: str) -> str:
    """Return the canonical label for a normalized trigger source token."""
    return f"{TRIGGER_SOURCE_LABEL_PREFIX}.{source}"


def canonical_label_for_journal_step(kind: str) -> str:
    """Return the canonical label for a journal step kind."""
    return f"{JOURNAL_STEP_LABEL_PREFIX}.{kind}"


def canonical_details_digest(details: dict[str, Any]) -> str:
    """Return the ``sha256:`` digest of an entry's ``details`` payload.

    The pre-image is canonical JSON (sorted keys, minimal separators, UTF-8),
    so the digest is stable across processes and platforms. The digest binds the
    payload without copying it into the feed, and it is what a fire receipt and a
    render template commit to.
    """
    preimage = json.dumps(details, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(preimage).hexdigest()


def _derive_related_resource_ids(resource_id: str, details: dict[str, Any]) -> tuple[str, ...]:
    """Derive the sorted, de-duplicated lineage ancestor ids from ``details``.

    The ancestor edges are the lineage spine anchors the entry carried. The
    entry's own ``resource_id`` is never listed as its own ancestor. Output is
    sorted so the projection is deterministic regardless of dict order.
    """
    found: set[str] = set()
    for key in _ANCESTOR_LIST_KEYS:
        raw = details.get(key)
        if isinstance(raw, (list, tuple)):
            for item in cast("list[Any]", raw):
                if isinstance(item, str) and item and item != resource_id:
                    found.add(item)
    for key in _ANCESTOR_SCALAR_KEYS:
        raw = details.get(key)
        if isinstance(raw, str) and raw and raw != resource_id:
            found.add(raw)
    return tuple(sorted(found))


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    """One event in the canonical grammar - a projection of an audit entry.

    Attributes:
        position: Zero-based index of the event within its projected window.
            An ordering aid only; the stable chain identity is ``hmac``.
        hmac: The audit entry's HMAC. This is the event's chain-position
            identity: unique, and pinned in the chain by ``prev_hmac`` linkage.
        prev_hmac: The predecessor entry's HMAC. Deleting or reordering any
            event breaks this linkage, which is how a window proves completeness
            and order offline.
        resource_id: The primary resource the event concerns.
        label: Dot-delimited label path (for example ``task.completed``).
        related_resource_ids: Sorted lineage ancestor ids - the edges the
            sequence layer walks to prove descent.
        payload_digest: ``sha256:`` digest of the entry's ``details``.
        timestamp: ISO 8601 timestamp copied from the entry.
        actor: The entry's actor.
    """

    position: int
    hmac: str
    prev_hmac: str
    resource_id: str
    label: str
    related_resource_ids: tuple[str, ...]
    payload_digest: str
    timestamp: str
    actor: str

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the canonical serialisable mapping for this event.

        ``position`` is intentionally excluded from the canonical body: it is a
        window-relative ordering aid, not part of the event's chain identity, so
        two windows that overlap project byte-identical bodies for shared events.
        """
        return {
            "v": GRAMMAR_VERSION,
            "hmac": self.hmac,
            "prev_hmac": self.prev_hmac,
            "resource_id": self.resource_id,
            "label": self.label,
            "related_resource_ids": list(self.related_resource_ids),
            "payload_digest": self.payload_digest,
            "timestamp": self.timestamp,
            "actor": self.actor,
        }

    def canonical_line(self) -> str:
        """Return the canonical single-line JSON form (no trailing newline)."""
        return json.dumps(self.to_canonical_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_canonical_dict(cls, raw: dict[str, Any], *, position: int) -> CanonicalEvent:
        """Rebuild a canonical event from its serialised body.

        Used by the offline verifier to reconstruct a window from a stored
        envelope without re-reading the audit chain. ``position`` is supplied by
        the caller because it is a window-relative aid, not part of the body.
        """
        related_raw = raw.get("related_resource_ids", [])
        related = (
            tuple(str(item) for item in cast("list[Any]", related_raw))
            if isinstance(related_raw, (list, tuple))
            else ()
        )
        return cls(
            position=position,
            hmac=str(raw.get("hmac", "")),
            prev_hmac=str(raw.get("prev_hmac", GENESIS_HMAC)),
            resource_id=str(raw.get("resource_id", "")),
            label=str(raw.get("label", "")),
            related_resource_ids=related,
            payload_digest=str(raw.get("payload_digest", "")),
            timestamp=str(raw.get("timestamp", "")),
            actor=str(raw.get("actor", "")),
        )


def project_entry(entry: dict[str, Any], *, position: int) -> CanonicalEvent:
    """Project a single audit-chain entry into a :class:`CanonicalEvent`.

    This is a pure function of the entry bytes: the same on-disk entry projects
    the same canonical event on every host.

    Args:
        entry: The raw audit-log dict as read from ``.sdd/audit/*.jsonl`` -
            carrying ``event_type``, ``resource_id``, ``details``, ``hmac`` and
            ``prev_hmac``.
        position: Zero-based index of this entry within the window being
            projected.

    Returns:
        The canonical event.
    """
    details_raw = entry.get("details")
    details: dict[str, Any] = cast("dict[str, Any]", details_raw) if isinstance(details_raw, dict) else {}
    resource_id = str(entry.get("resource_id", ""))
    return CanonicalEvent(
        position=position,
        hmac=str(entry.get("hmac", "")),
        prev_hmac=str(entry.get("prev_hmac", GENESIS_HMAC)),
        resource_id=resource_id,
        label=str(entry.get("event_type", "")),
        related_resource_ids=_derive_related_resource_ids(resource_id, details),
        payload_digest=canonical_details_digest(details),
        timestamp=str(entry.get("timestamp", "")),
        actor=str(entry.get("actor", "")),
    )


__all__ = [
    "GENESIS_HMAC",
    "GRAMMAR_VERSION",
    "JOURNAL_STEP_LABEL_PREFIX",
    "SSE_LABEL_MAP",
    "TRIGGER_SOURCE_LABEL_PREFIX",
    "CanonicalEvent",
    "canonical_details_digest",
    "canonical_label_for_journal_step",
    "canonical_label_for_sse",
    "canonical_label_for_trigger_source",
    "project_entry",
]
