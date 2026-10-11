"""Lane registry as a deterministic projection of the audit chain (#5120).

``lanes.py`` gave the lane record and the bootstrap decision (create-if-absent,
`registered`/`updated`/`unchanged`); this module is what makes that decision
answerable across process runs instead of just within one call to
``reconcile_lanes``. A lane's *active* hash lives in the HMAC audit chain --
``lane.registered`` / ``lane.updated`` / ``lane.retired`` events (see
:mod:`bernstein.core.security.audit_chain`) -- and :func:`project_lane_registry`
replays those events to rebuild the current ``{name: lane_hash}`` map. There is
no side table to drift out of agreement with the chain.

Manifest bodies live in a content-addressed store (:class:`LaneStore`), exactly
the shape :class:`~bernstein.core.sandbox.pool_registry.PoolStore` already
established: each body is written to ``<root>/lanes/<lane_hash>.json`` and
re-verified against its own canonical hash on load, so a tampered body is
self-detecting rather than silently trusted.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.govern.lanes import LaneError, LaneManifest

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_LANE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_EVENT_REGISTERED = "lane.registered"
_EVENT_UPDATED = "lane.updated"
_EVENT_RETIRED = "lane.retired"


class LaneStoreError(ValueError):
    """Raised when a content-addressed lane body is missing or tampered."""


def _event_field(event: Any, key: str) -> Any:
    """Read *key* from *event* whether it is a mapping or a dataclass."""
    if isinstance(event, Mapping):
        return event.get(key)
    return getattr(event, key, None)


def project_lane_registry(events: Iterable[Any]) -> dict[str, str]:
    """Return the active ``{lane_name: lane_hash}`` map from *events*.

    A pure function over the ordered ``lane.*`` event stream, mirroring
    :func:`bernstein.core.sandbox.pool_registry.project_pool_registry`.
    Non-lane events are ignored, so callers may pass the whole chain. Register
    and update set the active hash for the name; retire drops it. Replaying
    the same ordered events always yields the same map.

    Args:
        events: Ordered events, each either a mapping or an object exposing
            ``event_type`` and a ``details`` payload carrying ``lane_name`` /
            ``lane_hash``.

    Returns:
        Mapping from lane name to the currently active canonical ``lane_hash``.
    """
    active: dict[str, str] = {}
    for event in events:
        event_type = _event_field(event, "event_type")
        details = _event_field(event, "details") or {}
        name = details.get("lane_name")
        lane_hash = details.get("lane_hash")
        if not name:
            continue
        if event_type in (_EVENT_REGISTERED, _EVENT_UPDATED):
            if lane_hash:
                active[name] = lane_hash
        elif event_type == _EVENT_RETIRED:
            active.pop(name, None)
    return active


@dataclass(frozen=True)
class LaneStore:
    """Content-addressed store for lane manifest bodies.

    Bodies live under ``root/lanes/<lane_hash>.json``. The hash in the filename
    is the manifest's own canonical hash, so a load both locates the body and
    verifies it: a tampered body recomputes to a different hash and is refused.
    """

    root: Path

    @property
    def lanes_dir(self) -> Path:
        return self.root / "lanes"

    def _path_for(self, lane_hash: str) -> Path:
        """Return the on-disk path for *lane_hash*, guarded against traversal."""
        if not _LANE_HASH_RE.match(lane_hash):
            raise LaneStoreError(f"lane_hash is not a canonical sha256 digest: {lane_hash!r}")
        base = self.lanes_dir
        candidate = base / f"{lane_hash}.json"
        base_real = os.path.realpath(base)
        cand_real = os.path.realpath(candidate)
        if os.path.commonpath([base_real, cand_real]) != base_real:
            raise LaneStoreError(f"lane body path escapes the lanes directory: {lane_hash!r}")
        return candidate

    def put(self, manifest: LaneManifest) -> Path:
        """Write *manifest* to its content-addressed path and return it.

        Idempotent: re-writing an identical manifest is a no-op-equivalent
        (the bytes are byte-identical because the payload is canonical).
        """
        path = self._path_for(manifest.lane_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")), encoding="utf-8")
        return path

    def get(self, lane_hash: str) -> LaneManifest:
        """Load and hash-verify the manifest body for *lane_hash*.

        Raises:
            LaneStoreError: The body is absent, unreadable, or its recomputed
                canonical hash does not equal *lane_hash* (tampered).
        """
        path = self._path_for(lane_hash)
        if not path.is_file():
            raise LaneStoreError(f"no lane body for {lane_hash!r}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise LaneStoreError(f"unreadable lane body for {lane_hash!r}: {exc}") from exc
        try:
            manifest = LaneManifest.from_dict(data)
        except (LaneError, KeyError, ValueError, TypeError) as exc:
            # from_dict recomputes the hash and refuses a body whose embedded
            # lane_hash no longer matches its own content -- surface that as a
            # store-level tamper error, not a manifest construction error.
            raise LaneStoreError(f"lane body hash mismatch for {lane_hash!r} (tampered): {exc}") from exc
        if manifest.lane_hash != lane_hash:
            raise LaneStoreError(f"lane body hash mismatch for {lane_hash!r} (tampered)")
        return manifest

    def has(self, lane_hash: str) -> bool:
        """Return whether a body exists for *lane_hash* (no hash verification)."""
        try:
            return self._path_for(lane_hash).is_file()
        except LaneStoreError:
            return False


@dataclass(frozen=True)
class LaneRegistry:
    """Chain-authoritative view of active lanes, backed by a content store.

    The active set is the projection of the chain events; the bodies are loaded
    content-addressed from the store and hash-verified. Nothing here is a
    mutable source of truth -- both halves are deterministic derivations.
    """

    active: dict[str, str]
    store: LaneStore

    @classmethod
    def from_events(cls, events: Iterable[Any], store: LaneStore) -> LaneRegistry:
        """Build a registry by projecting *events* over *store*."""
        return cls(active=project_lane_registry(events), store=store)

    def names(self) -> list[str]:
        """Return the sorted names of currently active lanes."""
        return sorted(self.active)

    def hash_for(self, name: str) -> str | None:
        """Return the active lane hash for *name*, or ``None`` if retired/absent."""
        return self.active.get(name)

    def get(self, name: str) -> LaneManifest | None:
        """Load the active manifest for *name*, or ``None`` if none is active.

        Raises:
            LaneStoreError: The active hash resolves to a missing or tampered
                body -- surfaced loudly rather than silently returning a stale
                or wrong lane.
        """
        lane_hash = self.active.get(name)
        if lane_hash is None:
            return None
        return self.store.get(lane_hash)


__all__ = [
    "LaneRegistry",
    "LaneStore",
    "LaneStoreError",
    "project_lane_registry",
]
