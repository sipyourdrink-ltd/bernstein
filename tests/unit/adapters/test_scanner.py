"""Tests for scanner.py - ScannerAdapter contract and related types."""

from __future__ import annotations

import tempfile
from pathlib import Path

from bernstein.adapters.scanner import (
    DeterminismTier,
    OutputFormat,
    ScannerAdapter,
    ScannerCategory,
    ScanResult,
    ScanScope,
)
from bernstein.adapters.scanner_finding import Finding


def test_output_format_enum() -> None:
    """OutputFormat enum should have correct values."""
    assert OutputFormat.SARIF == "sarif"
    assert OutputFormat.JSON == "json"
    assert OutputFormat.XML == "xml"
    assert isinstance(OutputFormat.SARIF, str)


def test_determinism_tier_enum() -> None:
    """DeterminismTier enum should have correct values."""
    assert DeterminismTier.DETERMINISTIC == "deterministic"
    assert DeterminismTier.FEED_PINNED == "feed_pinned"
    assert DeterminismTier.TRANSCRIPT_ANCHORED == "transcript_anchored"
    assert isinstance(DeterminismTier.DETERMINISTIC, str)


def test_scanner_category_enum() -> None:
    """ScannerCategory enum should have correct values."""
    assert ScannerCategory.SAST == "sast"
    assert ScannerCategory.SCA == "sca"
    assert ScannerCategory.SECRET == "secret"
    assert ScannerCategory.IAC == "iac"
    assert ScannerCategory.RECON == "recon"
    assert ScannerCategory.DAST == "dast"
    assert isinstance(ScannerCategory.SAST, str)


def test_scan_scope_init_defaults() -> None:
    """ScanScope should initialize with correct defaults."""
    scope = ScanScope()
    assert scope.roots == ()
    assert scope.include == ()
    assert scope.exclude == ()
    assert scope.max_depth is None
    assert scope.config == {}


def test_scan_scope_init_all_fields() -> None:
    """ScanScope should initialize with all fields."""
    roots = (Path("/tmp/a"), Path("/tmp/b"))
    scope = ScanScope(
        roots=roots,
        include=("*.py", "*.js"),
        exclude=("*.tmp",),
        max_depth=10,
        config={"severity": "high"},
    )
    assert scope.roots == roots
    assert scope.include == ("*.py", "*.js")
    assert scope.exclude == ("*.tmp",)
    assert scope.max_depth == 10
    assert scope.config == {"severity": "high"}


def test_scan_scope_to_dict() -> None:
    """ScanScope.to_dict should return correct dict."""
    roots = (Path("/tmp/a"), Path("/tmp/b"))
    scope = ScanScope(
        roots=roots,
        include=("*.py",),
        exclude=("*.tmp",),
        max_depth=5,
        config={"key": "value"},
    )
    d = scope.to_dict()
    expected = {
        "roots": ["/tmp/a", "/tmp/b"],
        "include": ["*.py"],
        "exclude": ["*.tmp"],
        "max_depth": 5,
        "config": {"key": "value"},
    }
    assert d == expected


def test_scan_result_init_defaults() -> None:
    """ScanResult should initialize with correct defaults."""
    result = ScanResult()
    assert result.findings == []
    assert result.transcript == ""
    assert result.feed_digest == ""


def test_scan_result_init_all_fields() -> None:
    """ScanResult should initialize with all fields."""
    findings = [Finding(rule="r1", path="p1"), Finding(rule="r2", path="p2")]
    result = ScanResult(
        findings=findings,
        transcript="some transcript text",
        feed_digest="abc123",
    )
    assert result.findings == findings
    assert result.transcript == "some transcript text"
    assert result.feed_digest == "abc123"


def test_scan_result_finding_hashes() -> None:
    """ScanResult.finding_hashes should return sorted hashes."""
    findings = [
        Finding(rule="c", path="p3"),
        Finding(rule="a", path="p1"),
        Finding(rule="b", path="p2"),
    ]
    result = ScanResult(findings=findings)
    hashes = result.finding_hashes()
    assert hashes == sorted(hashes)  # Should be sorted


def test_scanner_adapter_cannot_instantiate_directly() -> None:
    """ScannerAdapter should not be instantiable directly (abstract)."""
    try:
        ScannerAdapter()
    except TypeError:
        pass  # Expected - abstract class
    else:
        raise AssertionError("Should not be instantiable")


class MockScannerAdapter(ScannerAdapter):
    """Mock scanner adapter for testing."""

    registry_name = "mock-scanner"
    output_format = OutputFormat.JSON
    determinism = DeterminismTier.DETERMINISTIC
    pinned_inputs = ("source_files",)
    category = ScannerCategory.SAST

    def name(self) -> str:
        return self.registry_name

    def scan(self, target: Path, scope: ScanScope, workdir: Path) -> ScanResult:
        return ScanResult(
            findings=[Finding(rule="mock-rule", path=str(target))],
            transcript="mock transcript",
            feed_digest="mock-digest",
        )


def test_mock_scanner_adapter() -> None:
    """MockScannerAdapter should work correctly."""
    adapter = MockScannerAdapter()
    assert adapter.name() == "mock-scanner"
    assert adapter.output_format == OutputFormat.JSON
    assert adapter.determinism == DeterminismTier.DETERMINISTIC
    assert adapter.pinned_inputs == ("source_files",)
    assert adapter.category == ScannerCategory.SAST
    assert adapter.external_endpoints == ()

    # Test scan
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        target = Path("/tmp/test")
        scope = ScanScope(roots=(target,))
        result = adapter.scan(target, scope, workdir)

        assert isinstance(result, ScanResult)
        assert len(result.findings) == 1
        assert result.findings[0].rule == "mock-rule"
        assert result.transcript == "mock transcript"
        assert result.feed_digest == "mock-digest"
        assert adapter.enforce_network_policy() is None  # No endpoints


def test_scanner_adapter_rate_limit_meter() -> None:
    """ScannerAdapter should have rate_limit_meter property."""
    adapter = MockScannerAdapter()
    meter = adapter.rate_limit_meter
    assert meter is not None
    # Second call should return same meter
    assert adapter.rate_limit_meter is meter


def test_scanner_adapter_record_rate_limit_hit() -> None:
    """record_rate_limit_hit should work without error."""
    adapter = MockScannerAdapter()
    adapter.record_rate_limit_hit(error_code="429")


def test_scanner_adapter_digests_for_pinned_inputs() -> None:
    """_digests_for_pinned_inputs should compute digests."""
    adapter = MockScannerAdapter()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "file1.txt").write_text("content1")
        (root / "file2.txt").write_text("content2")

        scope = ScanScope(roots=(root,), config={"key": "value"})
        digests = adapter._digests_for_pinned_inputs(scope)

        assert "source_files" in digests
        assert isinstance(digests["source_files"], str)
        assert len(digests["source_files"]) == 64  # SHA-256 hex


def test_scanner_adapter_not_iterable() -> None:
    """ScannerAdapter should not be iterable."""
    adapter = MockScannerAdapter()
    try:
        list(adapter)
    except TypeError as e:
        assert "not iterable" in str(e)
    else:
        raise AssertionError("Should raise TypeError")


def test_scan_result_with_empty_findings() -> None:
    """ScanResult with empty findings should work."""
    result = ScanResult(findings=[])
    assert result.finding_hashes() == []


def test_scanner_category_comparison() -> None:
    """ScannerCategory should be comparable as strings."""
    assert ScannerCategory.SAST == "sast"
    assert ScannerCategory.SAST == "sast"
    assert ScannerCategory.SAST != ScannerCategory.SCA
