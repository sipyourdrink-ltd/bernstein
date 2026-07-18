"""Claim-based fleet dedup for cache-key contention.

When two fleet workers pick up semantically identical goals that resolve to the
same cache key, only one should pay for the minutes-to-hours agent run. A naive
in-process lock cannot dedup across hosts, and a losing worker that *blocks* on
an hours-long run wastes a seat.

This routes cache-key contention through the same atomic claim protocol the
task backlog uses (:func:`bernstein.core.tasks.claim.backlog_transaction`): the
cache key becomes a single-row backlog, the first worker to flip it to
``in_progress`` wins the spawn, and every loser receives a signed
:class:`DuplicateOfReceipt` binding it to the winner's key and claim position.
The loser does not block; it completes by a lineage edge to the winner's
verified output. If the winner never records a verified output the claim can be
released and re-contended deterministically.

Row creation, the claim flip, the winner read, and the release all happen
inside one held claim lock. Creating the row outside the lock would let a
second contender's create overwrite a claim the first contender had already
been granted, and releasing outside the lock would let a stale reopen clobber a
concurrent claim; both windows are closed by running the whole read-modify-save
cycle as a single transaction.

Verifiability (issue #2551 AC3): a :class:`DuplicateOfReceipt` is HMAC-signed
over its canonical body with the audit-chain key, so it verifies offline; any
mutation of the winner reference, the cache key, or the claim position breaks
the tag, and :func:`verify_duplicate_receipt` names the mismatching field.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.cache_policy import (
    resolve_cached_path,
    validate_cache_key,
)
from bernstein.core.tasks.claim import (
    Backlog,
    BacklogEntry,
    ClaimFilter,
    backlog_transaction,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_RECEIPT_VERSION = 1

#: Status a released row returns to, matching the claim primitive's vocabulary.
_OPEN_STATUS = "open"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


# ---------------------------------------------------------------------------
# Duplicate-of receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicateOfReceipt:
    """A signed proof that ``loser`` deduped onto ``winner``'s cache key.

    Attributes:
        cache_key: The contended cache key (hex).
        winner: Claimer id that won the spawn.
        loser: Claimer id that deduped and did not spawn.
        claim_position: 1-based order in which ``loser`` arrived at the claim
            (the winner is position 0; the first loser is 1, and so on).
        winner_output_ref: ``sha256:`` reference to the winner's verified
            output - the lineage edge target the loser completes against.
        ts: Integer timestamp the receipt was minted.
        hmac: Hex HMAC-SHA256 tag over the canonical body, keyed with the
            audit-chain key.
    """

    cache_key: str
    winner: str
    loser: str
    claim_position: int
    winner_output_ref: str
    ts: int
    hmac: str = ""

    def body(self) -> dict[str, Any]:
        """Return the HMAC-covered body (every field except ``hmac``)."""
        return {
            "v": _RECEIPT_VERSION,
            "cache_key": self.cache_key,
            "winner": self.winner,
            "loser": self.loser,
            "claim_position": self.claim_position,
            "winner_output_ref": self.winner_output_ref,
            "ts": self.ts,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return the full JSON row, including the tag."""
        row = self.body()
        row["hmac"] = self.hmac
        return row

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DuplicateOfReceipt:
        """Reconstruct a receipt from its JSON row."""
        return cls(
            cache_key=str(data["cache_key"]),
            winner=str(data["winner"]),
            loser=str(data["loser"]),
            claim_position=int(data["claim_position"]),
            winner_output_ref=str(data["winner_output_ref"]),
            ts=int(data["ts"]),
            hmac=str(data.get("hmac", "")),
        )


def mint_duplicate_receipt(
    *,
    cache_key: str,
    winner: str,
    loser: str,
    claim_position: int,
    winner_output_ref: str,
    ts: int,
    hmac_key: bytes,
) -> DuplicateOfReceipt:
    """Return a signed :class:`DuplicateOfReceipt`.

    The tag is ``HMAC-SHA256(hmac_key, canonical(body))`` so a verifier holding
    the key confirms the receipt offline; a party without the key cannot forge a
    mutated receipt that still verifies.
    """
    unsigned = DuplicateOfReceipt(
        cache_key=cache_key,
        winner=winner,
        loser=loser,
        claim_position=claim_position,
        winner_output_ref=winner_output_ref,
        ts=ts,
    )
    tag = _hmac.new(hmac_key, _canonical_bytes(unsigned.body()), hashlib.sha256).hexdigest()
    return DuplicateOfReceipt(
        cache_key=cache_key,
        winner=winner,
        loser=loser,
        claim_position=claim_position,
        winner_output_ref=winner_output_ref,
        ts=ts,
        hmac=tag,
    )


def verify_duplicate_receipt(
    receipt: DuplicateOfReceipt,
    *,
    hmac_key: bytes,
    authoritative: DuplicateOfReceipt | None = None,
) -> tuple[bool, str | None]:
    """Verify a receipt offline; return ``(ok, mismatch_field)``.

    The HMAC is recomputed over the receipt's body. When it matches the stored
    tag the receipt is authentic and ``(True, None)`` is returned.

    When it does not match, the receipt was tampered with (or signed under a
    different key). If an ``authoritative`` copy is supplied - e.g. the winner
    reference, cache key, and claim position an operator holds from the audit
    chain and lineage store - the first body field that diverges is named, so a
    mutated ``winner``, ``cache_key``, or ``claim_position`` is reported by
    field (issue #2551 AC3). Absent an authoritative copy the mismatch is
    reported as ``"hmac"``.
    """
    expected = _hmac.new(hmac_key, _canonical_bytes(receipt.body()), hashlib.sha256).hexdigest()
    if _hmac.compare_digest(receipt.hmac, expected):
        return True, None
    if authoritative is not None:
        auth_body = authoritative.body()
        for field_name, value in receipt.body().items():
            if auth_body.get(field_name) != value:
                return False, field_name
    return False, "hmac"


