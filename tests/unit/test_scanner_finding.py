"""Tests for scanner_finding.py."""

from __future__ import annotations

import json

from bernstein.adapters.scanner_finding import Finding, _canonical_json_bytes, findings_hash


def test_finding_init_defaults() -> None:
    """Finding should initialize with correct defaults."""
    f = Finding(rule="test-rule", path="/some/path")
    assert f.rule == "test-rule"
    assert f.path == "/some/path"
    assert f.severity == "informational"
    assert f.summary == ""
    assert f.extra == {}


def test_finding_init_all_fields() -> None:
    """Finding should initialize with all fields provided."""
    f = Finding(
        rule="test-rule",
        path="/some/path",
        severity="high",
        summary="Test summary",
        extra={"key": "value", "number": 42},
    )
    assert f.rule == "test-rule"
    assert f.path == "/some/path"
    assert f.severity == "high"
    assert f.summary == "Test summary"
    assert f.extra == {"key": "value", "number": 42}


def test_finding_to_dict() -> None:
    """Finding.to_dict should return correct dict representation."""
    f = Finding(
        rule="test-rule",
        path="/some/path",
        severity="high",
        summary="Test summary",
        extra={"key": "value"},
    )
    expected = {
        "rule": "test-rule",
        "path": "/some/path",
        "severity": "high",
        "summary": "Test summary",
        "extra": {"key": "value"},
    }
    assert f.to_dict() == expected


def test_finding_to_dict_no_extra() -> None:
    """Finding.to_dict should omit extra when empty."""
    f = Finding(rule="test-rule", path="/some/path")
    expected = {
        "rule": "test-rule",
        "path": "/some/path",
        "severity": "informational",
        "summary": "",
    }
    assert f.to_dict() == expected


def test_finding_from_dict() -> None:
    """Finding.from_dict should reconstruct correctly."""
    data = {
        "rule": "test-rule",
        "path": "/some/path",
        "severity": "high",
        "summary": "Test summary",
        "extra": {"key": "value"},
    }
    f = Finding.from_dict(data)
    assert f.rule == "test-rule"
    assert f.path == "/some/path"
    assert f.severity == "high"
    assert f.summary == "Test summary"
    assert f.extra == {"key": "value"}


def test_finding_from_dict_minimal() -> None:
    """Finding.from_dict should work with minimal data."""
    data = {
        "rule": "test-rule",
        "path": "/some/path",
    }
    f = Finding.from_dict(data)
    assert f.rule == "test-rule"
    assert f.path == "/some/path"
    assert f.severity == "informational"  # default
    assert f.summary == ""  # default
    assert f.extra == {}  # default


def test_finding_hash() -> None:
    """Finding.finding_hash should return SHA-256 of canonical JSON."""
    f = Finding(rule="test-rule", path="/some/path", severity="high", summary="test")
    # Canonical JSON: {"rule":"test-rule","path":"/some/path","severity":"high","summary":"test"}
    # Let's compute it manually to verify
    canonical = json.dumps(
        {
            "rule": "test-rule",
            "path": "/some/path",
            "severity": "high",
            "summary": "test",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")

    # Actually compute the hash
    import hashlib

    expected = hashlib.sha256(canonical).hexdigest()
    assert f.finding_hash() == expected


def test_finding_equality() -> None:
    """Finding should be equal when all fields match."""
    f1 = Finding(rule="r", path="p", severity="s", summary="sum", extra={"a": 1})
    f2 = Finding(rule="r", path="p", severity="s", summary="sum", extra={"a": 1})
    assert f1 == f2
    assert hash(f1) == hash(f2)  # Should be hashable since frozen


def test_finding_inequality() -> None:
    """Finding should be unequal when fields differ (extra is excluded from comparison)."""
    # extra is excluded from comparison (compare=False)
    f1 = Finding(rule="r", path="p", severity="s", summary="sum", extra={"a": 1})
    f2 = Finding(rule="r", path="p", severity="s", summary="sum", extra={"a": 2})
    assert f1 == f2  # extra doesn't affect equality

    f3 = Finding(rule="r", path="p", severity="s", summary="different", extra={"a": 1})
    assert f1 != f3  # summary differs

    f4 = Finding(rule="r", path="p", severity="different", summary="sum", extra={"a": 1})
    assert f1 != f4  # severity differs


def test_finding_frozen() -> None:
    """Finding should be frozen (immutable)."""
    f = Finding(rule="r", path="p")
    try:
        f.rule = "changed"  # type: ignore[misc]
    except AttributeError:
        pass  # Expected
    else:
        raise AssertionError("Should not be able to modify frozen field")


def test_canonical_json_bytes() -> None:
    """_canonical_json_bytes should produce consistent ordering."""
    obj1 = {"b": 2, "a": 1}
    obj2 = {"a": 1, "b": 2}
    assert _canonical_json_bytes(obj1) == _canonical_json_bytes(obj2)

    # Should be compact JSON with sorted keys
    expected = b'{"a":1,"b":2}'
    assert _canonical_json_bytes(obj1) == expected
    assert _canonical_json_bytes(obj2) == expected


def test_findings_hash() -> None:
    """findings_hash should return SHA-256 of sorted finding hashes."""
    f1 = Finding(rule="a", path="p1")
    f2 = Finding(rule="b", path="p2")
    f3 = Finding(rule="c", path="p3")

    # Order shouldn't matter - should sort by hash
    result1 = findings_hash([f1, f2, f3])
    result2 = findings_hash([f3, f1, f2])
    result3 = findings_hash([f2, f3, f1])

    assert result1 == result2 == result3

    # Should be hash of concatenated individual hashes
    individual_hashes = sorted([f.finding_hash() for f in [f1, f2, f3]])
    concatenated = "\n".join(individual_hashes).encode("utf-8")
    import hashlib

    expected = hashlib.sha256(concatenated).hexdigest()
    assert result1 == expected


def test_findings_hash_empty() -> None:
    """findings_hash with empty list should work."""
    assert (
        findings_hash([]) == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )  # SHA-256 of empty string
