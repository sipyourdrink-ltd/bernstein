"""One receipt protocol for every receipt kind the project emits.

:mod:`bernstein.core.receipts.protocol` holds the single envelope,
canonicalisation, ``sign_receipt`` and ``verify_receipt``; producing modules
register their kind against it instead of writing a verifier of their own.
"""

from __future__ import annotations

from bernstein.core.receipts.protocol import (
    CANONICALIZATION_V1,
    DuplicateReceiptKindError,
    ReceiptEnvelope,
    ReceiptProtocolError,
    ReceiptVerification,
    UnknownReceiptKindError,
    canonical_receipt_bytes,
    receipt_payload_digest,
    register_receipt_kind,
    registered_kinds,
    sign_receipt,
    verify_receipt,
)

__all__ = [
    "CANONICALIZATION_V1",
    "DuplicateReceiptKindError",
    "ReceiptEnvelope",
    "ReceiptProtocolError",
    "ReceiptVerification",
    "UnknownReceiptKindError",
    "canonical_receipt_bytes",
    "receipt_payload_digest",
    "register_receipt_kind",
    "registered_kinds",
    "sign_receipt",
    "verify_receipt",
]
