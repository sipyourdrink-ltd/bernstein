"""Tests for ``govern discover --assist`` findings document (issue #5020)."""

from __future__ import annotations

from datetime import datetime

from bernstein.core.govern import Finding, FindingsDocument


def test_finding_round_trip() -> None:
    """Finding to_dict / from_dict should preserve all fields."""
    f = Finding(
        surface="arn:aws:s3:::my-bucket",
        observed_value='{"Versioning": {"Status": "Enabled"}}',
        evidence_ref="aws://s3/describe-buckets#my-bucket",
        readable=True,
    )
    raw = f.to_dict()
    f2 = Finding.from_dict(raw)
    assert f2.surface == f.surface
    assert f2.observed_value == f.observed_value
    assert f2.evidence_ref == f.evidence_ref
    assert f2.readable == f.readable


def test_finding_content_hash_deterministic() -> None:
    """Two identical findings must produce byte-identical canonical bytes."""
    f1 = Finding(
        surface="arn:aws:s3:::my-bucket",
        observed_value='{"Versioning": {"Status": "Enabled"}}',
        evidence_ref="aws://s3/describe-buckets#my-bucket",
        readable=True,
    )
    f2 = Finding(
        surface="arn:aws:s3:::my-bucket",
        observed_value='{"Versioning": {"Status": "Enabled"}}',
        evidence_ref="aws://s3/describe-buckets#my-bucket",
        readable=True,
    )
    assert f1.content_hash() == f2.content_hash()


def test_findings_document_round_trip() -> None:
    """FindingsDocument to_dict / from_dict should preserve all fields."""
    f1 = Finding(
        surface="arn:aws:s3:::my-bucket",
        observed_value='{"Versioning": {"Status": "Enabled"}}',
        evidence_ref="aws://s3/describe-buckets#my-bucket",
        readable=True,
    )
    f2 = Finding(
        surface="arn:aws:s3:::another-bucket",
        observed_value="{}",
        evidence_ref="aws://s3/list-buckets#another-bucket",
        readable=False,  # unreadable surface
    )
    fd = FindingsDocument(
        findings=(f1, f2),
        inventory_hash="sha256:abc123def456",
        timestamp=1234567890,
    )
    raw = fd.to_dict()
    fd2 = FindingsDocument.from_dict(raw)
    assert fd2.findings == fd.findings
    assert fd2.inventory_hash == fd.inventory_hash
    assert fd2.timestamp == fd.timestamp


def test_findings_document_content_addressed() -> None:
    """FindingsDocument content_hash must be content-addressed and deterministic."""
    f1 = Finding(
        surface="arn:aws:s3:::my-bucket",
        observed_value='{"Versioning": {"Status": "Enabled"}}',
        evidence_ref="aws://s3/describe-buckets#my-bucket",
        readable=True,
    )
    f2 = Finding(
        surface="arn:aws:s3:::another-bucket",
        observed_value="{}",
        evidence_ref="aws://s3/list-buckets#another-bucket",
        readable=False,
    )
    fd1 = FindingsDocument(
        findings=(f1, f2),
        inventory_hash="sha256:abc123def456",
        timestamp=1234567890,
    )
    fd2 = FindingsDocument(
        findings=(f1, f2),
        inventory_hash="sha256:abc123def456",
        timestamp=1234567890,
    )
    assert fd1.content_hash() == fd2.content_hash()
    assert fd1.content_hash().startswith("sha256:")


def test_findings_document_to_canonical_bytes_deterministic() -> None:
    """Two identical FindingsDocument must produce byte-identical canonical bytes."""
    f1 = Finding(
        surface="arn:aws:s3:::my-bucket",
        observed_value='{"Versioning": {"Status": "Enabled"}}',
        evidence_ref="aws://s3/describe-buckets#my-bucket",
        readable=True,
    )
    f2 = Finding(
        surface="arn:aws:s3:::another-bucket",
        observed_value="{}",
        evidence_ref="aws://s3/list-buckets#another-bucket",
        readable=False,
    )
    fd1 = FindingsDocument(
        findings=(f1, f2),
        inventory_hash="sha256:abc123def456",
        timestamp=1234567890,
    )
    fd2 = FindingsDocument(
        findings=(f1, f2),
        inventory_hash="sha256:abc123def456",
        timestamp=1234567890,
    )
    assert fd1.to_canonical_bytes() == fd2.to_canonical_bytes()


def test_findings_document_readable_unreadable_surfaces() -> None:
    """readable_surfaces() and unreadable_surfaces() must work correctly."""
    f1 = Finding(
        surface="arn:aws:s3:::my-bucket",
        observed_value='{"Versioning": {"Status": "Enabled"}}',
        evidence_ref="aws://s3/describe-buckets#my-bucket",
        readable=True,
    )
    f2 = Finding(
        surface="arn:aws:s3:::another-bucket",
        observed_value="",
        evidence_ref="aws://s3/list-buckets#another-bucket",
        readable=False,  # unreadable surface
    )
    f3 = Finding(
        surface="arn:aws:s3:::third-bucket",
        observed_value="{}",
        evidence_ref="aws://s3/list-buckets#third-bucket",
        readable=True,
    )
    fd = FindingsDocument(
        findings=(f1, f2, f3),
        inventory_hash="sha256:abc123def456",
        timestamp=1234567890,
    )

    readable = fd.readable_surfaces()
    assert len(readable) == 2
    assert readable[0].surface == "arn:aws:s3:::my-bucket"
    assert readable[1].surface == "arn:aws:s3:::third-bucket"
    assert all(f.readable for f in readable)

    unreadable = fd.unreadable_surfaces()
    assert len(unreadable) == 1
    assert unreadable[0] == "arn:aws:s3:::another-bucket"


def test_findings_document_timestamp_conversion() -> None:
    """timestamp_from_utc() must produce valid ISO 8601 string."""
    fd = FindingsDocument(
        findings=(),
        inventory_hash="sha256:abc123def456",
        timestamp=1234567890,
    )
    iso_str = fd.timestamp_from_utc()
    # Parse it back to verify it's valid ISO 8601
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    assert dt.timestamp() == 1234567890
    assert dt.tzinfo is not None


def test_findings_document_empty() -> None:
    """Empty findings document should work."""
    fd = FindingsDocument(
        findings=(),
        inventory_hash="sha256:abc123def456",
        timestamp=1234567890,
    )
    assert len(fd.findings) == 0
    assert fd.inventory_hash == "sha256:abc123def456"
    assert fd.timestamp == 1234567890
    assert fd.readable_surfaces() == ()
    assert fd.unreadable_surfaces() == ()
