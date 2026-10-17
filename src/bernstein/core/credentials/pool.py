"""Managed credential pool with usage tracking, rate-limit detection, and rotation.

Each credential in the pool tracks provider, usage count, remaining quota,
last-used timestamp, and rate-limit cooldown state.  Pool-level ``acquire()``
returns the least-used non-rate-limited key; ``release(key_id, usage_delta)``
updates counters.  Automatic rotation marks a key as cooling down on 429 or
quota exhaustion; the next ``acquire()`` skips it.

Pool state is persisted to ``.sdd/credentials/pools/<pool_name>.json`` via
atomic writes and survives orchestrator restart.  The credential *values*
live in the operator's external secret store behind
:class:`~bernstein.core.security.external_secret_store.SecretRef`; only
opaque references are held here.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path  # noqa: TC003 - runtime use
from typing import Any

from bernstein.core.persistence.atomic_write import write_atomic_json

logger = logging.getLogger(__name__)

_POOL_SCHEMA_VERSION = 1
_DEFAULT_COOLDOWN_S = 60.0
_DEFAULT_QUOTA = -1  # unlimited


class CredentialPoolError(RuntimeError):
    """Base for credential pool failures."""


class NoAvailableCredentialError(CredentialPoolError):
    """Every credential in the pool is either exhausted or cooling down."""


@dataclass
class CredentialEntry:
    """One credential in the pool."""

    key_id: str
    provider: str
    secret_ref: str
    usage_count: int = 0
    remaining_quota: int = _DEFAULT_QUOTA
    last_used_ts: float = 0.0
    cooldown_until_ts: float = 0.0
    rate_limit_count: int = 0
    exit_proxy: str = ""
    fingerprint_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def is_available(self) -> bool:
        if self.cooldown_until_ts > time.time():
            return False
        return self.remaining_quota != 0


@dataclass(frozen=True)
class PoolStatus:
    """Aggregate health of a credential pool."""

    pool_name: str
    total_keys: int
    active_keys: int
    exhausted_keys: int
    cooling_down_keys: int
    aggregate_remaining_quota: int

    @property
    def has_capacity(self) -> bool:
        return self.active_keys > 0


@dataclass(frozen=True)
class PoolSnapshot:
    """Serialisable point-in-time snapshot of a pool."""

    pool_name: str
    schema_version: int
    entries: list[dict[str, Any]]
    created_ts: float
    updated_ts: float


class CredentialPool:
    """Thread-safe credential pool with least-used-first acquisition."""

    def __init__(
        self,
        pool_name: str,
        *,
        persist_dir: Path | None = None,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_S,
    ) -> None:
        self._name = pool_name
        self._cooldown_s = cooldown_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, CredentialEntry] = {}
        self._persist_dir = persist_dir
        self._created_ts = time.time()

        if persist_dir is not None:
            self._load(persist_dir)

    @property
    def name(self) -> str:
        return self._name

    def add(self, entry: CredentialEntry) -> None:
        with self._lock:
            if entry.key_id in self._entries:
                raise CredentialPoolError(f"key_id {entry.key_id!r} already exists in pool {self._name!r}")
            self._entries[entry.key_id] = entry
            self._persist()

    def remove(self, key_id: str) -> CredentialEntry:
        with self._lock:
            entry = self._entries.pop(key_id)
            self._persist()
            return entry

    def update(self, key_id: str, **kwargs: Any) -> CredentialEntry:
        with self._lock:
            entry = self._entries[key_id]
            for k, v in kwargs.items():
                if not hasattr(entry, k):
                    raise CredentialPoolError(f"CredentialEntry has no field {k!r}")
                object.__setattr__(entry, k, v)
            self._persist()
            return entry

    def get(self, key_id: str) -> CredentialEntry:
        with self._lock:
            return self._entries[key_id]

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._entries)

    def acquire(self, *, provider: str | None = None) -> CredentialEntry:
        with self._lock:
            now = time.time()
            candidates = [
                e for e in self._entries.values() if e.is_available and (provider is None or e.provider == provider)
            ]
            if not candidates:
                raise NoAvailableCredentialError(
                    f"no available credential in pool {self._name!r}"
                    + (f" for provider {provider!r}" if provider else "")
                )
            best = min(candidates, key=lambda e: (e.usage_count, e.last_used_ts))
            best.usage_count += 1
            best.last_used_ts = now
            self._persist()
            return best

    def release(
        self,
        key_id: str,
        *,
        usage_delta: int = 0,
        quota_consumed: int = 0,
        rate_limited: bool = False,
        exhausted: bool = False,
    ) -> None:
        with self._lock:
            entry = self._entries[key_id]
            entry.usage_count += usage_delta
            if entry.remaining_quota > 0:
                entry.remaining_quota = max(0, entry.remaining_quota - quota_consumed)
            if rate_limited:
                entry.cooldown_until_ts = time.time() + self._cooldown_s
                entry.rate_limit_count += 1
                logger.info(
                    "credential %s in pool %s rate-limited (count: %d)",
                    key_id,
                    self._name,
                    entry.rate_limit_count,
                )
            if exhausted:
                entry.remaining_quota = 0
                logger.warning("credential %s in pool %s marked exhausted", key_id, self._name)
            self._persist()

    def status(self) -> PoolStatus:
        with self._lock:
            now = time.time()
            total = len(self._entries)
            exhausted = sum(1 for e in self._entries.values() if e.remaining_quota == 0)
            cooling = sum(1 for e in self._entries.values() if e.cooldown_until_ts > now and e.remaining_quota != 0)
            active = total - exhausted - cooling
            agg_quota = sum(e.remaining_quota for e in self._entries.values() if e.remaining_quota > 0)
            unlimited = any(e.remaining_quota == _DEFAULT_QUOTA for e in self._entries.values() if e.is_available)
            return PoolStatus(
                pool_name=self._name,
                total_keys=total,
                active_keys=active,
                exhausted_keys=exhausted,
                cooling_down_keys=cooling,
                aggregate_remaining_quota=-1 if unlimited else agg_quota,
            )

    def batch_add(self, entries: list[CredentialEntry]) -> int:
        """Add multiple credentials at once.  Returns count of newly added keys."""
        added = 0
        with self._lock:
            for entry in entries:
                if entry.key_id in self._entries:
                    logger.debug("batch_add: skipping duplicate key_id %s", entry.key_id)
                    continue
                self._entries[entry.key_id] = entry
                added += 1
            if added:
                self._persist()
        return added

    @classmethod
    def from_manifest(
        cls,
        manifest_path: Path,
        *,
        pool_name: str | None = None,
        persist_dir: Path | None = None,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_S,
    ) -> CredentialPool:
        """Load a pool from a JSON manifest file.

        Manifest format::

            {
              "pool_name": "free-tier",
              "cooldown_seconds": 90,
              "credentials": [
                {
                  "key_id": "oai-001",
                  "provider": "openai",
                  "secret_ref": "vault:oai-001",
                  "exit_proxy": "socks5://10.0.0.1:1080",
                  "fingerprint_id": "fp-alpha",
                  "remaining_quota": 200
                }
              ]
            }
        """
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        name = pool_name or raw.get("pool_name", manifest_path.stem)
        cd = raw.get("cooldown_seconds", cooldown_seconds)
        pool = cls(name, persist_dir=persist_dir, cooldown_seconds=cd)
        entries = []
        for cred in raw.get("credentials", []):
            meta = cred.pop("metadata", {})
            entries.append(CredentialEntry(**cred, metadata=meta))
        pool.batch_add(entries)
        return pool

    def reachability_check(
        self,
        probe_fn: Any = None,
    ) -> dict[str, bool]:
        """Probe each credential and return {key_id: reachable}.

        ``probe_fn(entry) -> bool`` is called for each entry.  If not
        provided, all entries are assumed reachable.  Unreachable entries
        are put into cooldown.
        """
        results: dict[str, bool] = {}
        with self._lock:
            for entry in self._entries.values():
                if probe_fn is None:
                    results[entry.key_id] = True
                    continue
                try:
                    ok = probe_fn(entry)
                except Exception:
                    ok = False
                results[entry.key_id] = ok
                if not ok:
                    entry.cooldown_until_ts = time.time() + self._cooldown_s
                    logger.info("reachability: %s unreachable, cooling down", entry.key_id)
            self._persist()
        return results

    def rotate_exhausted(self) -> list[CredentialEntry]:
        """Remove and return all exhausted credentials."""
        removed = []
        with self._lock:
            exhausted_ids = [k for k, e in self._entries.items() if e.remaining_quota == 0]
            for kid in exhausted_ids:
                removed.append(self._entries.pop(kid))
            if removed:
                self._persist()
        return removed

    def available_by_proxy(self) -> dict[str, list[str]]:
        """Return {exit_proxy: [key_ids]} for load-spreading analysis."""
        by_proxy: dict[str, list[str]] = {}
        with self._lock:
            for entry in self._entries.values():
                if entry.is_available:
                    proxy = entry.exit_proxy or "direct"
                    by_proxy.setdefault(proxy, []).append(entry.key_id)
        return by_proxy

    def _persist(self) -> None:
        if self._persist_dir is None:
            return
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        path = self._persist_dir / f"{self._name}.json"
        snap = self._snapshot_unlocked()
        write_atomic_json(path, asdict(snap))

    def _load(self, persist_dir: Path) -> None:
        path = persist_dir / f"{self._name}.json"
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("failed to load pool %s: %s", self._name, exc)
            return
        if raw.get("schema_version") != _POOL_SCHEMA_VERSION:
            logger.warning(
                "pool %s schema version %s != %s, ignoring",
                self._name,
                raw.get("schema_version"),
                _POOL_SCHEMA_VERSION,
            )
            return
        self._created_ts = raw.get("created_ts", time.time())
        for entry_data in raw.get("entries", []):
            meta = entry_data.pop("metadata", {})
            entry = CredentialEntry(**entry_data, metadata=meta)
            self._entries[entry.key_id] = entry

    def _snapshot_unlocked(self) -> PoolSnapshot:
        return PoolSnapshot(
            pool_name=self._name,
            schema_version=_POOL_SCHEMA_VERSION,
            entries=[asdict(e) for e in self._entries.values()],
            created_ts=self._created_ts,
            updated_ts=time.time(),
        )

    def snapshot(self) -> PoolSnapshot:
        with self._lock:
            return self._snapshot_unlocked()
