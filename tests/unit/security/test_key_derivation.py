"""Tests for :mod:`bernstein.core.security.key_derivation`.

These pin the two properties that make per-store key derivation safe to rely
on: different domains must produce different keys (domain separation), and the
same inputs must always produce the same key (determinism). They also pin the
scheme-versioned domain tag strings that downstream spine/audit wiring depends
on.
"""

from __future__ import annotations

import pytest

from bernstein.core.security.key_derivation import (
    DOMAIN_AUDIT,
    DOMAIN_LINEAGE,
    SCHEME_V1,
    SCHEME_V2,
    derive_store_key,
    domain_tag,
)

_MASTER_KEY = b"test-key"


def test_different_domains_produce_different_keys() -> None:
    lineage_key = derive_store_key(_MASTER_KEY, DOMAIN_LINEAGE)
    audit_key = derive_store_key(_MASTER_KEY, DOMAIN_AUDIT)
    assert lineage_key != audit_key


def test_derivation_is_deterministic() -> None:
    first = derive_store_key(_MASTER_KEY, DOMAIN_LINEAGE)
    second = derive_store_key(_MASTER_KEY, DOMAIN_LINEAGE)
    assert first == second


def test_derived_key_is_32_bytes() -> None:
    key = derive_store_key(_MASTER_KEY, DOMAIN_LINEAGE)
    assert len(key) == 32


def test_different_master_keys_produce_different_keys() -> None:
    assert derive_store_key(b"key-a", DOMAIN_LINEAGE) != derive_store_key(b"key-b", DOMAIN_LINEAGE)


def test_domain_tag_v2_is_non_empty_and_versioned() -> None:
    tag = domain_tag(DOMAIN_LINEAGE)
    assert tag
    assert tag == "bernstein:lineage:v2"


def test_domain_tag_v1_is_empty() -> None:
    assert domain_tag(DOMAIN_LINEAGE, SCHEME_V1) == ""


def test_domain_tag_audit_v2() -> None:
    assert domain_tag(DOMAIN_AUDIT) == "bernstein:audit:v2"


def test_domain_tag_uses_supplied_version() -> None:
    assert domain_tag(DOMAIN_LINEAGE, SCHEME_V2) == "bernstein:lineage:v2"


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        (DOMAIN_LINEAGE, "bernstein:lineage:v2"),
        (DOMAIN_AUDIT, "bernstein:audit:v2"),
    ],
)
def test_domain_tag_round_trip(domain: str, expected: str) -> None:
    assert domain_tag(domain) == expected
