"""Per-hop HMAC-chained delegation receipts (RFC 8693-style token exchange).

A single run authorizes a chain of principals:
``principal -> orchestrator -> sub-agent``. Each hop is an act of delegation
(the principal authorizes the orchestrator; the orchestrator spawns a
sub-agent to act on the principal's behalf). This module records one
*delegation receipt* per hop, HMAC-chained so an auditor can reconstruct
offline exactly which principal authorized which sub-agent action for a run.

Receipt shape
-------------
Each receipt is one JSONL line under ``<root>/delegation/<run_id>.jsonl``::

    {
      "run_id": "...",
      "hop_index": 0,
      "issuer": "principal:alex",     # who is delegating (RFC 8693 subject)
      "subject": "orchestrator",      # the acting party being authorized
      "audience": "sub-agent:backend",# the target the token is minted for
      "act": "task.spawn",            # the delegated action
      "created": 1730000000,
      "prev_hmac": "<hex>",
      "hmac": "<hex>"
    }

The HMAC covers the previous receipt's HMAC concatenated with the canonical
JSON of this receipt's body (all fields except ``hmac``), using the same
construction as :mod:`bernstein.core.security.audit`. A flipped field, a
deleted hop, or the wrong key surfaces as a linkage/HMAC error in
:func:`verify_run_chain` rather than passing silently.

The key is the install-scoped audit key (``load_or_create_audit_key``) so the
delegation chain shares the install's tamper-evidence anchor: strip the
identity and the chain no longer verifies.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "DEFAULT_ROOT",
    "GENESIS_HMAC",
    "ChainResult",
    "DelegationLedger",
    "DelegationReceipt",
    "default_ledger",
    "record_delegation_hop",
    "verify_run",
    "verify_run_chain",
]

#: Genesis linkage value for the first hop of a run (matches the audit-chain
#: convention of a fixed 64-hex-zero anchor).
GENESIS_HMAC: Final[str] = "0" * 64

_SUBDIR: Final[str] = "delegation"

#: Default root for delegation receipts - the same tree as the HMAC-chained
#: audit log so ``.sdd/audit/delegation/`` sits beside ``.sdd/audit/*.jsonl``.
DEFAULT_ROOT: Final[Path] = Path(".sdd/audit")


@dataclass(frozen=True)
class DelegationReceipt:
    """A single HMAC-chained delegation hop."""

    run_id: str
    hop_index: int
    issuer: str
    subject: str
    audience: str
    act: str
    created: int
    prev_hmac: str = GENESIS_HMAC
    hmac: str = ""

    def body(self) -> dict[str, Any]:
        """Return the signed body (all fields except ``hmac``)."""
        data = asdict(self)
        data.pop("hmac", None)
        return data


def _compute_hmac(key: bytes, prev_hmac: str, body: dict[str, Any]) -> str:
    """HMAC-SHA256 over ``prev_hmac`` concatenated with canonical JSON body.

    Identical construction to :func:`bernstein.core.security.audit._compute_hmac`
    so the delegation chain and the audit chain share tamper-evidence
    semantics.
    """
    payload = prev_hmac + json.dumps(body, sort_keys=True)
    return _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


@dataclass
class ChainResult:
    """Outcome of reconstructing a run's delegation chain offline."""

    valid: bool
    hops: int
    receipts: list[DelegationReceipt] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class DelegationLedger:
    """Append-only, HMAC-chained delegation-receipt store rooted at a dir.

    One JSONL file per run under ``<root>/delegation/``. The ledger tracks
    the running ``prev_hmac`` per run by reading the tail of the file, so
    hops recorded across process restarts still chain continuously.
    """

    def __init__(self, root: Path, key: bytes) -> None:
        """Bind the ledger to a root directory and a chaining key.

        Args:
            root: Base directory; receipts land under ``root/delegation/``.
            key: HMAC key - use the install audit key so the chain is
                anchored to the install identity.
        """
        self.root = Path(root)
        self._key = key
        self._dir = self.root / _SUBDIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def receipt_path(self, run_id: str) -> Path:
        """Return the JSONL path backing ``run_id``'s receipts."""
        safe = run_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"{safe}.jsonl"

    def _tail(self, run_id: str) -> tuple[str, int]:
        """Return ``(prev_hmac, next_hop_index)`` for the run.

        Reads the last JSONL line's ``hmac`` as the linkage anchor; a fresh
        run starts from :data:`GENESIS_HMAC` at index 0.
        """
        path = self.receipt_path(run_id)
        if not path.is_file():
            return GENESIS_HMAC, 0
        prev = GENESIS_HMAC
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            prev = obj.get("hmac", prev)
            count += 1
        return prev, count

    def record_hop(
        self,
        *,
        run_id: str,
        issuer: str,
        subject: str,
        audience: str,
        act: str,
        created: int | None = None,
    ) -> DelegationReceipt:
        """Append one delegation receipt and return it.

        Args:
            run_id: Run the delegation belongs to.
            issuer: The delegating party (RFC 8693 subject principal).
            subject: The acting party being authorized.
            audience: The target the delegated token is minted for.
            act: Symbolic name of the delegated action.
            created: Optional unix timestamp; defaults to now (exposed for
                deterministic tests).

        Returns:
            The freshly-appended, HMAC-computed receipt.
        """
        prev_hmac, hop_index = self._tail(run_id)
        ts = int(created if created is not None else time.time())
        body = {
            "run_id": run_id,
            "hop_index": hop_index,
            "issuer": issuer,
            "subject": subject,
            "audience": audience,
            "act": act,
            "created": ts,
            "prev_hmac": prev_hmac,
        }
        computed = _compute_hmac(self._key, prev_hmac, body)
        entry = dict(body)
        entry["hmac"] = computed
        path = self.receipt_path(run_id)
        with path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return DelegationReceipt(**entry)


