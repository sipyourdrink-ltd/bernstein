"""Money/text canonical-encoding policy tests for signed payment payloads.

The two policies vendored in :mod:`bernstein.core.payments._canonical` guard the
invariant that no float ever enters a signed mandate or receipt payload and that
recipient/category strings are rejected (never silently normalized) unless they
are already NFC.
"""

from __future__ import annotations

import unicodedata

import pytest

from bernstein.core.payments._canonical import (
    NANO_SCALE,
    format_nano_units,
    require_nfc,
    to_nano_units,
    validate_currency,
)


class TestMoney:
    def test_integer_amount_scales_to_nano_units(self) -> None:
        assert to_nano_units("40") == str(40 * 10**NANO_SCALE)

    def test_fractional_amount_scales_exactly(self) -> None:
        # 40.00 == 40 major units == 40e9 nano-units, no float drift.
        assert to_nano_units("40.00") == "40000000000"

    def test_smallest_unit_survives(self) -> None:
        assert to_nano_units("0.000000001") == "1"

    def test_half_even_rounds_once_at_the_nano_boundary(self) -> None:
        # 10.5 nano-units -> banker's rounding -> 10 (ties to even).
        assert to_nano_units("0.0000000105") == "10"
        # 11.5 nano-units -> ties to even -> 12.
        assert to_nano_units("0.0000000115") == "12"

    def test_no_float_ever_returned(self) -> None:
        out = to_nano_units("1.23")
        assert isinstance(out, str)
        assert "." not in out

    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "-1", "-0.5", "", "abc", "1e9999999999"])
    def test_rejects_non_finite_negative_and_garbage(self, bad: str) -> None:
        with pytest.raises(ValueError):
            to_nano_units(bad)

    def test_format_round_trips_for_display(self) -> None:
        assert format_nano_units("40000000000") == "40.000000000"
        assert format_nano_units("1") == "0.000000001"


class TestNfc:
    def test_accepts_already_nfc(self) -> None:
        assert require_nfc("café", field="recipient") == "café"  # precomposed é

    def test_rejects_non_nfc_never_normalizes(self) -> None:
        # 'e' + combining acute accent is NFD, not NFC.
        nfd = "café"
        assert unicodedata.normalize("NFC", nfd) != nfd
        with pytest.raises(ValueError):
            require_nfc(nfd, field="recipient")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            require_nfc("", field="category")


class TestCurrency:
    def test_accepts_iso4217_style_upper_ascii(self) -> None:
        assert validate_currency("USD") == "USD"
        assert validate_currency("EUR") == "EUR"

    @pytest.mark.parametrize("bad", ["usd", "US", "USDT", "US1", "", "$$$", "üsd"])
    def test_rejects_non_upper_ascii_or_wrong_length(self, bad: str) -> None:
        with pytest.raises(ValueError):
            validate_currency(bad)
