"""RFC 8785 number serialisation for ``canonicalize_jcs``.

RFC 8785 section 3.2.2.3 defers the number rule to ECMAScript's
``Number::toString`` (ECMA-262 7.1.12.1). Python's ``repr`` generates the
same shortest-round-trip digits, but lays them out differently in three
places, and each difference changes the bytes that get signed:

* an integer-valued float keeps a trailing ``.0`` (``10.0`` instead of ``10``)
* the exponent is padded (``1e-07`` instead of ``1e-7``)
* the switch into scientific notation happens at different magnitudes than
  ES6's ``-6 < n <= 21`` window (``1e+20`` instead of the 21 digits)

Signed card bodies carry ``max_budget_usd``, ``created_at`` and
``expires_at`` as floats, so an independent RFC 8785 verifier recomputing
over the same body read a valid Bernstein signature as invalid whenever one
of those values landed on an integer boundary.

The expected strings below are the ES6 rule applied by hand, not the output
of any implementation, so this file does not inherit one implementation's
opinion. The reference-vector cases in
``tests/property/test_a2a_card_bughunt.py`` cross-check the same rule
against the published RFC 8785 vectors.
"""

from __future__ import annotations

import math

import pytest

from bernstein.core.security.agent_card_signer import (
    JCS_CANONICALIZATION_VERSION,
    canonicalize_jcs,
)


def _canon(value: object) -> str:
    return canonicalize_jcs(value).decode("utf-8")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # An integer-valued float is an integer. RFC 8785 3.2.2.3.
        (0.0, "0"),
        (1.0, "1"),
        (10.0, "10"),
        (100.0, "100"),
        (56.0, "56"),
        # Negative zero normalises; the sign is not part of the value.
        (-0.0, "0"),
        # Ordinary fractions keep the shortest round-trip digits.
        (0.5, "0.5"),
        (1.5, "1.5"),
        (-1.5, "-1.5"),
        (4.50, "4.5"),
        (123.456, "123.456"),
        (333333333.33333329, "333333333.3333333"),
        # ES6 uses decimal notation while -6 < n <= 21, so 1e20 is written
        # out in full and 1e21 is the first magnitude to go scientific.
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1e30, "1e+30"),
        # The same boundary on the small side: 1e-6 is decimal, 1e-7 is not.
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (2e-3, "0.002"),
        (1e-27, "1e-27"),
        # No padded exponent, and the sign is explicit in both directions.
        (2.5e-10, "2.5e-10"),
        (5e-324, "5e-324"),
    ],
)
def test_number_follows_the_es6_layout_rule(value: float, expected: str) -> None:
    assert _canon(value) == expected


@pytest.mark.parametrize("value", [1.0, 1e0, 100e-2, 1.000])
def test_every_spelling_of_one_lands_on_the_same_bytes(value: float) -> None:
    """Two operators writing the same number must sign the same bytes."""
    assert _canon(value) == "1"
    assert _canon(value) == _canon(1)


def test_integers_are_emitted_exactly() -> None:
    """A count past 2**53 keeps the value the caller signed.

    The showback statement vectors carry ``nano_usd`` values that
    deliberately exceed the double-exact integer range, so routing an int
    through the double path would change a signed number rather than
    reformat it.
    """
    beyond_double = 2**53 + 1
    assert _canon(beyond_double) == str(beyond_double)
    assert _canon(9007199254740993) == "9007199254740993"


def test_bool_is_not_a_number() -> None:
    """``bool`` subclasses ``int``; it must not fall through to the int arm."""
    assert _canon(True) == "true"
    assert _canon(False) == "false"
    assert _canon([True, 1]) == "[true,1]"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_floats_are_refused(value: float) -> None:
    """RFC 8785 has no encoding for these, so there is no answer to give."""
    with pytest.raises(ValueError, match="not JSON compliant"):
        canonicalize_jcs(value)


def test_unsupported_type_still_raises_type_error() -> None:
    with pytest.raises(TypeError, match="not JSON serializable"):
        canonicalize_jcs({"a": {1, 2}})


def test_numbers_nested_in_containers_use_the_same_rule() -> None:
    assert _canon({"b": [10.0, 1e-7], "a": -0.0}) == '{"a":0,"b":[10,1e-7]}'


def test_payload_without_a_float_is_unchanged_by_this_revision() -> None:
    """Revision 3 moves float bytes only; nothing else was touched."""
    payload = {"n": 42, "s": "text", "b": True, "z": None, "l": [1, 2, 3]}
    assert _canon(payload) == '{"b":true,"l":[1,2,3],"n":42,"s":"text","z":null}'


def test_canonicalization_version_records_the_number_rule() -> None:
    assert JCS_CANONICALIZATION_VERSION == 3
