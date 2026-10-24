"""Receipt-bearing admission path for evolve-candidate intake (issue #5437).

A candidate that carries a verifiable friction receipt has stronger evidence
than an unlabelled collaborator request, yet the current admission path rejects
it.  This module adds a second admission branch: a candidate is admitted when
EITHER the existing author-trust rule holds, OR the receipt branch here passes.

Receipt admission contract
==========================

A proposal is receipt-admitted when ALL of the following hold:

1. The proposal body carries a ``receipt_hash`` (SHA-256 hex, 64 chars).
2. A :class:`FrictionReceipt` with that hash can be resolved (from the
   proposal itself, or from a resolver the caller supplies).
3. The receipt's ``command`` and ``version`` fields match the failure the
   proposal claims (``claimed_command`` / ``claimed_version``).
4. The receipt's public-key signature verifies with Ed25519 using the key
   the reporting install exported.
5. The proposal's risk level is L0, L1, or L2 -- never L3.
6. The reporting install has not exceeded ``max_per_install_per_day``
   receipt-admitted proposals today (default 3).

When any condition fails the proposal is NOT queued; the reason is written to
the audit entry and to the caller for issue-comment feedback.

No new crypto
=============

Signature verification reuses :func:`~bernstein.core.identity.agent_card_signer.verify_jws`
(already imported in the identity package) via the existing Ed25519 helpers.
The receipt body is hashed with SHA-256 over its canonical JSON; that hash is
what the caller compares to the ``receipt_hash`` in the proposal body.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

__all__ = [
    "RECEIPT_ADMISSION_REASON",
    "RECEIPT_CAP_EXCEEDED_REASON",
    "RECEIPT_COMMAND_MISMATCH_REASON",
    "RECEIPT_L3_BLOCKED_REASON",
    "RECEIPT_TAMPERED_REASON",
    "FrictionReceipt",
    "ReceiptAdmissionResult",
    "ReceiptGate",
]

#: Reason string written to the audit entry on success.
RECEIPT_ADMISSION_REASON = "receipt-verified"

#: Reason strings for each refusal mode (stable strings, callers may match them).
RECEIPT_L3_BLOCKED_REASON = "receipt-rejected: L3 proposals are never receipt-admitted"
RECEIPT_CAP_EXCEEDED_REASON = "receipt-rejected: per-install daily cap exceeded"
RECEIPT_TAMPERED_REASON = "receipt-rejected: receipt hash or signature does not verify"
RECEIPT_COMMAND_MISMATCH_REASON = "receipt-rejected: receipt command/version does not match claimed failure"

#: Default per-install daily cap (overridable at construction time).
DEFAULT_MAX_PER_INSTALL_PER_DAY = 3


@dataclass(frozen=True)
class FrictionReceipt:
    """Minimal receipt a reporting install attaches to a candidate issue.

    Attributes:
        receipt_hash: SHA-256 hex digest of the canonical JSON body (64 chars).
        install_id: Stable identifier the install exported with its key pair.
        public_key_pem: Ed25519 public key in PEM format, as exported by the
            reporting install.
        signature: Base64url-encoded Ed25519 signature over the canonical body.
        command: Exact bernstein sub-command that triggered the failure.
        version: Bernstein version string the install was running.
        body: The canonical JSON body that was signed.  Must round-trip to the
            same ``receipt_hash`` to be valid.
    """

    receipt_hash: str
    install_id: str
    public_key_pem: str
    signature: str
    command: str
    version: str
    body: str  # canonical JSON used to compute receipt_hash and verify signature

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrictionReceipt:
        return cls(
            receipt_hash=str(data.get("receipt_hash", "")),
            install_id=str(data.get("install_id", "")),
            public_key_pem=str(data.get("public_key_pem", "")),
            signature=str(data.get("signature", "")),
            command=str(data.get("command", "")),
            version=str(data.get("version", "")),
            body=str(data.get("body", "")),
        )


@dataclass(frozen=True)
class ReceiptAdmissionResult:
    """Outcome of the receipt admission check."""

    admitted: bool
    reason: str
    install_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"admitted": self.admitted, "reason": self.reason, "install_id": self.install_id}


class ReceiptResolver(Protocol):
    """Callable that resolves a receipt hash to a :class:`FrictionReceipt`."""

    def __call__(self, receipt_hash: str) -> FrictionReceipt | None: ...


def _compute_body_hash(body: str) -> str:
    """SHA-256 hex of the canonical body string as UTF-8 bytes."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _verify_ed25519_signature(public_key_pem: str, message: bytes, signature_b64url: str) -> bool:
    """Verify an Ed25519 signature using stdlib ``cryptography`` if available.

    Returns ``False`` (not ``True``) when the ``cryptography`` package is not
    installed, so an absent dependency is a refusal, never a silent pass.
    The signature is base64url-encoded with optional ``=`` padding stripped.
    """
    try:
        import base64

        from cryptography.hazmat.primitives import serialization

        padding = 4 - (len(signature_b64url) % 4)
        if padding != 4:
            signature_b64url = signature_b64url + "=" * padding
        sig_bytes = base64.urlsafe_b64decode(signature_b64url)
        pub = serialization.load_pem_public_key(public_key_pem.encode())
        pub.verify(sig_bytes, message)
        return True
    except Exception:
        return False