def verify_run_chain(*, root: Path, run_id: str, key: bytes) -> ChainResult:
    """Reconstruct and verify a run's delegation chain offline.

    Walks ``<root>/delegation/<run_id>.jsonl`` from genesis, recomputing each
    hop's HMAC and checking linkage. Reconstructs the
    ``principal -> orchestrator -> sub-agent`` ordering (AC4).

    Args:
        root: Base directory the ledger was rooted at.
        run_id: Run to verify.
        key: The HMAC key (install audit key) the receipts were written with.

    Returns:
        A :class:`ChainResult`. ``valid`` is True only when at least one hop
        exists and the whole chain verifies from genesis to tail.
    """
    ledger_dir = Path(root) / _SUBDIR
    safe = run_id.replace("/", "_").replace("\\", "_")
    path = ledger_dir / f"{safe}.jsonl"
    if not path.is_file():
        return ChainResult(valid=False, hops=0, errors=["no delegation receipts for run"])

    receipts: list[DelegationReceipt] = []
    errors: list[str] = []
    prev_hmac = GENESIS_HMAC
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines()):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"hop {lineno}: malformed JSON: {exc}")
            break
        stored_hmac = obj.get("hmac", "")
        body = {k: v for k, v in obj.items() if k != "hmac"}
        if body.get("prev_hmac") != prev_hmac:
            errors.append(
                f"hop {body.get('hop_index', lineno)}: broken linkage (prev_hmac does not match preceding hop)"
            )
            break
        expected = _compute_hmac(key, prev_hmac, body)
        if not _hmac.compare_digest(expected, stored_hmac):
            errors.append(f"hop {body.get('hop_index', lineno)}: HMAC mismatch (receipt tampered or wrong key)")
            break
        receipts.append(DelegationReceipt(**obj))
        prev_hmac = stored_hmac

    valid = not errors and len(receipts) > 0
    return ChainResult(valid=valid, hops=len(receipts), receipts=receipts, errors=errors)


# ---------------------------------------------------------------------------
# Default-resolving convenience helpers (install-anchored key + audit root)
# ---------------------------------------------------------------------------


def _audit_key() -> bytes:
    """Return the install-scoped audit HMAC key (the delegation chain anchor)."""
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def default_ledger(root: Path | None = None) -> DelegationLedger:
    """Return a ledger rooted at the audit tree, keyed by the install audit key.

    Args:
        root: Optional root override; defaults to :data:`DEFAULT_ROOT`.

    Returns:
        A :class:`DelegationLedger` chained to the install identity's audit key.
    """
    return DelegationLedger(root=root or DEFAULT_ROOT, key=_audit_key())


def record_delegation_hop(
    *,
    run_id: str,
    issuer: str,
    subject: str,
    audience: str,
    act: str,
    root: Path | None = None,
) -> DelegationReceipt:
    """Record one delegation hop for ``run_id`` using install-anchored defaults.

    Convenience wrapper the orchestrator calls at each
    ``principal -> orchestrator -> sub-agent`` handoff.
    """
    return default_ledger(root).record_hop(run_id=run_id, issuer=issuer, subject=subject, audience=audience, act=act)


def verify_run(run_id: str, *, root: Path | None = None) -> ChainResult:
    """Verify a run's delegation chain using install-anchored defaults.

    Args:
        run_id: Run to verify.
        root: Optional root override; defaults to :data:`DEFAULT_ROOT`.

    Returns:
        The :class:`ChainResult` from :func:`verify_run_chain`.
    """
    return verify_run_chain(root=root or DEFAULT_ROOT, run_id=run_id, key=_audit_key())
