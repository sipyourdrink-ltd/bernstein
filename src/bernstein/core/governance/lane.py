"""Reconciliation lanes: named, prioritised work channels (#5120).

A *lane* is a first-class, named queue for reconciliation work.  Operators
define lanes to separate high-priority drift corrections from low-priority
routine sweeps, capping concurrency independently and targeting different
agent pools.

This module provides:

* :class:`LaneManifest` -- an immutable, canonicalisable description of one
  lane.  Its identity is its :attr:`LaneManifest.lane_hash` (SHA-256 of the
  canonical JSON), following the same ``canonical_json + sha256`` pattern used
  by :mod:`bernstein.core.sandbox.pool` and
  :mod:`bernstein.core.config.manifest`.

* :class:`LaneStore` -- a lightweight CRUD store backed by a single JSON file,
  modelled on :class:`~bernstein.core.security.quarantine.QuarantineStore`.
  All mutations are written atomically so a crash between two lane updates
  never leaves the file in a half-written state.

Typical use::

    from pathlib import Path
    from bernstein.core.governance.lane import LaneManifest, LaneStore

    store = LaneStore(Path(".sdd/runtime/lanes.json"))
    store.put(LaneManifest(
        lane_id="urgent",
        priority=10,
        max_concurrency=2,
        target_class="drift-correction",
        created_at="2025-01-01T00:00:00Z",
    ))
    lane = store.get("urgent")
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.atomic_write import write_atomic_json

if TYPE_CHECKING:
    from pathlib import Path

#: Wire-format version stamped into every lane manifest.
LANE_MANIFEST_SCHEMA_VERSION: int = 1

#: Maximum allowed concurrency for a single lane.
MAX_LANE_CONCURRENCY: int = 64

#: Priority range: higher values run first.
MIN_PRIORITY: int = 0
MAX_PRIORITY: int = 100


class LaneError(ValueError):
    """Raised when a :class:`LaneManifest` is constructed with invalid fields."""


def _canonical_json(obj: Any) -> bytes:
    """Return sorted, compact UTF-8 JSON bytes for *obj*."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True, slots=True)
class LaneManifest:
    """Immutable description of one reconciliation lane.

    Attributes:
        lane_id: Unique identifier for this lane (non-empty, no whitespace).
        priority: Scheduling priority; higher values are processed first.
            Must be in ``[0, 100]``.
        max_concurrency: Maximum number of items processed simultaneously in
            this lane.  Must be in ``[1, 64]``.
        target_class: Optional agent class or pool name this lane routes work
            to.  Empty string means ``"default"``.
        created_at: ISO 8601 timestamp of lane creation.
    """

    lane_id: str
    priority: int
    max_concurrency: int
    target_class: str
    created_at: str

    def __post_init__(self) -> None:
        if not self.lane_id or any(c.isspace() for c in self.lane_id):
            raise LaneError(f"lane_id must be a non-empty string without whitespace; got {self.lane_id!r}")
        if not (MIN_PRIORITY <= self.priority <= MAX_PRIORITY):
            raise LaneError(f"priority must be in [{MIN_PRIORITY}, {MAX_PRIORITY}]; got {self.priority}")
        if not (1 <= self.max_concurrency <= MAX_LANE_CONCURRENCY):
            raise LaneError(f"max_concurrency must be in [1, {MAX_LANE_CONCURRENCY}]; got {self.max_concurrency}")
        if not self.created_at:
            raise LaneError("created_at must be a non-empty ISO 8601 timestamp")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict of this manifest."""
        return {
            "schema_version": LANE_MANIFEST_SCHEMA_VERSION,
            "lane_id": self.lane_id,
            "priority": self.priority,
            "max_concurrency": self.max_concurrency,
            "target_class": self.target_class,
            "created_at": self.created_at,
        }

    @property
    def lane_hash(self) -> str:
        """SHA-256 of the canonical JSON of this manifest."""
        return hashlib.sha256(_canonical_json(self.to_dict())).hexdigest()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LaneManifest:
        """Reconstruct a :class:`LaneManifest` from a serialised dict.

        Args:
            data: Dict as returned by :meth:`to_dict`.

        Returns:
            The reconstructed :class:`LaneManifest`.

        Raises:
            LaneError: A required field is missing or has the wrong type.
        """
        try:
            return cls(
                lane_id=str(data["lane_id"]),
                priority=int(str(data["priority"])),
                max_concurrency=int(str(data["max_concurrency"])),
                target_class=str(data.get("target_class", "")),
                created_at=str(data["created_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise LaneError(f"invalid lane manifest: {exc}") from exc


class LaneStore:
    """Persistent CRUD store for :class:`LaneManifest` objects.

    All mutations are written atomically to avoid a torn-write window where
    the lane file is empty between a truncate and a rewrite.

    Args:
        path: Full path to the lane JSON file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def all(self) -> list[LaneManifest]:
        """Return all stored lane manifests.

        Returns:
            List of :class:`LaneManifest` objects sorted by descending priority,
            then ascending ``lane_id``.  Empty list if the file does not exist.
        """
        if not self._path.exists():
            return []
        try:
            raw: list[dict[str, Any]] = json.loads(self._path.read_text(encoding="utf-8"))
            manifests = [LaneManifest.from_dict(item) for item in raw]
        except (json.JSONDecodeError, LaneError):
            return []
        return sorted(manifests, key=lambda m: (-m.priority, m.lane_id))

    def get(self, lane_id: str) -> LaneManifest | None:
        """Return the manifest for *lane_id*, or ``None`` if absent.

        Args:
            lane_id: Lane identifier to look up.

        Returns:
            The matching :class:`LaneManifest`, or ``None``.
        """
        for manifest in self.all():
            if manifest.lane_id == lane_id:
                return manifest
        return None

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def _save(self, manifests: list[LaneManifest]) -> None:
        write_atomic_json(self._path, [m.to_dict() for m in manifests])

    def put(self, manifest: LaneManifest) -> None:
        """Create or replace the lane manifest for *manifest.lane_id*.

        Args:
            manifest: The manifest to store.
        """
        existing = [m for m in self.all() if m.lane_id != manifest.lane_id]
        existing.append(manifest)
        self._save(existing)

    def delete(self, lane_id: str) -> bool:
        """Remove the manifest for *lane_id*.

        Args:
            lane_id: Lane identifier to remove.

        Returns:
            ``True`` if the lane was present and removed, ``False`` if absent.
        """
        before = self.all()
        after = [m for m in before if m.lane_id != lane_id]
        if len(before) == len(after):
            return False
        self._save(after)
        return True
