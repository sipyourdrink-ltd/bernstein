"""Claim-time fleet-variable config plane (#2550).

A fleet variable is a named, mutable JSON value whose identity is its audit
chain segment rather than a live-state row. Three coupled guarantees make it
substrate-native rather than a plain key-value store:

* **Every write is a chain event.** :meth:`FleetVariableStore.set` appends a
  ``fleet.var_set`` record (actor, old value hash, new value hash, per-name
  write ordinal) through the HMAC-chained audit log. Mutating or deleting any
  historical write flips ``bernstein audit verify`` with a named failing
  record - the variable's history is tamper-evident.

* **Every read is content-addressed and pinned.**
  :meth:`FleetVariableStore.read_for_task` resolves the current value, hashes
  it canonically, writes the value bytes into a content-addressed blob store,
  and pins ``(name, value_hash, chain_position)`` into the reading task's
  lineage spine. The value bytes are never deleted, so a pinned hash always
  resolves to the exact bytes the task read.

* **Replay resolves from the pin, never the live value.** :func:`replay_reads`
  walks a run's spine and resolves each pinned read from its content hash, so
  a run replayed after N further mutations produces byte-identical reads. A
  live-value store cannot do this - the determinism axis is not decoration,
  it is the mechanism.

Divergence between two workers is explained offline from the chain alone
(:meth:`FleetVariableStore.explain_divergence`): the writes that landed
between their two pinned chain positions, with no server running.

This module is store-agnostic and filesystem-backed (under
``<sdd>/fleet/variables``); it never imports the CLI or a running server.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.lineage.spine import content_hash_of
from bernstein.core.security.audit_chain import (
    EVENT_FLEET_VAR_SET,
    record_fleet_var_set,
)

if TYPE_CHECKING:
    from bernstein.core.lineage.spine import LineageSpine
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "FleetVariableStore",
    "PinnedRead",
    "VariableRead",
    "VariableWrite",
    "canonical_value_bytes",
    "replay_reads",
    "value_hash_of",
]

#: Spine ``artifact_path`` prefix for a claim-time config read pin. Replay
#: recognises pins by this prefix; the trailing segment is the variable name.
_READ_ARTIFACT_PREFIX = "config/var"


def canonical_value_bytes(value: Any) -> bytes:
    """Serialise *value* to canonical JSON bytes.

    Sorted keys, compact separators, ASCII-escaped: the encoding is a pure
    function of the value, independent of platform and locale, so the value
    hash is reproducible on every machine in the fleet.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def value_hash_of(value: Any) -> str:
    """Return the ``sha256:``-prefixed content hash of *value*'s canonical bytes.

    Uses the same construction as the lineage spine's ``content_hash_of`` so
    that a pin's spine ``content_hash`` equals the variable ``value_hash`` -
    the pin and the value share one content-addressed identity.
    """
    return content_hash_of(canonical_value_bytes(value))


@dataclass(frozen=True)
class VariableWrite:
    """A recorded ``fleet.var_set`` write, projected from the audit chain."""

    name: str
    old_value_hash: str
    new_value_hash: str
    chain_position: int
    actor: str
    event_hmac: str


@dataclass(frozen=True)
class VariableRead:
    """A claim-time read pinned into a task's lineage spine."""

    name: str
    value_hash: str
    chain_position: int
    spine_entry_hash: str


@dataclass(frozen=True)
class PinnedRead:
    """A pinned read resolved back to its exact recorded value during replay."""

    name: str
    value_hash: str
    chain_position: int
    value: Any


def _validate_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or name in {".", ".."} or "\x00" in name:
        raise ValueError(f"invalid fleet variable name: {name!r}")


