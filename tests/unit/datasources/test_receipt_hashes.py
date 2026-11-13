"""Pin the canonical encoding used for offline query receipt hashes."""

import hashlib

from bernstein.core.datasources.receipt import compute_binding, hash_params, hash_query_text


def test_query_hash_normalizes_unicode_without_stripping_query_text() -> None:
    composed = "SELECT 'café'"
    decomposed = "SELECT 'cafe\u0301'"
    expected = hashlib.sha256(composed.encode()).hexdigest()
    assert hash_query_text(composed) == hash_query_text(decomposed) == f"sha256:{expected}"
    assert hash_query_text(composed + " ") != f"sha256:{expected}"


def test_parameter_hash_is_stable_under_mapping_order() -> None:
    first = {"region": "eu", "limit": 2}
    second = {"limit": 2, "region": "eu"}
    expected = hashlib.sha256(b'{"limit":2,"region":"eu"}').hexdigest()
    assert hash_params(first) == hash_params(second) == f"sha256:{expected}"
    assert hash_params([1, 2]) != hash_params([2, 1])


def test_receipt_binding_uses_an_unprefixed_digest_and_binds_content() -> None:
    expected = hashlib.sha256(b'{"driver":"test","rows":2}').hexdigest()
    assert compute_binding({"rows": 2, "driver": "test"}) == expected
    assert compute_binding({"driver": "test", "rows": 2}) == expected
    assert compute_binding({"driver": "test", "rows": 3}) != expected
