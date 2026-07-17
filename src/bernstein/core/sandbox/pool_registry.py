"""Pool registry as a deterministic projection of the audit chain (#2547).

There is no side database to drift. A pool's lifecycle lives entirely in the
HMAC audit chain: ``pool.registered`` / ``pool.updated`` / ``pool.retired``
events (see :mod:`bernstein.core.security.audit_chain`) are the only mutation
path. The *authority* on which pool is active for a given name is the chain;
:func:`project_pool_registry` replays those events in order and returns the
current ``{name: pool_hash}`` map. Two operators replaying the same events
arrive at byte-identical projections.

The manifest *bodies* are held in a content-addressed store
(:class:`PoolStore`): each manifest is written to ``<root>/pools/<pool_hash>.json``
and re-verified against its canonical hash on load. The store is immutable and
self-verifying -- editing a stored body changes its hash and the load refuses
it -- so it cannot drift out of agreement with the chain the way a mutable
registry table could.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.sandbox.pool import PoolManifest, PoolManifestError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

_POOL_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

#: Event-type strings the projection understands. Imported lazily to avoid a
#: hard import cycle with the audit-chain module at import time.
_EVENT_REGISTERED = "pool.registered"
_EVENT_UPDATED = "pool.updated"
_EVENT_RETIRED = "pool.retired"


class PoolStoreError(ValueError):
    """Raised when a content-addressed pool body is missing or tampered."""


def _event_field(event: Any, key: str) -> Any:
    """Read *key* from *event* whether it is a mapping or a dataclass."""
    if isinstance(event, Mapping):
        return event.get(key)
    return getattr(event, key, None)


def project_pool_registry(events: Iterable[Any]) -> dict[str, str]:
    """Return the active ``{pool_name: pool_hash}`` map from *events*.

    A pure function over the ordered ``pool.*`` event stream. Non-pool events
    are ignored, so callers may pass the whole chain. Register and update set
    the active hash for the name; retire drops it. Replaying the same ordered
    events always yields the same map (AC: determinism).

    Args:
        events: Ordered events, each either a mapping or an object exposing
            ``event_type`` and a ``details`` payload carrying ``pool_name`` /
            ``pool_hash`` (e.g. :class:`AuditEvent`).

    Returns:
        Mapping from pool name to the currently active canonical ``pool_hash``.
    """
    active: dict[str, str] = {}
    for event in events:
        event_type = _event_field(event, "event_type")
        details = _event_field(event, "details") or {}
        name = details.get("pool_name")
        pool_hash = details.get("pool_hash")
        if not name:
            continue
        if event_type in (_EVENT_REGISTERED, _EVENT_UPDATED):
            if pool_hash:
                active[name] = pool_hash
        elif event_type == _EVENT_RETIRED:
            active.pop(name, None)
    return active


@dataclass(frozen=True)
class PoolStore:
    """Content-addressed store for pool manifest bodies.

    Bodies live under ``root/pools/<pool_hash>.json``. The hash in the filename
    is the manifest's own canonical hash, so a load both locates the body and
    verifies it: a tampered body recomputes to a different hash and is refused.
    """

    root: Path

    @property
    def pools_dir(self) -> Path:
        return self.root / "pools"

    def _path_for(self, pool_hash: str) -> Path:
        """Return the on-disk path for *pool_hash*, guarded against traversal."""
        if not _POOL_HASH_RE.match(pool_hash):
            raise PoolStoreError(f"pool_hash is not a canonical sha256 digest: {pool_hash!r}")
        base = self.pools_dir
        candidate = base / f"{pool_hash}.json"
        base_real = os.path.realpath(base)
        cand_real = os.path.realpath(candidate)
        if os.path.commonpath([base_real, cand_real]) != base_real:
            raise PoolStoreError(f"pool body path escapes the pools directory: {pool_hash!r}")
        return candidate

    def put(self, manifest: PoolManifest) -> Path:
        """Write *manifest* to its content-addressed path and return it.

        Idempotent: re-writing an identical manifest is a no-op-equivalent
        (the bytes are byte-identical because the payload is canonical).
        """
        path = self._path_for(manifest.pool_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")), encoding="utf-8")
        return path

    def get(self, pool_hash: str) -> PoolManifest:
        """Load and hash-verify the manifest body for *pool_hash*.

        Raises:
            PoolStoreError: The body is absent, unreadable, or its recomputed
                canonical hash does not equal *pool_hash* (tampered).
        """
        path = self._path_for(pool_hash)
        if not path.is_file():
            raise PoolStoreError(f"no pool body for {pool_hash!r}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise PoolStoreError(f"unreadable pool body for {pool_hash!r}: {exc}") from exc
        try:
            manifest = PoolManifest.from_dict(data)
        except (PoolManifestError, KeyError, ValueError, TypeError) as exc:
            # from_dict recomputes the hash and refuses a body whose embedded
            # pool_hash no longer matches its own content -- surface that as a
            # store-level tamper error, not a manifest construction error.
            raise PoolStoreError(f"pool body hash mismatch for {pool_hash!r} (tampered): {exc}") from exc
        if manifest.compute_hash() != pool_hash:
            raise PoolStoreError(f"pool body hash mismatch for {pool_hash!r} (tampered)")
        return manifest

    def has(self, pool_hash: str) -> bool:
        """Return whether a body exists for *pool_hash* (no hash verification)."""
        try:
            return self._path_for(pool_hash).is_file()
        except PoolStoreError:
            return False


@dataclass(frozen=True)
class PoolRegistry:
    """Chain-authoritative view of active pools, backed by a content store.

    The active set is the projection of the chain events; the bodies are loaded
    content-addressed from the store and hash-verified. Nothing here is a
    mutable source of truth -- both halves are deterministic derivations.
    """

    active: dict[str, str]
    store: PoolStore

    @classmethod
    def from_events(cls, events: Iterable[Any], store: PoolStore) -> PoolRegistry:
        """Build a registry by projecting *events* over *store*."""
        return cls(active=project_pool_registry(events), store=store)

    def names(self) -> list[str]:
        """Return the sorted names of currently active pools."""
        return sorted(self.active)

    def hash_for(self, name: str) -> str | None:
        """Return the active pool hash for *name*, or ``None`` if retired/absent."""
        return self.active.get(name)

    def get(self, name: str) -> PoolManifest | None:
        """Load the active manifest for *name*, or ``None`` if none is active.

        Raises:
            PoolStoreError: The active hash resolves to a missing or tampered
                body -- surfaced loudly rather than silently returning a stale
                or wrong pool.
        """
        pool_hash = self.active.get(name)
        if pool_hash is None:
            return None
        return self.store.get(pool_hash)


__all__ = [
    "PoolRegistry",
    "PoolStore",
    "PoolStoreError",
    "project_pool_registry",
]
