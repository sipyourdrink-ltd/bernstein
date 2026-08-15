"""One card-vector table, exercised through every Luhn call site (issue #3820).

The point of the table is not that Luhn is implemented correctly -- each site
was correct for its own inputs. It is that the *length policy* lived in three
places, so a change to "what counts as a card" had three places to be made and
nothing noticed when only two of them moved.

Where the sites legitimately differ, the difference is named here rather than
unified:

* ``log_redact`` sees only 16-digit runs (its regex fixes the length), so it
  needs no bound of its own.
* ``pii_output_gate`` bounds ``13 <= n <= 19``.
* ``dlp_scanner_v2`` bounds ``n >= 13`` with **no upper bound**.

That last row is a real disagreement: a 25-digit Luhn-valid run is a card to
``dlp_scanner_v2`` and not to ``pii_output_gate``. It is pinned below rather
than changed -- narrowing it is a policy decision, not a de-duplication.
"""

from __future__ import annotations

import pytest

from bernstein.core.observability.log_redact import redact_pii
from bernstein.core.security.dlp_scanner_v2 import _validate_credit_card
from bernstein.core.security.luhn import luhn_check
from bernstein.core.security.pii_output_gate import _looks_like_credit_card

#: ``(digits, passes_luhn)``. Checksum truth only -- no length policy.
LUHN_VECTORS: list[tuple[str, bool]] = [
    # Well-known test PANs (all Luhn-valid).
    ("4111111111111111", True),  # Visa, 16
    ("5500005555555559", True),  # Mastercard, 16
    ("378282246310005", True),  # Amex, 15
    ("6011111111111117", True),  # Discover, 16
    ("30569309025904", True),  # Diners, 14
    ("4222222222222", True),  # Visa, 13
    # Same numbers with the final check digit disturbed.
    ("4111111111111112", False),
    ("5500005555555558", False),
    ("378282246310006", False),
    ("6011111111111118", False),
    # Shape lookalikes that are not cards: trace ids, ledger sequences.
    ("1234567890123456", False),
    ("0000000000000001", False),
    # All zeroes checksums to 0 -- Luhn-valid, which is exactly why a
    # checksum alone is not a card test.
    ("0000000000000000", True),
]


@pytest.mark.parametrize(("digits", "expected"), LUHN_VECTORS)
def test_shared_luhn_check_matches_the_table(digits: str, expected: bool) -> None:
    assert luhn_check(digits) is expected


@pytest.mark.parametrize(("digits", "expected"), LUHN_VECTORS)
def test_dlp_scanner_v2_agrees_on_13_to_19_digit_vectors(digits: str, expected: bool) -> None:
    """``dlp_scanner_v2`` accepts >= 13 digits, so the whole table applies."""
    assert _validate_credit_card(digits) is expected


@pytest.mark.parametrize(("digits", "expected"), LUHN_VECTORS)
def test_pii_output_gate_agrees_on_13_to_19_digit_vectors(digits: str, expected: bool) -> None:
    """Every vector is 13-19 digits, inside the gate's own bounds."""
    assert _looks_like_credit_card(digits) is expected


@pytest.mark.parametrize(("digits", "expected"), LUHN_VECTORS)
def test_log_redact_agrees_on_16_digit_vectors(digits: str, expected: bool) -> None:
    """``log_redact`` only ever sees 16-digit runs, so only those apply."""
    if len(digits) != 16:
        pytest.skip("log_redact's regex matches 16-digit runs only")
    redacted = redact_pii(f"card={digits}")
    assert (digits not in redacted) is expected


# ---------------------------------------------------------------------------
# Separator handling: each site strips differently, and must keep doing so
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", ["4111 1111 1111 1111", "4111-1111-1111-1111"])
def test_all_sites_see_through_separators(spelling: str) -> None:
    assert _validate_credit_card(spelling) is True
    assert _looks_like_credit_card(spelling) is True
    assert "4111" not in redact_pii(f"card={spelling}")


# ---------------------------------------------------------------------------
# Length policy: where the sites legitimately disagree
# ---------------------------------------------------------------------------


def test_length_policy_disagreement_is_deliberate() -> None:
    """A 25-digit Luhn-valid run: a card to dlp_scanner_v2, not to the gate.

    Pinned, not unified. ``dlp_scanner_v2._validate_credit_card`` bounds only
    ``>= 13``; ``pii_output_gate`` bounds ``13 <= n <= 19``. Whether the
    scanner should gain an upper bound is a policy change with its own
    decision to make.
    """
    long_run = "4" + "0" * 23 + "6"
    assert len(long_run) == 25
    assert luhn_check(long_run) is True

    assert _validate_credit_card(long_run) is True
    assert _looks_like_credit_card(long_run) is False


@pytest.mark.parametrize("digits", ["", "411111111111111"[:12], "4"])
def test_runs_below_thirteen_digits_are_not_cards_anywhere(digits: str) -> None:
    assert _validate_credit_card(digits) is False
    assert _looks_like_credit_card(digits) is False


# ---------------------------------------------------------------------------
# Non-digit input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "abcd", "4111-1111-1111-111x"])
def test_shared_luhn_check_rejects_non_digit_input(value: str) -> None:
    assert luhn_check(value) is False


def test_shared_luhn_check_does_not_raise_on_non_decimal_digits() -> None:
    """``str.isdigit()`` accepts superscripts that ``int()`` refuses.

    ``"\\u00b2".isdigit()`` is True but ``int("\\u00b2")`` raises ValueError, so an
    ``isdigit()`` guard in front of ``int()`` is a latent crash. No current
    caller can deliver one (both strip to ``\\d``, which is decimals only), but
    the shared helper is now reachable from anywhere, so it must not be a trap.
    """
    assert luhn_check("41111111111111²") is False
