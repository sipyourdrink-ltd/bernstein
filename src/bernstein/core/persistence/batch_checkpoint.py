"""Per-item checkpoint ledger for crash-safe batch processing (#5126 slice 1).

Each batch item is identified by an opaque ``entity_id`` string.  When an item
is processed successfully the caller writes one entry via
:meth:`BatchCheckpointLedger.record_success`.  On a crash or restart a new
:class:`BatchCheckpointLedger` over the same file replays the entries to
rebuild the index, and :meth:`BatchCheckpointLedger.is_done` returns ``True``
for every entity that already succeeded — so the caller skips it without
performing the side effect again.

The ledger is an append-only JSONL file.  Each line is a JSON object::

    {"entity_id": "...", "succeeded_at": "...", "prev_hash": "...", "entry_hash": "..."}

``prev_hash`` chains entries together so tampering with or removing an entry
breaks the chain.  The initial entry's ``prev_hash`` is the zero-hash
(64 hex zeros).  :meth:`BatchCheckpointLedger.verify` replays the chain and
returns the list of integrity errors.

Thread safety: concurrent writers in the same process share one instance and
the internal lock; concurrent processes appending to the same file depend on
OS-level atomicity for appends shorter than ``PIPE_BUF``.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

#: SHA-256 hex digest of the empty string — the genesis ``prev_hash``.
GENESIS_HASH: str = "0" * 64


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry_hash(prev_hash: str, entity_id: str, succeeded_at: str) -> str:
    """Derive the chain hash for one ledger entry."""
    payload = f"{prev_hash}\x00{entity_id}\x00{succeeded_at}".encode()
    return _sha256_hex(payload)


@dataclass
class CheckpointEntry:
    """One ledger row, reconstructed from a JSONL line."""

    entity_id: str
    succeeded_at: str
    prev_hash: str
    entry_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "succeeded_at": self.succeeded_at,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


class BatchCheckpointLedger:
    """Append-only per-item checkpoint ledger for batch processing (#5126).

    Args:
        path: Path to the ``.jsonl`` ledger file.  Created on first write if
            it does not exist.

    Usage::

        ledger = BatchCheckpointLedger(Path(".sdd/batch.jsonl"))
        for entity_id in items:
            if ledger.is_done(entity_id):
                continue          # already succeeded on a previous run
            process(entity_id)    # side-effecting step
            ledger.record_success(entity_id, datetime.utcnow().isoformat())
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        # entity_id → succeeded_at for every entry in the file
        self._done: dict[str, str] = {}
        self._last_hash: str = GENESIS_HASH
        self._load()

    # ------------------------------------------------------------------ public

    def is_done(self, entity_id: str) -> bool:
        """Return ``True`` if *entity_id* has a success entry in the ledger."""
        with self._lock:
            return entity_id in self._done

    def record_success(self, entity_id: str, succeeded_at: str) -> None:
        """Append a success entry for *entity_id* and update the in-memory index.

        Args:
            entity_id: Opaque identifier for the batch item (e.g. a resource
                path, a user id, or a secret name).
            succeeded_at: ISO 8601 timestamp of the successful processing.
        """
        with self._lock:
            eh = _entry_hash(self._last_hash, entity_id, succeeded_at)
            entry = CheckpointEntry(
                entity_id=entity_id,
                succeeded_at=succeeded_at,
                prev_hash=self._last_hash,
                entry_hash=eh,
            )
            line = json.dumps(entry.to_dict(), separators=(",", ":"), ensure_ascii=False)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._done[entity_id] = succeeded_at
            self._last_hash = eh

    def verify(self) -> list[str]:
        """Replay the ledger file and return chain-integrity errors.

        An empty list means the file is intact.  Errors name the 1-based line
        number and the problem (hash mismatch or missing field).
        """
        errors: list[str] = []
        if not self._path.exists():
            return errors
        prev = GENESIS_HASH
        for lineno, raw in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                errors.append(f"line {lineno}: JSON decode error: {exc}")
                continue
            for key in ("entity_id", "succeeded_at", "prev_hash", "entry_hash"):
                if key not in row:
                    errors.append(f"line {lineno}: missing field {key!r}")
                    break
            else:
                expected = _entry_hash(row["prev_hash"], row["entity_id"], row["succeeded_at"])
                if row["entry_hash"] != expected:
                    errors.append(f"line {lineno}: entry_hash mismatch for entity_id={row['entity_id']!r}")
                if row["prev_hash"] != prev:
                    errors.append(f"line {lineno}: prev_hash mismatch for entity_id={row['entity_id']!r}")
                prev = row["entry_hash"]
        return errors

    @property
    def done_count(self) -> int:
        """Number of distinct entity ids that have a success entry."""
        with self._lock:
            return len(self._done)

    # ------------------------------------------------------------------ private

    def _load(self) -> None:
        """Read existing entries from disk into the in-memory index."""
        if not self._path.exists():
            return
        for raw in self._path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
                if "entity_id" in row and "succeeded_at" in row and "entry_hash" in row:
                    self._done[row["entity_id"]] = row["succeeded_at"]
                    self._last_hash = row["entry_hash"]
            except (json.JSONDecodeError, KeyError):
                pass
