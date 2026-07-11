"""Verifiable spending mandates recorded as journal-anchored consent receipts.

Issue #2306. When an agent spends against an external service the operator
needs non-repudiable proof that a specific payment was authorized by a
specific intent, plus revocable authority they can bound and audit. The
lineage spine (issue #2292) is the natural home: a signed spending mandate,
the tool calls it authorizes, and the settlement reference become one
content-addressed record so "this payment was authorized by this exact
intent" is provable offline.

The public surface lives in :mod:`bernstein.core.protocols.payments.mandates`
and models the AP2-style two-level mandate shape (an Intent Mandate for what
the operator wants, a Cart Mandate for the concrete action) plus the HTTP 402
pay-and-retry settlement flow.
"""

from __future__ import annotations

from bernstein.core.protocols.payments.mandates import (
    MANDATE_RUN_ID,
    CartMandate,
    ConsentReceipt,
    IntentMandate,
    MandateVerifyResult,
    RevocationEntry,
    SettlementRef,
    authorized_action_set,
    emit_consent_receipt,
    is_revoked,
    revoke_mandate,
    verify_consent_receipt,
)

__all__ = [
    "MANDATE_RUN_ID",
    "CartMandate",
    "ConsentReceipt",
    "IntentMandate",
    "MandateVerifyResult",
    "RevocationEntry",
    "SettlementRef",
    "authorized_action_set",
    "emit_consent_receipt",
    "is_revoked",
    "revoke_mandate",
    "verify_consent_receipt",
]
