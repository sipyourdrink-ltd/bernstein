"""Canonical money and text core for tenant showback statements (#2554).

Showback statements are meant to be recomputed byte-identically by two
independent parties and verified offline, so every value that enters a
statement must have exactly one encoding. This module fixes the three
policies everything downstream builds on:

**Money is a fixed-scale integer.** Amounts are integers of nano-USD
(1 USD = 10**9 nano-USD). Rounding happens exactly once, when a line item
is created from the float spend ledger (:func:`nano_usd_from_float`,
banker's rounding); totals are integer sums of those exact values, so
aggregation order can never change a digit. Inside statement payloads,
amounts travel as *string-encoded* integers ("250000000"), keeping every
number in the payload inside the I-JSON interoperable range.

**Text is rejected, not repaired.** Every string in a statement must
already be in Unicode NFC (:func:`require_nfc`). Normalizing on behalf of
the caller would mean the verifier transforms input before hashing it,
and normalization tables move between Unicode versions for unassigned
code points; rejection is stable, and data accepted once stays NFC under
every later Unicode version by the normalization stability policy.

**Floats never reach canonical bytes.** :func:`canonical_statement_bytes`
walks the payload tree, rejects any ``float``, any non-NFC string, and
any integer outside the I-JSON safe range, then delegates the encoding to
the shared RFC 8785 canonicalizer
(:func:`bernstein.core.security.agent_card_signer.canonicalize_jcs`).

Cross-language test vectors live in
``tests/fixtures/showback/canonical_vectors.json``; extend the vectors
file rather than encoding new cases in test code.
"""

from __future__ import annotations

import math
import re
import unicodedata
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from bernstein.core.security.agent_card_signer import canonicalize_jcs

__all__ = [
    "NANO_USD_PER_USD",
    "CanonicalStatementError",
    "FloatRejectedError",
    "MoneyFormatError",
    "NonCanonicalTextError",
    "canonical_statement_bytes",
    "nano_usd_from_decimal_str",
    "nano_usd_from_float",
    "nano_usd_to_decimal_str",
    "require_nfc",
]

#: Fixed money scale: one USD in nano-USD.
NANO_USD_PER_USD: int = 10**9

#: JSON numbers outside ``(-2**53, 2**53)`` are not interoperable (RFC 7493
#: I-JSON); statement payloads must string-encode anything that large.
_IJSON_MAX_INT: int = 2**53

#: Canonical decimal-string grammar: optional minus, integer part without
#: leading zeros, optional fraction of at most nine digits. No exponent,
#: no plus sign, no whitespace, ASCII digits only.
_MONEY_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?", re.ASCII)

_NANO_QUANTUM = Decimal(1).scaleb(-9)


class CanonicalStatementError(ValueError):
    """A value cannot appear in a canonical showback statement."""


class MoneyFormatError(CanonicalStatementError):
    """A monetary value is not in the canonical fixed-scale form."""


class NonCanonicalTextError(CanonicalStatementError):
    """A string is not in Unicode NFC; the caller must fix its input."""


class FloatRejectedError(CanonicalStatementError):
    """A float reached a statement payload; money must be fixed-scale."""


def require_nfc(text: str) -> str:
    """Return ``text`` unchanged if it is NFC; raise otherwise.

    The value is deliberately never normalized here: a verifier must hash
    exactly the bytes it was handed, and silent repair would let two
    parties sign different bytes for the "same" statement.
    """
    if not unicodedata.is_normalized("NFC", text):
        raise NonCanonicalTextError(
            f"text is not NFC-normalized: {text!r}; normalize at the input boundary before it enters a statement"
        )
    return text


def nano_usd_from_decimal_str(text: str) -> int:
    """Parse a canonical decimal USD string into integer nano-USD.

    Accepts at most nine fractional digits and rejects every notation
    with more than one spelling (exponents, leading zeros, ``+``, ``-0``,
    bare or trailing dots, whitespace, non-ASCII digits) so a given
    amount has exactly one accepted text form per fraction length.
    """
    if not _MONEY_RE.fullmatch(text):
        raise MoneyFormatError(f"not a canonical decimal USD amount: {text!r}")
    negative = text.startswith("-")
    body = text[1:] if negative else text
    integer_part, _, fraction = body.partition(".")
    nanos = int(integer_part) * NANO_USD_PER_USD + int(fraction.ljust(9, "0") or "0")
    if negative:
        if nanos == 0:
            raise MoneyFormatError("negative zero is not a canonical amount")
        nanos = -nanos
    return nanos


def nano_usd_from_float(cost_usd: float) -> int:
    """Convert a float ledger amount to nano-USD, rounding exactly once.

    The float is read through its shortest round-trip decimal (``repr``),
    then quantized to one nano-USD with banker's rounding. This is the
    single place a showback amount may be rounded; everything downstream
    works on the exact integer.
    """
    if not math.isfinite(cost_usd):
        raise MoneyFormatError(f"non-finite amount: {cost_usd!r}")
    quantized = Decimal(repr(cost_usd)).quantize(_NANO_QUANTUM, rounding=ROUND_HALF_EVEN)
    return int(quantized.scaleb(9))


def nano_usd_to_decimal_str(nanos: int) -> str:
    """Render integer nano-USD in the canonical decimal form.

    Always nine fractional digits: one rule, no trimming ambiguity, and
    ``nano_usd_from_decimal_str`` round-trips the result exactly.
    """
    sign = "-" if nanos < 0 else ""
    whole, frac = divmod(abs(nanos), NANO_USD_PER_USD)
    return f"{sign}{whole}.{frac:09d}"


def canonical_statement_bytes(payload: Any) -> bytes:
    """Encode a statement payload as RFC 8785 canonical JSON bytes.

    Validates the whole tree first: floats are rejected outright (money
    must already be fixed-scale, string-encoded), every string key and
    value must be NFC, and integers must stay inside the I-JSON safe
    range. Only a payload that passes untransformed is encoded.
    """
    _validate_tree(payload, "$")
    return canonicalize_jcs(payload)


def _validate_tree(value: Any, path: str) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, float):
        raise FloatRejectedError(
            f"float at {path}: statement amounts must be string-encoded integer nano-USD, not floats"
        )
    if isinstance(value, int):
        if not -_IJSON_MAX_INT < value < _IJSON_MAX_INT:
            raise MoneyFormatError(f"integer at {path} exceeds the I-JSON safe range; string-encode it")
        return
    if isinstance(value, str):
        try:
            require_nfc(value)
        except NonCanonicalTextError as exc:
            raise NonCanonicalTextError(f"at {path}: {exc}") from None
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_tree(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalStatementError(f"non-string key at {path}: {key!r}")
            try:
                require_nfc(key)
            except NonCanonicalTextError as exc:
                raise NonCanonicalTextError(f"key at {path}: {exc}") from None
            _validate_tree(item, f"{path}.{key}")
        return
    raise CanonicalStatementError(f"unsupported type at {path}: {type(value).__name__}")
