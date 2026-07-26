"""RFC 8785 property-name ordering for ``canonicalize_jcs`` (#3105).

RFC 8785 section 3.2.3 sorts object property names as arrays of UTF-16 code
units.  Sorting by Unicode code point agrees with that everywhere except one
case: a supplementary-plane name (above U+FFFF) compared against a name
starting in U+E000 to U+FFFF.  In UTF-16 the supplementary name begins with a
high surrogate in U+D800 to U+DBFF, which sorts below that range, while its
code point sorts above it.

``canonicalize_jcs`` produces the bytes that get signed, so the two orders
producing different bytes means an independent RFC 8785 verifier recomputing
over the same object reads a valid signature as invalid.

The expected byte strings below are derived from the RFC rule directly (sort
the names by their UTF-16BE encoding, which is a byte-order-preserving
serialisation of the code-unit array) rather than from an implementation, so
this file does not inherit any implementation's opinion.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.security.agent_card_signer import canonicalize_jcs

# The minimal disagreeing pair from the issue: U+10000 (supplementary,
# UTF-16 D800 DC00) against U+FF21 (BMP, above the surrogate range).
_LOW_IN_UTF16 = chr(0x10000)
_HIGH_IN_UTF16 = chr(0xFF21)


def _rfc8785_order(obj: dict[str, object]) -> bytes:
    """Encode *obj* with property names sorted per RFC 8785 section 3.2.3."""
    ordered = {key: obj[key] for key in sorted(obj, key=lambda name: name.encode("utf-16-be"))}
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def test_supplementary_name_sorts_below_private_use_name() -> None:
    """The exact pair named in the issue, pinned to literal bytes."""
    obj = {_LOW_IN_UTF16: 1, _HIGH_IN_UTF16: 2}

    assert canonicalize_jcs(obj) == b'{"\xf0\x90\x80\x80":1,"\xef\xbc\xa1":2}'


def test_code_point_order_would_differ() -> None:
    """Guard: the vector is only meaningful because the two orders disagree.

    If a future change made code-point order agree here, this test would stop
    proving anything, so assert the disagreement explicitly.
    """
    obj = {_LOW_IN_UTF16: 1, _HIGH_IN_UTF16: 2}
    code_point_bytes = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    assert canonicalize_jcs(obj) != code_point_bytes


@pytest.mark.parametrize(
    "obj",
    [
        # Supplementary name against the whole U+E000..U+FFFF band.
        {chr(0x1F600): 1, chr(0xE000): 2, chr(0xFFFD): 3},
        # Mixed planes, including names the two orders agree on.
        {"a": 0, chr(0x1F600): 1, chr(0xE000): 2, chr(0xFFFD): 3, chr(0x4E2D): 4},
        # Two supplementary names against each other.
        {chr(0x10000): 1, chr(0x10FFFD): 2, chr(0xE000): 3},
        # Nested objects are canonicalized too, not just the root.
        {"outer": {chr(0x20000): 1, chr(0xF900): 2}},
        # Inside an array element.
        {"items": [{chr(0x1F600): 1, chr(0xFFFD): 2}]},
    ],
)
def test_property_names_sort_by_utf16_code_units(obj: dict[str, object]) -> None:
    assert canonicalize_jcs(obj) == _rfc8785_order_deep(obj)


def _rfc8785_order_deep(value: object) -> bytes:
    return json.dumps(
        _sorted_tree(value),
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sorted_tree(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _sorted_tree(value[key])  # type: ignore[index]
            for key in sorted(value, key=lambda name: str(name).encode("utf-16-be"))
        }
    if isinstance(value, list):
        return [_sorted_tree(item) for item in value]
    return value


def test_ascii_and_bmp_payloads_are_byte_identical_to_code_point_order() -> None:
    """Every name below U+D800 keeps the bytes it had before this change.

    All shipped signing surfaces use ASCII property names, so this is the
    property that makes the fix non-breaking for them.
    """
    payloads: list[dict[str, object]] = [
        {"alg": "EdDSA", "kid": "k1", "typ": "JOSE"},
        {"agent_id": "a", "role": "backend", "task_ids": ["t1"], "nested": {"b": 1, "a": 2}},
        {"café": 1, "中": 2, "z": 3, "퟿": 4},
    ]
    for payload in payloads:
        code_point_bytes = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

        assert canonicalize_jcs(payload) == code_point_bytes, payload


def test_non_string_property_names_still_encode() -> None:
    """Integer and boolean names keep working, coerced as JSON requires."""
    assert canonicalize_jcs({2: "b", 10: "a"}) == b'{"10":"a","2":"b"}'
    assert canonicalize_jcs({True: 1, None: 2}) == b'{"null":2,"true":1}'
