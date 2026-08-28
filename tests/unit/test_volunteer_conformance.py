"""Tests for the volunteer protocol conformance harness.

Coverage:

* to_hub_projection / from_hub_projection round-trips produce identical
  canonical_hash.
* to_github_projection / from_github_projection round-trips produce identical
  canonical_hash.
* Both projections recover the same hash as the original dict.
* Base64 fallback used when JSON contains ```.
* Missing marker raises ValueError.
* Malformed JSON raises ValueError.
* ConformanceHarness generic: registering a second doc type works with only
  to_dict/from_dict.
* assert_conformance raises AssertionError on failure.
"""

from __future__ import annotations

import pytest

from bernstein.core.protocols.volunteer.conformance import (
    GITHUB_BASE64_PREFIX,
    GITHUB_MARKER,
    ConformanceHarness,
    assert_conformance,
    from_github_projection,
    from_hub_projection,
    to_github_projection,
    to_hub_projection,
)
from bernstein.core.protocols.volunteer.documents import canonical_hash

# ---------------------------------------------------------------------------
# Hub projection
# ---------------------------------------------------------------------------


class TestHubProjection:
    """to_hub_projection / from_hub_projection round-trip."""

    def test_round_trip_preserves_hash(self) -> None:
        doc = {"worker_id": "w1", "task_id": "t1", "claimed_at": "2024-01-01T00:00:00Z"}
        projected = to_hub_projection(doc)
        recovered = from_hub_projection(projected)
        assert canonical_hash(recovered) == canonical_hash(doc)

    def test_round_trip_preserves_dict(self) -> None:
        doc = {"a": 1, "b": 2}
        recovered = from_hub_projection(to_hub_projection(doc))
        assert recovered == doc

    def test_round_trip_bytes_input(self) -> None:
        """from_hub_projection accepts bytes as input."""
        doc = {"key": "value"}
        projected = to_hub_projection(doc)
        recovered = from_hub_projection(projected)
        assert recovered == doc

    def test_round_trip_dict_input(self) -> None:
        """from_hub_projection accepts dict as input (passthrough)."""
        doc = {"key": "value"}
        recovered = from_hub_projection(doc)
        assert recovered == doc

    def test_invalid_bytes_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid UTF-8 JSON"):
            from_hub_projection(b"\xff\xfe invalid")

    def test_non_object_raises(self) -> None:
        with pytest.raises(ValueError, match="expected object"):
            from_hub_projection(b"42")

    def test_non_dict_non_bytes_raises(self) -> None:
        with pytest.raises(ValueError, match="must be bytes or dict"):
            from_hub_projection(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# GitHub projection
# ---------------------------------------------------------------------------


class TestGitHubProjection:
    """to_github_projection / from_github_projection round-trip."""

    def test_round_trip_preserves_hash(self) -> None:
        doc = {"worker_id": "w1", "task_id": "t1", "claimed_at": "2024-01-01T00:00:00Z"}
        projected = to_github_projection(doc)
        recovered = from_github_projection(projected)
        assert canonical_hash(recovered) == canonical_hash(doc)

    def test_round_trip_preserves_dict(self) -> None:
        doc = {"a": 1, "b": 2}
        recovered = from_github_projection(to_github_projection(doc))
        assert recovered == doc

    def test_contains_marker(self) -> None:
        doc = {"k": "v"}
        projected = to_github_projection(doc)
        assert GITHUB_MARKER in projected
        assert "```json" in projected

    def test_github_round_trip_same_as_hub_round_trip(self) -> None:
        """Both projections produce the same canonical hash."""
        doc = {"task_id": "T-1", "worker_id": "alice", "claimed_at": "2024-06-01T12:00:00+00:00"}
        hub_proj = to_hub_projection(doc)
        github_proj = to_github_projection(doc)
        hub_hash = canonical_hash(from_hub_projection(hub_proj))
        github_hash = canonical_hash(from_github_projection(github_proj))
        assert hub_hash == github_hash == canonical_hash(doc)

    def test_base64_fallback_when_fence_in_json(self) -> None:
        """JSON containing ``` is base64-encoded."""
        doc = {"text": "Here is ``` in the string"}
        projected = to_github_projection(doc)
        assert GITHUB_BASE64_PREFIX in projected
        recovered = from_github_projection(projected)
        assert recovered == doc

    def test_no_base64_for_normal_json(self) -> None:
        """Normal JSON without ``` does not use base64."""
        doc = {"key": "normal value"}
        projected = to_github_projection(doc)
        assert GITHUB_BASE64_PREFIX not in projected

    def test_missing_marker_raises(self) -> None:
        with pytest.raises(ValueError, match="does not contain marker"):
            from_github_projection("no marker here")

    def test_missing_fence_raises(self) -> None:
        with pytest.raises(ValueError, match="missing"):
            from_github_projection(f"{GITHUB_MARKER}\njust text")

    def test_invalid_base64_raises(self) -> None:
        bad = f"""{GITHUB_MARKER}
```json
{GITHUB_BASE64_PREFIX}not-valid-base64!!!
```
"""
        with pytest.raises(ValueError, match="base64 decode failed"):
            from_github_projection(bad)

    def test_invalid_json_after_decode_raises(self) -> None:
        """Valid base64 that decodes to non-JSON raises."""
        import base64

        bad_json = b"not json at all"
        bad_proj = f"""{GITHUB_MARKER}
```json
{GITHUB_BASE64_PREFIX}{base64.b64encode(bad_json).decode()}
```
"""
        with pytest.raises(ValueError, match="invalid JSON"):
            from_github_projection(bad_proj)

    def test_dict_input_passthrough(self) -> None:
        """from_github_projection accepts a dict directly."""
        doc = {"key": "value"}
        result = from_github_projection(doc)
        assert result == doc

    def test_non_string_raises(self) -> None:
        with pytest.raises(ValueError, match="must be str or dict"):
            from_github_projection(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ConformanceHarness
# ---------------------------------------------------------------------------


class TestConformanceHarness:
    """Generic harness registers and checks multiple doc types."""

    def test_single_doc_type_succeeds(self) -> None:
        """A single registered doc type passes conformance."""
        harness = ConformanceHarness()
        harness.register(
            name="Claim",
            to_canonical_dict=lambda c: c,  # identity for dict input
            from_canonical_dict=lambda d: d,
        )
        result = harness.check("Claim", {"worker_id": "w", "task_id": "t", "claimed_at": "2024-01-01T00:00:00Z"})
        assert result.ok is True
        assert result.original_hash == result.github_hash == result.hub_hash

    def test_unknown_doc_type_fails(self) -> None:
        harness = ConformanceHarness()
        harness.register(
            name="Claim",
            to_canonical_dict=lambda c: c,
            from_canonical_dict=lambda d: d,
        )
        result = harness.check("UnknownType", {"key": "value"})
        assert result.ok is False
        assert "unknown document type" in result.error

    def test_to_canonical_dict_raises_propagates(self) -> None:
        harness = ConformanceHarness()
        harness.register(
            name="Bad",
            to_canonical_dict=lambda x: (_ for _ in ()).throw(RuntimeError("boom")),
            from_canonical_dict=lambda d: d,
        )
        result = harness.check("Bad", {"key": "value"})
        assert result.ok is False
        assert "RuntimeError" in result.error

    def test_harness_reusable_second_doc_type(self) -> None:
        """A second doc type registers and checks independently."""

        class DummyDoc:
            def __init__(self, data: dict) -> None:
                self.data = data

            def to_dict(self) -> dict:
                return {"kind": "dummy", **self.data}

        harness = ConformanceHarness()
        harness.register(
            name="Claim",
            to_canonical_dict=lambda c: c,
            from_canonical_dict=lambda d: d,
        )
        harness.register(
            name="Dummy",
            to_canonical_dict=lambda d: d.to_dict(),
            from_canonical_dict=lambda d: DummyDoc(d),
        )

        claim_doc = {"worker_id": "w", "task_id": "t", "claimed_at": "2024-01-01T00:00:00Z"}
        dummy_doc = DummyDoc({"id": "d1", "value": 42})

        r1 = harness.check("Claim", claim_doc)
        r2 = harness.check("Dummy", dummy_doc)

        assert r1.ok is True
        assert r2.ok is True
        assert r1.original_hash == r1.github_hash == r1.hub_hash
        assert r2.original_hash == r2.github_hash == r2.hub_hash
        # Different doc types have different hashes.
        assert r1.original_hash != r2.original_hash


# ---------------------------------------------------------------------------
# assert_conformance
# ---------------------------------------------------------------------------


class TestAssertConformance:
    """assert_conformance raises AssertionError on failure."""

    def test_success_no_exception(self) -> None:
        result = assert_conformance(
            {"key": "value"},
            name="Test",
            to_canonical_dict=lambda x: x,
            from_canonical_dict=lambda d: d,
        )
        assert result.ok is True

    def test_unknown_type_raises(self) -> None:
        harness = ConformanceHarness()
        harness.register(
            name="Claim",
            to_canonical_dict=lambda c: c,
            from_canonical_dict=lambda d: d,
        )
        with pytest.raises(AssertionError, match="unknown document type"):
            assert_conformance(
                {"key": "value"},
                harness=harness,
                name="Unregistered",
            )
