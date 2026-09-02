"""The one shape an offline verification answers in.

Every receipt in this repository can be re-checked without the process that
wrote it: read it back, recompute its hash, and confirm it is anchored in the
chain it claims. Eight modules did exactly that, and each declared its own
three-field result class to say how it went - same fields, same meaning, no
relationship between any two of them. A caller holding one of those results
could not be written to handle another without an adapter, so in practice the
generic caller was never written and every consumer was pinned to one verifier.

:class:`VerifyResult` is that shape declared once. It is generic in the receipt
it carries, so a module specialises it rather than redeclaring it::

    DispatchVerifyResult = VerifyResult[DispatchReceipt]

The specialisation keeps the receipt's static type - a caller that reads
``result.receipt.decision_hash`` still type-checks - while "did this pass, and
why" is now answerable the same way regardless of which verifier produced the
result.

The type is frozen because a verdict a caller can rewrite in place is not a
verdict. ``reason`` is empty on an ordinary pass, since there is nothing to
explain, but a verifier may still fill it in on a passing result to flag
something the caller should know - a degraded receipt that verified with no
window left to recompute, for instance. A failure always names its cause,
because the whole value of an offline check is that it says which field
stopped matching.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["VerifyResult"]


@dataclass(frozen=True, slots=True)
class VerifyResult[ReceiptT]:
    """Outcome of verifying a receipt offline.

    Attributes:
        ok: Whether the receipt read back, recomputed and anchored intact.
        reason: Why the verification failed, or ``""`` when it passed. A
            passing verification may still carry a note - a degraded receipt
            verifies but has no window to recompute, and says so here.
        receipt: The receipt that was checked, or ``None`` when there was
            nothing to check - no receipt at that hash, or none on disk.
    """

    ok: bool
    reason: str
    receipt: ReceiptT | None = None
