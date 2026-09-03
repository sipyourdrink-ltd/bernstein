"""Ingest adapter contract and event-type constants."""

from __future__ import annotations

from dataclasses import dataclass

INGEST_EVENT_TYPES = ("gen_ai_activity", "untyped_activity")


@dataclass(frozen=True, slots=True)
class IngestAdapterDeclaration:
    """Declaration of an ingest adapter's identity and supported event types.

    Attributes:
        name: Short stable identifier for this ingest adapter.
        version: Version string of the adapter.
        declared_event_types: Tuple of event-type names this adapter
            declares it can receive. Must be a subset of INGEST_EVENT_TYPES.
        summary: One-line human-readable description.
    """

    name: str
    version: str
    declared_event_types: tuple[str, ...]
    summary: str = ""