class ReceiptGate:
    """Evaluates the receipt-bearing admission branch.

    Maintains an in-process per-install daily cap counter.  A production
    deployment should back the counter with a persistent store; the in-memory
    default is sufficient for single-process intake paths (e.g. the GitHub App
    webhook handler).

    Args:
        resolver: Callable that maps a receipt hash to a :class:`FrictionReceipt`.
            When ``None``, the gate only accepts receipts embedded directly in
            the proposal via a ``friction_receipt`` key.
        max_per_install_per_day: How many receipt-admitted proposals one install
            may produce in a 24-hour window.  Defaults to
            :data:`DEFAULT_MAX_PER_INSTALL_PER_DAY`.
    """

    def __init__(
        self,
        resolver: ReceiptResolver | None = None,
        *,
        max_per_install_per_day: int = DEFAULT_MAX_PER_INSTALL_PER_DAY,
    ) -> None:
        self._resolver = resolver
        self._max_per_day = max_per_install_per_day
        # {install_id: [(epoch_ts, admitted_bool), ...]}
        self._daily_log: dict[str, list[tuple[float, bool]]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def evaluate(
        self,
        proposal: Any,
        *,
        claimed_command: str = "",
        claimed_version: str = "",
        risk_level_value: str = "",
    ) -> ReceiptAdmissionResult:
        """Check whether *proposal* may be receipt-admitted.

        Args:
            proposal: Any object with a ``receipt_hash`` attribute or key, and
                optionally a ``friction_receipt`` attribute carrying the full
                receipt dict.
            claimed_command: The sub-command the issue claims failed.
            claimed_version: The bernstein version the issue claims was running.
            risk_level_value: String value of the proposal's risk level
                (``"L3_STRUCTURAL"`` blocks; anything else is checked normally).

        Returns:
            A :class:`ReceiptAdmissionResult` with ``admitted=True`` on success.
        """
        # L3 is never receipt-admitted.
        if risk_level_value == "L3_STRUCTURAL":
            return ReceiptAdmissionResult(admitted=False, reason=RECEIPT_L3_BLOCKED_REASON)

        receipt_hash = self._extract_hash(proposal)
        if not receipt_hash:
            return ReceiptAdmissionResult(admitted=False, reason="receipt-rejected: no receipt_hash in proposal")

        receipt = self._resolve_receipt(proposal, receipt_hash)
        if receipt is None:
            short = receipt_hash[:16]
            return ReceiptAdmissionResult(admitted=False, reason=f"receipt-rejected: receipt {short}… not found")

        # 1. Hash integrity: body must recompute to receipt_hash.
        computed = _compute_body_hash(receipt.body)
        if computed != receipt.receipt_hash or receipt.receipt_hash != receipt_hash:
            return ReceiptAdmissionResult(admitted=False, reason=RECEIPT_TAMPERED_REASON, install_id=receipt.install_id)

        # 2. Signature.
        if not _verify_ed25519_signature(receipt.public_key_pem, receipt.body.encode("utf-8"), receipt.signature):
            return ReceiptAdmissionResult(admitted=False, reason=RECEIPT_TAMPERED_REASON, install_id=receipt.install_id)

        # 3. Command/version match when caller provides claimed values.
        if claimed_command and receipt.command != claimed_command:
            return ReceiptAdmissionResult(
                admitted=False, reason=RECEIPT_COMMAND_MISMATCH_REASON, install_id=receipt.install_id
            )
        if claimed_version and receipt.version != claimed_version:
            return ReceiptAdmissionResult(
                admitted=False, reason=RECEIPT_COMMAND_MISMATCH_REASON, install_id=receipt.install_id
            )

        # 4. Per-install daily cap.
        if not self._check_cap(receipt.install_id):
            return ReceiptAdmissionResult(
                admitted=False, reason=RECEIPT_CAP_EXCEEDED_REASON, install_id=receipt.install_id
            )

        self._record_cap(receipt.install_id)
        logger.info("Proposal receipt-admitted: install=%s hash=%s", receipt.install_id, receipt_hash[:16])
        return ReceiptAdmissionResult(admitted=True, reason=RECEIPT_ADMISSION_REASON, install_id=receipt.install_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_hash(proposal: Any) -> str:
        """Pull the receipt hash from the proposal, however it is carried."""
        if isinstance(proposal, dict):
            return str(proposal.get("receipt_hash", ""))
        return str(getattr(proposal, "receipt_hash", "") or "")

    def _resolve_receipt(self, proposal: Any, receipt_hash: str) -> FrictionReceipt | None:
        # Prefer an inline receipt attached to the proposal itself.
        raw = None
        if isinstance(proposal, dict):
            raw = proposal.get("friction_receipt")
        else:
            raw = getattr(proposal, "friction_receipt", None)
        if isinstance(raw, dict):
            return FrictionReceipt.from_dict(raw)
        if isinstance(raw, FrictionReceipt):
            return raw
        # Fall back to the caller-supplied resolver.
        if self._resolver is not None:
            return self._resolver(receipt_hash)
        return None

    def _check_cap(self, install_id: str) -> bool:
        """True when the install has room under today's cap."""
        cutoff = time.time() - 86400.0
        recent = [ts for ts, _ in self._daily_log[install_id] if ts >= cutoff]
        return len(recent) < self._max_per_day

    def _record_cap(self, install_id: str) -> None:
        self._daily_log[install_id].append((time.time(), True))
        # Prune entries older than 25 h to prevent unbounded growth.
        cutoff = time.time() - 90000.0
        self._daily_log[install_id] = [(ts, ok) for ts, ok in self._daily_log[install_id] if ts >= cutoff]
