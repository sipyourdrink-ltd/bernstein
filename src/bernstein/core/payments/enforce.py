"""Enforcement: turn a transaction request + mandate into a chain-anchored receipt.

:func:`authorize` is the single decision point. It verifies the mandate
signature, checks the request against the mandate's bound scope (presence mode,
expiry, recipient, per-transaction amount) and against the cumulative spend
already recorded under the same ``mandate_hash``, then emits a
:class:`~bernstein.core.payments.receipt.TransactionReceipt` -- authorized or
refused. A refusal is a first-class receipt carrying a closed-enum reason, so a
denied attempt is as reconstructable as an approved one.

Cumulative-spend safety
-----------------------
Cumulative spend is aggregated on read from an append-only receipt ledger keyed
on ``mandate_hash`` (mirroring how :class:`bernstein.core.cost.spend_ledger.SpendLedger`
aggregates on read). The read-aggregate-decide-append sequence runs under an
exclusive ``flock`` over a per-workdir lock file -- the same lock discipline the
lineage store uses -- so two concurrent workers sharing one mandate can never
both read a stale total and each admit spend that, together, exceeds the cap.
All amount arithmetic is exact integer nano-units; no float is ever compared.

Refusal precedence
------------------
When more than one check would fail, the reported reason follows a fixed
precedence so the decision is deterministic:

    bad_signature -> wrong_presence_mode -> expired -> wrong_recipient
    -> over_max_amount -> cumulative_exceeded

Identity and structure are checked before temporal and payee constraints, and
per-transaction amount before the cumulative bound.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.payments._canonical import (
    require_nfc,
    to_nano_units,
    validate_currency,
)
from bernstein.core.payments.mandate import PresenceMode, SpendMandate
from bernstein.core.payments.receipt import (
    RECEIPT_VERSION,
    Decision,
    RefusalReason,
    TransactionReceipt,
    anchor_receipt,
)

if sys.platform == "win32":  # pragma: no cover - POSIX CI
    fcntl = None  # type: ignore[assignment]
else:
    import fcntl  # type: ignore[no-redef]

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from bernstein.core.payments._identity import OperatorIdentity
    from bernstein.core.security.audit_chain import AuditChainStore

__all__ = [
    "TransactionRequest",
    "authorize",
    "cumulative_authorized_nanos",
    "ledger_path",
    "load_mandate",
    "mandates_dir",
    "save_mandate",
]


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransactionRequest:
    """A concrete spend attempt an agent presents against a mandate.

    Amounts are already encoded to integer nano-unit strings and text is NFC;
    build via :meth:`build` so a caller passes human-friendly decimals and the
    canonical policies apply once.
    """

    amount_nanos: str
    currency: str
    recipient: str
    category: str
    presence_mode: str
    now: int

    @classmethod
    def build(
        cls,
        *,
        amount: str,
        currency: str,
        recipient: str,
        category: str,
        presence_mode: PresenceMode | str,
        now: int,
    ) -> TransactionRequest:
        """Encode and validate a request from operator-supplied values."""
        mode = presence_mode.value if isinstance(presence_mode, PresenceMode) else str(presence_mode)
        if mode not in {m.value for m in PresenceMode}:
            raise ValueError(f"unknown presence_mode: {mode!r}")
        return cls(
            amount_nanos=to_nano_units(amount),
            currency=validate_currency(currency),
            recipient=require_nfc(recipient, field="recipient"),
            category=require_nfc(category, field="category"),
            presence_mode=mode,
            now=int(now),
        )


# ---------------------------------------------------------------------------
# On-disk layout
# ---------------------------------------------------------------------------


def _payments_dir(workdir: Path) -> Path:
    return workdir / ".sdd" / "payments"


def mandates_dir(workdir: Path) -> Path:
    """Return the directory holding persisted signed mandates."""
    return _payments_dir(workdir) / "mandates"


def ledger_path(workdir: Path) -> Path:
    """Return the append-only receipt ledger path used for cumulative aggregation."""
    return _payments_dir(workdir) / "ledger.jsonl"


def _lock_path(workdir: Path) -> Path:
    return _payments_dir(workdir) / ".authorize.lock"


def _mandate_stem(mandate_hash: str) -> str:
    return mandate_hash.split(":", 1)[1] if ":" in mandate_hash else mandate_hash


def save_mandate(workdir: Path, mandate: SpendMandate) -> Path:
    """Persist *mandate* under ``.sdd/payments/mandates/<hash>.json``; return the path."""
    out_dir = mandates_dir(workdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_mandate_stem(mandate.mandate_hash())}.json"
    path.write_text(
        json.dumps(mandate.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def load_mandate(workdir: Path, mandate_hash: str) -> SpendMandate:
    """Load a persisted mandate by its content hash.

    Raises:
        FileNotFoundError: When no mandate with that hash is stored.
    """
    path = mandates_dir(workdir) / f"{_mandate_stem(mandate_hash)}.json"
    if not path.exists():
        raise FileNotFoundError(f"no mandate stored at {path}")
    return SpendMandate.from_dict(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Locking + cumulative aggregation
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _authorize_lock(workdir: Path) -> Iterator[None]:
    """Serialise read-aggregate-decide-append across threads and processes.

    Holds ``flock(LOCK_EX)`` over a stable lock file for the whole critical
    section so two concurrent authorizers cannot both observe a stale cumulative
    total. No-op on platforms without ``fcntl`` (Windows), matching the lineage
    store's degradation there.
    """
    lock = _lock_path(workdir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:  # pragma: no cover - Windows path
        yield
        return
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def cumulative_authorized_nanos(workdir: Path, mandate_hash: str) -> int:
    """Return the sum of authorized nano-unit amounts recorded for *mandate_hash*.

    Aggregation is on read over the append-only ledger; only ``authorized`` rows
    contribute (a refusal spent nothing). Malformed rows are skipped so a
    partially written tail degrades to under-counting rather than a crash.
    """
    path = ledger_path(workdir)
    if not path.exists():
        return 0
    total = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("mandate_hash") != mandate_hash:
            continue
        if row.get("decision") != Decision.AUTHORIZED.value:
            continue
        try:
            total += int(row.get("amount_nanos", 0))
        except (TypeError, ValueError):
            continue
    return total


def _append_ledger(
    workdir: Path, *, mandate_hash: str, receipt_hash: str, decision: str, amount_nanos: str, now: int
) -> None:
    path = ledger_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "mandate_hash": mandate_hash,
        "receipt_hash": receipt_hash,
        "decision": decision,
        "amount_nanos": amount_nanos,
        "now": now,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def _pre_cumulative_reason(request: TransactionRequest, mandate: SpendMandate) -> RefusalReason | None:
    """Return the refusal reason from checks that do not need cumulative state.

    Precedence: signature -> presence mode -> expiry -> recipient -> per-tx amount.
    Returns ``None`` when all these pass (the cumulative bound is checked next,
    under the lock).
    """
    if not mandate.verify_signature():
        return RefusalReason.BAD_SIGNATURE
    if request.presence_mode != mandate.presence_mode:
        return RefusalReason.WRONG_PRESENCE_MODE
    if request.now > mandate.not_after:
        return RefusalReason.EXPIRED
    if request.recipient != mandate.recipient:
        return RefusalReason.WRONG_RECIPIENT

    amount = int(request.amount_nanos)
    max_amount = int(mandate.max_amount_nanos)
    if amount > max_amount:
        return RefusalReason.OVER_MAX_AMOUNT
    if mandate.per_tx_cap_nanos is not None and amount > int(mandate.per_tx_cap_nanos):
        return RefusalReason.OVER_MAX_AMOUNT
    return None


def authorize(
    *,
    request: TransactionRequest,
    mandate: SpendMandate,
    workdir: Path,
    hmac_key: bytes,
    identity: OperatorIdentity,
    chain: AuditChainStore,
    nonce: str,
) -> TransactionReceipt:
    """Decide a transaction request against a mandate and emit an anchored receipt.

    Args:
        request: The concrete spend attempt.
        mandate: The signed mandate to authorize against.
        workdir: Project root holding ``.sdd/`` state.
        hmac_key: Operator HMAC key tagging lineage/audit records.
        identity: Operator signing identity (lineage ``.jws`` sidecar).
        chain: Audit chain store to mirror the receipt into.
        nonce: Per-receipt uniqueness nonce (an input, so the receipt is a
            deterministic function of its inputs).

    Returns:
        The anchored :class:`TransactionReceipt` (authorized or refused).

    Raises:
        ValueError: When the request currency does not match the mandate's. A
            currency mismatch is a malformed request, not a spend-policy
            refusal, so it is not one of the closed refusal reasons.
    """
    if request.currency != mandate.currency:
        raise ValueError(f"request currency {request.currency!r} does not match mandate currency {mandate.currency!r}")

    mandate_hash = mandate.mandate_hash()
    pre_reason = _pre_cumulative_reason(request, mandate)

    with _authorize_lock(workdir):
        reason = pre_reason
        if reason is None:
            cumulative = cumulative_authorized_nanos(workdir, mandate_hash)
            if cumulative + int(request.amount_nanos) > int(mandate.max_amount_nanos):
                reason = RefusalReason.CUMULATIVE_EXCEEDED

        decision = Decision.AUTHORIZED if reason is None else Decision.REFUSED
        receipt = TransactionReceipt(
            v=RECEIPT_VERSION,
            mandate_hash=mandate_hash,
            amount_nanos=request.amount_nanos,
            currency=request.currency,
            recipient=request.recipient,
            category=request.category,
            presence_mode=mandate.presence_mode,
            decision=decision.value,
            now=request.now,
            nonce=nonce,
            refusal_reason=None if reason is None else reason.value,
        )
        anchored = anchor_receipt(
            receipt,
            workdir=workdir,
            hmac_key=hmac_key,
            identity=identity,
            chain=chain,
        )
        _append_ledger(
            workdir,
            mandate_hash=mandate_hash,
            receipt_hash=anchored.receipt_hash(),
            decision=decision.value,
            amount_nanos=request.amount_nanos,
            now=request.now,
        )
    return anchored
