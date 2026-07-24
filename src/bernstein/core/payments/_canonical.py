"""Canonical money/text encoding policies for signed payment payloads.

Two invariants keep a signed mandate or receipt reproducible and safe:

* **Money never touches float.** Amounts are carried as string-encoded
  fixed-scale integer *nano-units* (1 unit = ``1e-9`` of the currency's major
  unit). A real-world decimal amount is converted to nano-units exactly once,
  rounding half-even at the nano boundary, and every downstream comparison and
  sum is exact integer arithmetic. No IEEE-754 double ever enters a payload
  that gets hashed and signed.
* **Text is rejected, never normalized.** ``recipient`` and ``category`` must
  already be NFC. A non-NFC string is refused rather than silently rewritten,
  so the bytes the operator signed are the exact bytes a verifier re-hashes;
  normalizing on the write path would let two different inputs collapse to one
  signed form.

Provenance note
---------------
These are local vendored copies of the money/text policies being consolidated
for showback statements (issues #2554 / #2868). Once that shared module lands
on ``main`` this file should be deleted and callers pointed at
``bernstein.core.cost.showback_canonical`` so there is a single rounding site.

TODO(#2554): replace this module with imports from
``bernstein.core.cost.showback_canonical`` when it is available on ``main``.
Do not import it from a feature branch.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

__all__ = [
    "NANO_SCALE",
    "format_nano_units",
    "require_nfc",
    "to_nano_units",
    "validate_currency",
]

#: Fixed decimal scale of the minor unit. ``9`` == nano-units: one integer
#: nano-unit is ``1e-9`` of the currency's major unit. Chosen wide enough to
#: carry sub-cent metered-API pricing without rounding, narrow enough that the
#: integer count of a realistic mandate cap stays well inside 64 bits when it
#: matters (arithmetic here is Python big-int, so there is no overflow, but the
#: scale is documented so a reader knows the boundary the single rounding uses).
NANO_SCALE: int = 9

#: Quantum used for the single half-even rounding: ``Decimal("1e-9")``.
_NANO_QUANTUM: Decimal = Decimal(1).scaleb(-NANO_SCALE)

#: ISO-4217-style code: exactly three uppercase ASCII letters. Uppercase is a
#: hard requirement -- a lowercase code is rejected, never up-cased, to keep the
#: "reject, never normalize" discipline consistent between money and text.
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def to_nano_units(amount: str) -> str:
    """Convert a decimal *amount* string to an integer nano-unit string.

    The conversion rounds half-even at the nano boundary exactly once. This is
    the single rounding site: callers pass the raw operator-supplied decimal
    and every later comparison or sum uses the returned integer string.

    Args:
        amount: A finite, non-negative decimal in the currency's major unit,
            e.g. ``"40"``, ``"40.00"``, ``"0.000000001"``.

    Returns:
        The amount as a base-10 integer count of nano-units, e.g. ``"40"`` ->
        ``"40000000000"``.

    Raises:
        ValueError: If *amount* is not a finite, non-negative decimal, or is
            not parseable. NaN and the infinities are rejected explicitly.
    """
    if not isinstance(amount, str):
        raise ValueError(f"amount must be a string, got {type(amount).__name__}")
    text = amount.strip()
    if not text:
        raise ValueError("amount must not be empty")
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"amount is not a valid decimal: {amount!r}") from exc
    if not dec.is_finite():
        raise ValueError(f"amount must be finite (no NaN/Infinity): {amount!r}")
    if dec < 0:
        raise ValueError(f"amount must not be negative: {amount!r}")
    # Single half-even quantization to the nano boundary, then scale to an
    # integer count. ``quantize`` on an already-finite Decimal cannot raise
    # here because the quantum is coarser than any realistic input; a wildly
    # over-precise input simply rounds.
    try:
        quantized = dec.quantize(_NANO_QUANTUM, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise ValueError(f"amount could not be quantized to nano-units: {amount!r}") from exc
    nanos = int((quantized * (10**NANO_SCALE)).to_integral_value())
    return str(nanos)


def format_nano_units(nanos: str) -> str:
    """Render an integer nano-unit string back to a fixed-scale decimal string.

    Display-only inverse of :func:`to_nano_units`; the result always carries
    exactly :data:`NANO_SCALE` fractional digits so it is stable to diff.

    Args:
        nanos: A base-10 integer nano-unit string.

    Returns:
        The value in major units with ``NANO_SCALE`` fractional digits, e.g.
        ``"40000000000"`` -> ``"40.000000000"``.

    Raises:
        ValueError: If *nanos* is not a base-10 integer string.
    """
    try:
        value = int(nanos)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"nanos must be an integer string: {nanos!r}") from exc
    dec = (Decimal(value) * _NANO_QUANTUM).quantize(_NANO_QUANTUM)
    return f"{dec:.{NANO_SCALE}f}"


def require_nfc(value: str, *, field: str) -> str:
    """Return *value* unchanged iff it is already NFC; otherwise raise.

    The string is never normalized here -- a non-NFC input is a caller error
    surfaced loudly, so the signed bytes equal the input bytes.

    Args:
        value: The text to check (a recipient id or a category label).
        field: Field name used in the error message.

    Returns:
        *value* unchanged.

    Raises:
        ValueError: If *value* is empty or not in NFC form.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field} must be NFC-normalized; it is rejected (never auto-normalized): {value!r}")
    return value


def validate_currency(code: str) -> str:
    """Return *code* unchanged iff it is an ISO-4217-style uppercase code.

    Args:
        code: A three-letter uppercase ASCII currency code, e.g. ``"USD"``.

    Returns:
        *code* unchanged.

    Raises:
        ValueError: If *code* is not exactly three uppercase ASCII letters.
    """
    if not isinstance(code, str) or not _CURRENCY_RE.match(code):
        raise ValueError(f"currency must be an ISO-4217-style 3-letter uppercase ASCII code: {code!r}")
    return code
