"""Schema-validated canonical event-type catalogue for tracker ingest (#5132).

Ingest parsers map raw source event names through this catalogue only.
Unknown names resolve to an explicit ``unmapped`` canonical entry and are
counted. The catalogue's content hash is available for journal rows that
recorded a classification decision.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import Counter
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

#: Packaged catalogue next to this module (ships in the wheel).
DEFAULT_CATALOGUE_PATH: Path = Path(__file__).with_name("event_catalogue.yaml")

#: Sentinel shape for GitLab note payloads (issue nested under ``issue``).
SHAPE_NOTE: Literal["note"] = "note"
SHAPE_DEFAULT: Literal["default"] = "default"

ParseShape = Literal["default", "note"]


class EventCatalogueError(ValueError):
    """Raised when the catalogue file is missing or fails validation."""


class EventCatalogueEntry(BaseModel):
    """One source-event → canonical mapping for a tracker adapter."""

    model_config = ConfigDict(extra="forbid")

    adapter: str = Field(min_length=1)
    source_event: str = Field(min_length=1)
    canonical: str = Field(min_length=1)
    shape: ParseShape = SHAPE_DEFAULT

    @field_validator("adapter", "source_event", "canonical")
    @classmethod
    def _strip_nonempty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be a non-empty string")
        return cleaned


class EventCatalogue(BaseModel):
    """Validated catalogue of canonical ingest event types."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    unmapped_canonical: str = Field(default="unmapped", min_length=1)
    entries: list[EventCatalogueEntry] = Field(min_length=1)

    @field_validator("unmapped_canonical")
    @classmethod
    def _strip_unmapped(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("unmapped_canonical must be a non-empty string")
        return cleaned

    @model_validator(mode="after")
    def _unique_adapter_source_pairs(self) -> EventCatalogue:
        seen: set[tuple[str, str]] = set()
        for entry in self.entries:
            key = (entry.adapter, entry.source_event)
            if key in seen:
                raise ValueError(
                    f"duplicate catalogue entry for adapter={entry.adapter!r} "
                    f"source_event={entry.source_event!r}"
                )
            seen.add(key)
        return self

    def lookup(self, adapter: str, source_event: str) -> EventCatalogueEntry | None:
        """Return the entry for ``(adapter, source_event)``, or ``None``."""

        for entry in self.entries:
            if entry.adapter == adapter and entry.source_event == source_event:
                return entry
        return None


def catalogue_content_hash(catalogue: EventCatalogue) -> str:
    """SHA-256 of the catalogue's canonical JSON projection.

    Mirrors :func:`bernstein.core.persistence.work_ledger.compute_entry_hash`'s
    sort-keys / compact-separators approach so the digest is stable across
    processes.
    """

    document = catalogue.model_dump(mode="json")
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_event_catalogue(path: Path | None = None) -> EventCatalogue:
    """Load and validate the event-type catalogue from ``path``.

    Args:
        path: Catalogue YAML path. Defaults to the packaged file beside
            this module.

    Returns:
        A validated :class:`EventCatalogue`.

    Raises:
        EventCatalogueError: If the file is missing or malformed.
    """

    catalogue_path = path or DEFAULT_CATALOGUE_PATH
    if not catalogue_path.exists():
        raise EventCatalogueError(f"event catalogue not found: {catalogue_path}")
    try:
        raw: object = yaml.safe_load(catalogue_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EventCatalogueError(f"malformed event catalogue YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise EventCatalogueError(
            f"event catalogue must be a mapping, got {type(raw).__name__}"
        )
    try:
        return EventCatalogue.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError + ValueError
        raise EventCatalogueError(f"event catalogue failed validation: {exc}") from exc


class EventCatalogueRuntime:
    """Process-wide catalogue handle with unmapped counters and content hash."""

    def __init__(self, catalogue: EventCatalogue) -> None:
        self._catalogue = catalogue
        self._content_hash = catalogue_content_hash(catalogue)
        self._lock = threading.Lock()
        self._unmapped_counts: Counter[str] = Counter()

    @property
    def catalogue(self) -> EventCatalogue:
        return self._catalogue

    @property
    def content_hash(self) -> str:
        return self._content_hash

    @property
    def unmapped_canonical(self) -> str:
        return self._catalogue.unmapped_canonical

    def resolve(self, adapter: str, source_event: str) -> EventCatalogueEntry | None:
        """Look up ``source_event`` for ``adapter``; ``None`` means unmapped."""

        return self._catalogue.lookup(adapter, source_event)

    def record_unmapped(self, adapter: str, source_event: str) -> str:
        """Count an unmapped source event and return the unmapped canonical id.

        Logs once on the first sight of each ``(adapter, source_event)`` pair
        so an unrecognised event is visible in production, not only countable
        in-process.
        """

        key = f"{adapter}:{source_event}"
        with self._lock:
            previous = self._unmapped_counts[key]
            self._unmapped_counts[key] = previous + 1
            first_sight = previous == 0
        if first_sight:
            logger.info(
                "Unmapped tracker source event adapter=%s source_event=%s "
                "(canonical=%s); further occurrences of this pair are counted silently",
                adapter,
                source_event,
                self._catalogue.unmapped_canonical,
            )
        return self._catalogue.unmapped_canonical

    def unmapped_counts(self) -> dict[str, int]:
        """Return a snapshot of ``adapter:source_event`` → count."""

        with self._lock:
            return dict(self._unmapped_counts)


_runtime_lock = threading.Lock()
_runtime: EventCatalogueRuntime | None = None


def get_event_catalogue_runtime() -> EventCatalogueRuntime:
    """Return the process-wide catalogue runtime, loading it once at first use."""

    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = EventCatalogueRuntime(load_event_catalogue())
        return _runtime


def reset_event_catalogue_runtime(
    catalogue: EventCatalogue | None = None,
    *,
    path: Path | None = None,
) -> EventCatalogueRuntime:
    """Replace the process-wide runtime (tests / explicit reload).

    When both ``catalogue`` and ``path`` are omitted, reloads the packaged
    default file.
    """

    global _runtime
    loaded = catalogue if catalogue is not None else load_event_catalogue(path)
    with _runtime_lock:
        _runtime = EventCatalogueRuntime(loaded)
        return _runtime


__all__ = [
    "DEFAULT_CATALOGUE_PATH",
    "SHAPE_DEFAULT",
    "SHAPE_NOTE",
    "EventCatalogue",
    "EventCatalogueEntry",
    "EventCatalogueError",
    "EventCatalogueRuntime",
    "catalogue_content_hash",
    "get_event_catalogue_runtime",
    "load_event_catalogue",
    "reset_event_catalogue_runtime",
]