# ---------------------------------------------------------------------------
# Claim-based contention
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimOutcome:
    """The result of contending for a cache key.

    Attributes:
        won: Whether this worker won the claim and must perform the spawn.
        claimer: This worker's claimer id.
        claim_position: 0 for the winner; 1-based arrival order for a loser.
        winner: The winning claimer id (equals ``claimer`` when ``won``).
    """

    won: bool
    claimer: str
    claim_position: int
    winner: str


def arbiter_backlog_path(base_dir: Path, cache_key: str) -> Path:
    """Return the contended-key backlog path for ``cache_key`` under ``base_dir``.

    The arbiter's backlog file is named after the cache key, so the key is
    validated as a single path component and the composed path is proven to
    resolve inside ``base_dir`` before it is handed back.

    Raises:
        UnsafeCacheKeyError: When ``cache_key`` is not a safe path component or
            the composed path would escape ``base_dir``.
    """
    return resolve_cached_path(base_dir, f"{validate_cache_key(cache_key)}.json")


class CacheKeyArbiter:
    """Serialise spawns for one cache key through the atomic claim protocol.

    The arbiter is backed by a single-row backlog file addressed by the cache
    key (see :func:`arbiter_backlog_path`). Row creation, the flip to
    ``in_progress``, the winner read, and the release each run inside one held
    claim lock - the same cross-thread and cross-process lock the task backlog
    claims under - so exactly one worker among N contenders wins even when all
    of them arrive before the backlog exists, and even across hosts sharing the
    backlog directory. A killed winner's claim is released by :meth:`release`
    and the next contender proceeds.
    """

    def __init__(self, backlog_path: Path, cache_key: str) -> None:
        self._path = backlog_path
        self._cache_key = validate_cache_key(cache_key)
        self._contenders = 0
        self._counter_lock = threading.Lock()

    @property
    def cache_key(self) -> str:
        return self._cache_key

    def _row(self, backlog: Backlog) -> BacklogEntry | None:
        """Return this key's row in ``backlog``, or ``None`` when absent."""
        for entry in backlog.entries:
            if entry.id == self._cache_key:
                return entry
        return None

    def _ensure_backlog(self) -> None:
        """Create this key's row inside the claim lock, idempotently.

        Never overwrites an existing document: a row that is already present
        (claimed or not) is left exactly as it is, so a late initialiser cannot
        reset a claim another contender has already been granted.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with backlog_transaction(self._path) as backlog:
            if self._row(backlog) is not None:
                return
            backlog.entries.append(BacklogEntry(id=self._cache_key))
            backlog.save()

    def contend(self, claimer: str) -> ClaimOutcome:
        """Attempt to claim the cache key for ``claimer``.

        The first caller to win flips the row and returns ``won=True`` with
        ``claim_position=0``. Every later caller finds the row already claimed
        and returns ``won=False`` with a 1-based ``claim_position`` reflecting
        arrival order, plus the winning claimer id.

        Creating the row, testing eligibility, flipping it, and reading back the
        winner all happen in one transaction, so no contender can observe or
        overwrite a half-initialised claim.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._counter_lock:
            self._contenders += 1
            position = self._contenders - 1
        with backlog_transaction(self._path) as backlog:
            entry = self._row(backlog)
            created = entry is None
            if entry is None:
                entry = BacklogEntry(id=self._cache_key)
                backlog.entries.append(entry)
            # The claim predicate is the backlog's own, scoped to this key's row
            # so a shared backlog directory can never hand the arbiter a foreign
            # row to claim.
            if ClaimFilter().allows(entry):
                entry.claim(claimer)
                backlog.save()
                return ClaimOutcome(won=True, claimer=claimer, claim_position=0, winner=claimer)
            if created:
                backlog.save()
            winner = entry.claimer or ""
        return ClaimOutcome(
            won=False,
            claimer=claimer,
            claim_position=position,
            winner=winner,
        )

    def current_winner(self) -> str:
        """Return the claimer currently holding this key, or ``""`` when free.

        Read under the claim lock, so the answer is consistent with the claim
        state rather than a snapshot taken mid-transaction.
        """
        with backlog_transaction(self._path) as backlog:
            entry = self._row(backlog)
            if entry is not None and entry.claimer is not None:
                return entry.claimer
        return ""

    def release(self) -> None:
        """Release the claim so the next contender can win (winner failed).

        Re-opens the backlog row to ``open`` and clears the claimer inside the
        claim lock, mirroring a deterministic claim release: the successor's
        next :meth:`contend` wins. Holding the lock across the load and the save
        means a release can never clobber a claim granted in between.
        """
        with backlog_transaction(self._path) as backlog:
            entry = self._row(backlog)
            if entry is None or (entry.claimer is None and entry.status == _OPEN_STATUS):
                return
            entry.status = _OPEN_STATUS
            entry.claimer = None
            entry.claimed_at = None
            backlog.save()


__all__ = [
    "CacheKeyArbiter",
    "ClaimOutcome",
    "DuplicateOfReceipt",
    "arbiter_backlog_path",
    "mint_duplicate_receipt",
    "verify_duplicate_receipt",
]