class FleetVariableStore:
    """Content-addressed, audit-chained store of named fleet variables.

    Args:
        root: Directory holding the value blobs and the name index (created on
            demand); conventionally ``<sdd>/fleet/variables``.
        chain: The audit chain every write is recorded into. The chain is the
            source of truth for history and divergence; the on-disk index is a
            derived cache of the current per-name head.
    """

    def __init__(self, root: Path, *, chain: AuditChainStore) -> None:
        self._root = Path(root)
        self._chain = chain
        self._blobs = self._root / "blobs"
        self._index_path = self._root / "index.json"

    # -- writes -----------------------------------------------------------

    def set(self, name: str, value: Any, *, actor: str = "operator") -> VariableWrite:
        """Write *value* under *name* as a chain event and return the record.

        The value bytes are written to the content-addressed blob store
        (append-only, never overwritten) before the chain event lands, so a
        pinned read can always resolve its value later.
        """
        _validate_name(name)
        index = self._load_index()
        prior = index.get(name)
        old_value_hash = prior["value_hash"] if prior else ""
        chain_position = (prior["chain_position"] + 1) if prior else 0

        new_value_hash = value_hash_of(value)
        self._write_blob(new_value_hash, canonical_value_bytes(value))

        event = record_fleet_var_set(
            chain=self._chain,
            name=name,
            old_value_hash=old_value_hash,
            new_value_hash=new_value_hash,
            chain_position=chain_position,
            actor=actor,
        )

        index[name] = {"value_hash": new_value_hash, "chain_position": chain_position}
        self._save_index(index)

        return VariableWrite(
            name=name,
            old_value_hash=old_value_hash,
            new_value_hash=new_value_hash,
            chain_position=chain_position,
            actor=actor,
            event_hmac=event.hmac,
        )

    # -- reads ------------------------------------------------------------

    def get(self, name: str) -> Any:
        """Return the current value of *name*.

        Raises:
            KeyError: If the variable has never been written.
        """
        index = self._load_index()
        prior = index.get(name)
        if prior is None:
            raise KeyError(name)
        return self.resolve(prior["value_hash"])

    def resolve(self, value_hash: str) -> Any:
        """Resolve a value from its content hash (replay path, never live).

        Raises:
            KeyError: If no blob is stored for *value_hash*.
        """
        blob = self._blob_path(value_hash)
        if not blob.exists():
            raise KeyError(value_hash)
        return json.loads(blob.read_bytes())

    def read_for_task(
        self,
        name: str,
        spine: LineageSpine,
        *,
        actor: str = "config_read",
        model: str = "fleet_config",
        timestamp: int | None = None,
    ) -> tuple[Any, VariableRead]:
        """Resolve *name* and pin ``(name, value_hash, chain_position)`` into *spine*.

        The pin's spine ``content`` is the value's canonical bytes, so the
        entry's ``content_hash`` equals the variable ``value_hash``. Replay
        recovers the value from this hash, never from the live store.
        """
        index = self._load_index()
        prior = index.get(name)
        if prior is None:
            raise KeyError(name)
        value_hash = str(prior["value_hash"])
        chain_position = int(prior["chain_position"])
        value = self.resolve(value_hash)

        ts = timestamp if timestamp is not None else int(datetime.now(tz=UTC).timestamp())
        entry_hash = spine.record(
            artifact_path=f"{_READ_ARTIFACT_PREFIX}/{name}",
            content=canonical_value_bytes(value),
            actor=actor,
            step_id=f"{name}@{chain_position}",
            model=model,
            timestamp=ts,
        )
        return value, VariableRead(
            name=name,
            value_hash=value_hash,
            chain_position=chain_position,
            spine_entry_hash=entry_hash,
        )

    # -- history / divergence (chain projections) -------------------------

    def history(self, name: str) -> list[VariableWrite]:
        """Return every write to *name*, oldest first, projected from the chain."""
        writes: list[VariableWrite] = []
        for event in self._chain.query(event_type=EVENT_FLEET_VAR_SET):
            details = event.details
            if details.get("name") != name:
                continue
            writes.append(
                VariableWrite(
                    name=name,
                    old_value_hash=str(details.get("old_value_hash", "")),
                    new_value_hash=str(details.get("new_value_hash", "")),
                    chain_position=int(details.get("chain_position", 0)),
                    actor=event.actor,
                    event_hmac=event.hmac,
                )
            )
        writes.sort(key=lambda w: w.chain_position)
        return writes

    def explain_divergence(self, name: str, position_a: int, position_b: int) -> list[VariableWrite]:
        """Return the writes that landed between two pinned read positions.

        Given two workers that pinned *name* at ``position_a`` and
        ``position_b``, the returned writes are those with a chain position
        strictly after the earlier pin and up to and including the later pin -
        the recorded cause of the two workers reading different values. This
        resolves from the chain alone, with no server running.
        """
        low, high = sorted((int(position_a), int(position_b)))
        return [w for w in self.history(name) if low < w.chain_position <= high]

    def list_names(self) -> list[str]:
        """Return the names of all variables ever written, sorted."""
        return sorted(self._load_index().keys())

    # -- persistence helpers ----------------------------------------------

    def _blob_path(self, value_hash: str) -> Path:
        # Strip the ``sha256:`` scheme prefix for the on-disk filename.
        digest = value_hash.split(":", 1)[-1]
        return self._blobs / f"{digest}.json"

    def _write_blob(self, value_hash: str, data: bytes) -> None:
        self._blobs.mkdir(parents=True, exist_ok=True)
        path = self._blob_path(value_hash)
        if path.exists():
            return
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if not self._index_path.exists():
            return {}
        try:
            loaded = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _save_index(self, index: dict[str, dict[str, Any]]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, sort_keys=True, indent=2), encoding="utf-8")
        os.replace(tmp, self._index_path)


def replay_reads(spine: LineageSpine, store: FleetVariableStore) -> list[PinnedRead]:
    """Resolve every pinned config read in *spine* from its content hash.

    Walks the run's spine in append order, and for each claim-time config-read
    pin resolves the value from the pinned hash via the content-addressed
    store - never from the live variable. The result is byte-identical across
    any number of later mutations; that is what makes replay a proof rather
    than a re-read.
    """
    reads: list[PinnedRead] = []
    for entry in spine.iter_entries():
        artifact = entry.artifact_path
        if not artifact.startswith(f"{_READ_ARTIFACT_PREFIX}/"):
            continue
        name = artifact[len(_READ_ARTIFACT_PREFIX) + 1 :]
        value_hash = entry.content_hash
        # step_id is ``<name>@<chain_position>``; recover the position.
        position = 0
        step = entry.step_id
        if "@" in step:
            try:
                position = int(step.rsplit("@", 1)[1])
            except ValueError:
                position = 0
        value = store.resolve(value_hash)
        reads.append(PinnedRead(name=name, value_hash=value_hash, chain_position=position, value=value))
    return reads
