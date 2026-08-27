"""Registry-shape tests for ``bernstein.adapters.digest``.

Locks the public narrative: digesters are registered, rulesets are versioned,
and trace records carry all metadata for replay verification.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from bernstein.adapters.digest.digesters import (
    default_digester,
    get_digester,
    list_families,
    pytest_digester,
)
from bernstein.adapters.digest.models import TraceRecord
from bernstein.adapters.digest.rulesets import (
    GIT_RULESET_V1,
    PYTEST_RULESET_V1,
    Ruleset,
    get_ruleset,
    list_rulesets,
)


def test_digesters_registry_has_pytest_and_git() -> None:
    """pytest and git digesters must be registered."""
    families = list_families()
    assert "pytest" in families
    assert "git" in families


def test_get_digester_returns_function() -> None:
    """``get_digester`` must return a callable that produces expected tuple."""
    digester = get_digester("pytest")
    assert callable(digester)
    # Verify it returns the expected tuple type
    result = digester(b"test")
    assert isinstance(result, tuple)
    assert len(result) == 2
    digest, counts = result
    assert isinstance(digest, bytes)
    assert isinstance(counts, dict)


def test_get_digester_raises_on_unknown_family() -> None:
    """``get_digester`` must raise ValueError for unknown families."""
    with pytest.raises(ValueError, match="No digester registered"):
        get_digester("unknown-family")


def test_pytest_digester_is_deterministic() -> None:
    """Same raw bytes must produce byte-identical digest."""
    raw = b"test output"
    digest1, counts1 = pytest_digester(raw)
    digest2, counts2 = pytest_digester(raw)

    assert digest1 == digest2
    assert counts1 == counts2
    assert counts1["raw"] == len(raw)
    assert counts1["digest"] == len(digest1)


def test_git_digester_is_deterministic() -> None:
    """Same raw bytes must produce byte-identical digest."""
    raw = b"git output"
    digest1, counts1 = pytest_digester(raw)
    digest2, counts2 = pytest_digester(raw)

    assert digest1 == digest2
    assert counts1 == counts2


def test_default_digester_produces_sha256() -> None:
    """Default digester must produce SHA-256 hash."""
    raw = b"some data"
    digest, counts = default_digester(raw)

    expected = hashlib.sha256(raw).digest()
    assert digest == expected
    assert counts["raw"] == len(raw)
    assert counts["digest"] == 32  # SHA-256 is 256 bits = 32 bytes


def test_digesters_are_pure_functions() -> None:
    """Digesters must not mutate their input."""
    raw = b"test data"
    original = raw[:]

    pytest_digester(raw)
    assert raw == original

    default_digester(raw)
    assert raw == original


# ---------------------------------------------------------------------------
# Ruleset tests
# ---------------------------------------------------------------------------


def test_ruleset_has_fully_qualified_id() -> None:
    """Ruleset.id must produce a fully-qualified identifier."""
    assert PYTEST_RULESET_V1.ruleset_id == "pytest-1"
    assert GIT_RULESET_V1.ruleset_id == "git-1"


def test_ruleset_has_description() -> None:
    """Ruleset must have a human-readable description."""
    assert PYTEST_RULESET_V1.description
    assert GIT_RULESET_V1.description


def test_get_ruleset_returns_ruleset() -> None:
    """``get_ruleset`` must return a Ruleset object."""
    ruleset = get_ruleset("pytest-1")
    assert isinstance(ruleset, Ruleset)
    assert ruleset.id == "pytest"
    assert ruleset.version == "1"


def test_get_ruleset_raises_on_unknown_id() -> None:
    """``get_ruleset`` must raise ValueError for unknown ruleset IDs."""
    with pytest.raises(ValueError, match="Unknown ruleset"):
        get_ruleset("unknown-999")


def test_list_rulesets_returns_all() -> None:
    """``list_rulesets`` must return all registered rulesets."""
    rulesets = list_rulesets()
    assert len(rulesets) >= 2
    rule_ids = {r.ruleset_id for r in rulesets}
    assert "pytest-1" in rule_ids
    assert "git-1" in rule_ids


# ---------------------------------------------------------------------------
# TraceRecord tests
# ---------------------------------------------------------------------------


def test_trace_record_is_frozen() -> None:
    """TraceRecord must be immutable (frozen dataclass)."""
    record = TraceRecord(
        ruleset_id="pytest",
        ruleset_version="1",
        raw_sha256="a" * 64,
        digest_sha256="b" * 64,
        raw_bytes=100,
        digest_bytes=32,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        record.ruleset_id = "changed"  # type: ignore[misc]


def test_trace_record_to_dict_round_trip() -> None:
    """TraceRecord must round-trip through dict."""
    original = TraceRecord(
        ruleset_id="pytest",
        ruleset_version="1",
        raw_sha256="a" * 64,
        digest_sha256="b" * 64,
        raw_bytes=100,
        digest_bytes=32,
    )

    data = original.to_dict()
    restored = TraceRecord.from_dict(data)

    assert restored.ruleset_id == original.ruleset_id
    assert restored.ruleset_version == original.ruleset_version
    assert restored.raw_sha256 == original.raw_sha256
    assert restored.digest_sha256 == original.digest_sha256
    assert restored.raw_bytes == original.raw_bytes
    assert restored.digest_bytes == original.digest_bytes


def test_trace_record_byte_counts() -> None:
    """TraceRecord.byte_counts must return a dict with expected keys."""
    record = TraceRecord(
        ruleset_id="pytest",
        ruleset_version="1",
        raw_sha256="a" * 64,
        digest_sha256="b" * 64,
        raw_bytes=100,
        digest_bytes=32,
    )

    counts = record.byte_counts
    assert counts["raw"] == 100
    assert counts["digest"] == 32
    # ByteCounts is a TypedDict, so we check structure instead of isinstance
    assert isinstance(counts, dict)
    assert "raw" in counts
    assert "digest" in counts


def test_trace_record_from_dict_strict_types() -> None:
    """``from_dict`` must convert values to correct types."""
    data = {
        "ruleset_id": 123,  # will be converted to str
        "ruleset_version": 456,  # will be converted to str
        "raw_sha256": 789,  # will be converted to str
        "digest_sha256": 101112,  # will be converted to str
        "raw_bytes": "100",  # will be converted to int
        "digest_bytes": "32",  # will be converted to int
    }

    record = TraceRecord.from_dict(data)

    assert isinstance(record.ruleset_id, str)
    assert isinstance(record.ruleset_version, str)
    assert isinstance(record.raw_sha256, str)
    assert isinstance(record.digest_sha256, str)
    assert isinstance(record.raw_bytes, int)
    assert isinstance(record.digest_bytes, int)
