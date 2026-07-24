"""Tests for the canonical money/text core behind tenant showback statements.

Every case that a cross-language implementation must reproduce lives in
``tests/fixtures/showback/canonical_vectors.json`` so external fixtures can
extend the set without touching this file. The tests here bind the vectors
to the Python implementation and add the Python-only edge cases (exception
types, float-bridge guards, tree validation).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path

import pytest

from bernstein.core.cost.showback_canonical import (
    FloatRejectedError,
    MoneyFormatError,
    NonCanonicalTextError,
    canonical_statement_bytes,
    nano_usd_from_decimal_str,
    nano_usd_from_float,
    nano_usd_to_decimal_str,
    require_nfc,
)

VECTORS = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "showback" / "canonical_vectors.json").read_text(
        encoding="utf-8"
    )
)


# ---------------------------------------------------------------------------
# Money: canonical decimal-string parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", VECTORS["money_parse"], ids=lambda c: c["desc"])
def test_money_parse_vectors(case: dict) -> None:
    if "nano_usd" in case:
        assert nano_usd_from_decimal_str(case["input"]) == case["nano_usd"]
    else:
        with pytest.raises(MoneyFormatError):
            nano_usd_from_decimal_str(case["input"])


@pytest.mark.parametrize("case", VECTORS["money_render"], ids=lambda c: c["desc"])
def test_money_render_vectors(case: dict) -> None:
    assert nano_usd_to_decimal_str(case["nano_usd"]) == case["text"]


@pytest.mark.parametrize("nano", [0, 1, -1, 250_000_000, -999_999_999, 12_345_678_900_000_000])
def test_money_render_parse_roundtrip(nano: int) -> None:
    assert nano_usd_from_decimal_str(nano_usd_to_decimal_str(nano)) == nano


# ---------------------------------------------------------------------------
# Money: bridge from the float ledger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", VECTORS["float_bridge"], ids=lambda c: c["desc"])
def test_float_bridge_vectors(case: dict) -> None:
    assert nano_usd_from_float(case["cost_usd"]) == case["nano_usd"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_float_bridge_rejects_non_finite(bad: float) -> None:
    with pytest.raises(MoneyFormatError):
        nano_usd_from_float(bad)


def test_float_bridge_ties_round_half_even() -> None:
    # 1.5e-9 ties between 1 and 2 nano-USD and must land on the even digit;
    # 2.5e-9 ties between 2 and 3 and must also land on 2.
    assert nano_usd_from_float(1.5e-9) == 2
    assert nano_usd_from_float(2.5e-9) == 2


# ---------------------------------------------------------------------------
# Text: reject-don't-repair NFC policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", VECTORS["nfc"], ids=lambda c: c["desc"])
def test_nfc_vectors(case: dict) -> None:
    text = "".join(chr(int(cp[2:], 16)) for cp in case["codepoints"].split())
    if case.get("ok"):
        assert require_nfc(text) == text
    else:
        with pytest.raises(NonCanonicalTextError):
            require_nfc(text)


def test_require_nfc_never_transforms() -> None:
    # The accepted value is returned unchanged, not re-normalized: a verifier
    # must hash exactly the bytes it was handed.
    composed = unicodedata.normalize("NFC", "café")
    assert require_nfc(composed) is composed


# ---------------------------------------------------------------------------
# Statements: canonical bytes over a validated tree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", VECTORS["statements"], ids=lambda c: c["desc"])
def test_statement_vectors(case: dict) -> None:
    got = canonical_statement_bytes(case["payload"])
    assert hashlib.sha256(got).hexdigest() == case["sha256"]


def test_statement_bytes_independent_of_key_insertion_order() -> None:
    a = {"tenant": "core", "total_nano_usd": "250000000"}
    b = {"total_nano_usd": "250000000", "tenant": "core"}
    assert canonical_statement_bytes(a) == canonical_statement_bytes(b)


def test_statement_rejects_float_anywhere() -> None:
    with pytest.raises(FloatRejectedError):
        canonical_statement_bytes({"line_items": [{"cost_usd": 0.1}]})


def test_statement_rejects_non_nfc_value_and_key() -> None:
    decomposed = "cafe\u0301"  # 'e' + COMBINING ACUTE ACCENT, i.e. NFD form
    with pytest.raises(NonCanonicalTextError):
        canonical_statement_bytes({"label": decomposed})
    with pytest.raises(NonCanonicalTextError):
        canonical_statement_bytes({decomposed: "label"})


def test_statement_rejects_ints_outside_ijson_range() -> None:
    with pytest.raises(MoneyFormatError):
        canonical_statement_bytes({"n": 2**53})
    with pytest.raises(MoneyFormatError):
        canonical_statement_bytes({"n": -(2**53)})


def test_statement_accepts_bool_none_and_nested_containers() -> None:
    payload = {"ok": True, "gap": None, "items": [{"n": 1}, {"n": 2}]}
    assert canonical_statement_bytes(payload) == b'{"gap":null,"items":[{"n":1},{"n":2}],"ok":true}'
