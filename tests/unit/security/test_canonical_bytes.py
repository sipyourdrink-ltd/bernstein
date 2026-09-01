"""The one canonical-JSON byte rule and the verifier-side version selection (#5094)."""

from __future__ import annotations

import math

import pytest

from bernstein.core.security.canonical import (
    CANONICALIZATION_VERSION,
    UnsupportedCanonicalization,
    bytes_for_verification,
    canonical_bytes,
    legacy_ascii_bytes,
    legacy_pretty_bytes,
)

_PAYLOAD = {"b": [1, {"z": None, "a": "é"}], "a": "ü"}


def test_canonical_bytes_sorts_keys_at_every_depth_and_keeps_non_ascii() -> None:
    assert canonical_bytes(_PAYLOAD) == '{"a":"ü","b":[1,{"a":"é","z":null}]}'.encode()


def test_legacy_ascii_rule_differs_only_for_non_ascii_payloads() -> None:
    assert legacy_ascii_bytes({"k": "v"}) == canonical_bytes({"k": "v"})
    assert legacy_ascii_bytes(_PAYLOAD) == b'{"a":"\\u00fc","b":[1,{"a":"\\u00e9","z":null}]}'
    assert legacy_ascii_bytes(_PAYLOAD) != canonical_bytes(_PAYLOAD)


def test_legacy_pretty_rule_is_indented_ascii_with_trailing_newline() -> None:
    assert legacy_pretty_bytes({"k": "é"}) == b'{\n  "k": "\\u00e9"\n}\n'


def test_canonical_bytes_refuses_values_that_are_not_json() -> None:
    with pytest.raises(ValueError):
        canonical_bytes({"x": math.nan})
    with pytest.raises(ValueError):
        canonical_bytes({"x": math.inf})


def test_verifier_recomputes_under_the_rule_the_artefact_names() -> None:
    assert bytes_for_verification(_PAYLOAD, CANONICALIZATION_VERSION, legacy=legacy_ascii_bytes) == canonical_bytes(
        _PAYLOAD
    )
    assert bytes_for_verification(_PAYLOAD, None, legacy=legacy_ascii_bytes) == legacy_ascii_bytes(_PAYLOAD)
    assert bytes_for_verification(_PAYLOAD, None, legacy=legacy_pretty_bytes) == legacy_pretty_bytes(_PAYLOAD)


def test_verifier_refuses_a_version_this_build_cannot_reproduce() -> None:
    with pytest.raises(UnsupportedCanonicalization):
        bytes_for_verification(_PAYLOAD, CANONICALIZATION_VERSION + 1, legacy=legacy_ascii_bytes)
