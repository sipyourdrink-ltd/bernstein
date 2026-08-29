"""Tests for scanner_registry.py - Scanner registry and lookup."""

from __future__ import annotations

import pytest

from bernstein.adapters.scanner import (
    DeterminismTier,
    OutputFormat,
    ScannerAdapter,
    ScannerCategory,
    ScanResult,
    ScanScope,
)
from bernstein.adapters.scanner_finding import Finding


class DummyScanner(ScannerAdapter):
    """A minimal concrete scanner for testing."""

    registry_name = "dummy-test"
    output_format = OutputFormat.JSON
    determinism = DeterminismTier.DETERMINISTIC
    pinned_inputs = ()
    category = ScannerCategory.SAST

    def name(self) -> str:
        return self.registry_name

    def scan(self, target, scope: ScanScope, workdir) -> ScanResult:
        return ScanResult(findings=[Finding(rule="dummy", path=str(target))])


class AnotherScanner(ScannerAdapter):
    """Another minimal scanner for testing."""

    registry_name = "another-test"
    output_format = OutputFormat.SARIF
    determinism = DeterminismTier.TRANSCRIPT_ANCHORED
    pinned_inputs = ()
    category = ScannerCategory.SECRET

    def name(self) -> str:
        return self.registry_name

    def scan(self, target, scope: ScanScope, workdir) -> ScanResult:
        return ScanResult(findings=[])


@pytest.fixture(autouse=True)
def _reset_scanner_state():
    """Reset scanner registry state before and after each test."""
    # Import the module to access its globals
    import bernstein.adapters.scanner_registry as scanner_reg

    # Save original state
    orig_scanners = dict(scanner_reg._SCANNERS)

    # Reset
    scanner_reg._SCANNERS.clear()
    scanner_reg._entrypoints_loaded = False

    yield

    # Restore
    scanner_reg._SCANNERS.update(orig_scanners)
