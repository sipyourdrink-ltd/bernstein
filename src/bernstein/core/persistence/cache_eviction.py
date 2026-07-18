"""Surgical, transitive cache eviction over served-from lineage edges.

``bernstein cache evict <key> --reason`` must not merely drop one row: a bad
cached value that other runs consumed has to be revocable together with
everything derived from it, and the operator needs a forensic recall set of the
runs that consumed the revoked value.

Two append-only artefacts back this:

* :class:`ServedFromLedger` - one row per ``served_from`` edge: a consuming run
  read a value from a cache key. The ledger is the by-artefact projection this
  module walks; it is append-only so an edge can never be silently rewritten.
* :class:`TombstoneStore` - append-only tombstone journal. A tombstoned key is
  always a miss, even when its drift verdict is fresh, so an evicted key can
  never serve again.

:meth:`TombstoneStore.evict` walks the ledger transitively from the evicted key
over ``served_from`` edges, tombstones every reachable key, and returns the full
:class:`RecallSet` - the consuming runs that must be treated as contaminated.

Determinism: the recall set is computed by a breadth-first walk in sorted order,
so the same ledger + eviction produces the byte-identical recall set and
tombstone order across processes.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.atomic_write import write_atomic_json

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

_LEDGER_NAME = "served_from.jsonl"
_TOMBSTONE_NAME = "tombstones.jsonl"


# ---------------------------------------------------------------------------
# Served-from ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServedFromEdge:
    """One ``served_from`` edge: ``consumer`` read ``cache_key``.

    Attributes:
        cache_key: The cache key whose value was served.
        consumer: The consuming run id (or a downstream cache key that pinned
            the served value as an input).
        output_hash: ``sha256:`` digest of the value that was served.
        ts: Integer timestamp the edge was recorded (provenance only; never
            read by the transitive walk).
    """

    cache_key: str
    consumer: str
    output_hash: str = ""
    ts: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable row."""
        return {
            "cache_key": self.cache_key,
            "consumer": self.consumer,
            "output_hash": self.output_hash,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ServedFromEdge:
        """Reconstruct an edge from its JSON row."""
        return cls(
            cache_key=str(data["cache_key"]),
            consumer=str(data["consumer"]),
            output_hash=str(data.get("output_hash", "")),
            ts=int(data.get("ts", 0)),
        )


class ServedFromLedger:
    """Append-only ledger of ``served_from`` edges under a cache directory."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def record(self, edge: ServedFromEdge) -> None:
        """Append one served-from edge as a canonical JSONL row."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        row = json.dumps(edge.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(row + "\n")

    def edges(self) -> list[ServedFromEdge]:
        """Return every recorded edge in append order."""
        if not self._path.exists():
            return []
        out: list[ServedFromEdge] = []
        for raw in self._path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            out.append(ServedFromEdge.from_dict(json.loads(raw)))
        return out

    def adjacency(self) -> dict[str, list[str]]:
        """Return ``cache_key -> [consumer, ...]`` in sorted, de-duplicated form.

        A consumer that is itself a cache key becomes a graph node, so the
        transitive walk cascades along chains of derived caches.
        """
        adj: dict[str, set[str]] = {}
        for edge in self.edges():
            adj.setdefault(edge.cache_key, set()).add(edge.consumer)
        return {key: sorted(consumers) for key, consumers in adj.items()}


# ---------------------------------------------------------------------------
# Tombstones
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tombstone:
    """A single tombstone: ``key`` is revoked because of ``root_key``'s eviction."""

    key: str
    reason: str
    root_key: str
    ts: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable row."""
        return {"key": self.key, "reason": self.reason, "root_key": self.root_key, "ts": self.ts}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Tombstone:
        """Reconstruct a tombstone from its JSON row."""
        return cls(
            key=str(data["key"]),
            reason=str(data.get("reason", "")),
            root_key=str(data.get("root_key", data["key"])),
            ts=int(data.get("ts", 0)),
        )


@dataclass(frozen=True)
class RecallSet:
    """The forensic result of one eviction.

    Attributes:
        root_key: The originally evicted key.
        reason: The operator-supplied revocation reason.
        tombstoned: Every key tombstoned by this eviction, in walk order
            (root first, then transitive keys in sorted BFS order).
        consumers: The consuming run ids reachable over ``served_from`` edges,
            sorted - the recall set an operator inspects for contamination.
    """

    root_key: str
    reason: str
    tombstoned: list[str] = field(default_factory=list[str])
    consumers: list[str] = field(default_factory=list[str])

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary."""
        return {
            "root_key": self.root_key,
            "reason": self.reason,
            "tombstoned": self.tombstoned.copy(),
            "consumers": self.consumers.copy(),
        }


class TombstoneStore:
    """Append-only tombstone journal with transitive eviction.

    A tombstoned key is a hard miss forever - :meth:`is_tombstoned` short
    circuits any lookup regardless of the drift verdict.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def all(self) -> dict[str, Tombstone]:
        """Return ``key -> Tombstone`` for every tombstone (last write wins)."""
        if not self._path.exists():
            return {}
        out: dict[str, Tombstone] = {}
        for raw in self._path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            ts = Tombstone.from_dict(json.loads(raw))
            out[ts.key] = ts
        return out

    def is_tombstoned(self, key: str) -> bool:
        """Return whether ``key`` has been revoked."""
        return key in self.all()

    def _append(self, tombstones: Iterable[Tombstone]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            for ts in tombstones:
                row = json.dumps(ts.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                fh.write(row + "\n")

    def evict(
        self,
        key: str,
        reason: str,
        *,
        ledger: ServedFromLedger,
        ts: int = 0,
    ) -> RecallSet:
        """Tombstone ``key`` and everything reachable over served-from edges.

        Walks the served-from graph breadth-first from ``key`` in sorted order,
        tombstoning every reachable cache key and collecting the consuming run
        ids. A consumer that is itself a cache key (a derived cache) is both
        tombstoned and traversed further, so revocation cascades down the
        lineage. Returns the full :class:`RecallSet`.

        Determinism (issue #2551 AC5): the tombstone order and recall set are a
        pure function of ``(key, ledger)`` - the BFS visits neighbours in sorted
        order, so two operators evicting the same key against the same ledger
        produce identical output.
        """
        adjacency = ledger.adjacency()
        graph_keys = set(adjacency)

        tombstoned_order: list[str] = []
        tombstoned_seen: set[str] = set()
        consumers_seen: set[str] = set()

        # BFS from the evicted key. A queued node is always a cache key.
        queue: deque[str] = deque([key])
        queued: set[str] = {key}
        while queue:
            current = queue.popleft()
            if current not in tombstoned_seen:
                tombstoned_order.append(current)
                tombstoned_seen.add(current)
            for consumer in adjacency.get(current, []):
                if consumer not in consumers_seen:
                    consumers_seen.add(consumer)
                # A consumer that is itself a cache key is a derived cache: keep
                # cascading and tombstone it too.
                if consumer in graph_keys and consumer not in queued:
                    queue.append(consumer)
                    queued.add(consumer)

        # The recall set is the consuming *runs* - consumers that are not
        # themselves cache keys. A consumer that is a derived cache key is
        # tombstoned (above), not reported as a run.
        recall = RecallSet(
            root_key=key,
            reason=reason,
            tombstoned=tombstoned_order,
            consumers=sorted(c for c in consumers_seen if c not in graph_keys),
        )
        self._append(Tombstone(key=k, reason=reason, root_key=key, ts=ts) for k in tombstoned_order)
        return recall


def cache_dir(workdir: Path) -> Path:
    """Return the canonical cache-policy directory under ``workdir``."""
    return workdir / ".sdd" / "caching" / "policy"


def open_ledger(workdir: Path) -> ServedFromLedger:
    """Return the served-from ledger rooted under ``workdir``."""
    return ServedFromLedger(cache_dir(workdir) / _LEDGER_NAME)


def open_tombstones(workdir: Path) -> TombstoneStore:
    """Return the tombstone store rooted under ``workdir``."""
    return TombstoneStore(cache_dir(workdir) / _TOMBSTONE_NAME)


def write_recall_report(path: Path, recall: RecallSet) -> None:
    """Persist a recall report as pretty JSON for the operator."""
    write_atomic_json(path, recall.to_dict())


__all__ = [
    "RecallSet",
    "ServedFromEdge",
    "ServedFromLedger",
    "Tombstone",
    "TombstoneStore",
    "cache_dir",
    "open_ledger",
    "open_tombstones",
    "write_recall_report",
]
