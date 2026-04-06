"""Tests for adapter auto-detection (AGENT-015)."""

from __future__ import annotations

from unittest.mock import patch

from bernstein.core.adapter_autodetect import (
    _KNOWN_BINARIES,
    DetectedAdapter,
    ScanResult,
    auto_register_adapters,
    scan_for_adapters,
)


class TestScanForAdapters:
    def test_scan_finds_nothing_when_path_empty(self) -> None:
        with patch("bernstein.core.adapter_autodetect.shutil.which", return_value=None):
            result = scan_for_adapters()
        assert len(result.found) == 0
        assert len(result.missing) == len(_KNOWN_BINARIES)

    def test_scan_finds_known_binary(self) -> None:
        def mock_which(name: str) -> str | None:
            if name == "claude":
                return "/usr/bin/claude"
            return None

        with patch("bernstein.core.adapter_autodetect.shutil.which", side_effect=mock_which):
            result = scan_for_adapters()
        found_names = [d.adapter_name for d in result.found]
        assert "claude" in found_names
        claude = next(d for d in result.found if d.adapter_name == "claude")
        assert claude.binary_path == "/usr/bin/claude"

    def test_scan_extra_binaries(self) -> None:
        def mock_which(name: str) -> str | None:
            if name == "my-agent":
                return "/usr/local/bin/my-agent"
            return None

        with patch("bernstein.core.adapter_autodetect.shutil.which", side_effect=mock_which):
            result = scan_for_adapters(extra_binaries={"my-agent": "myagent"})
        found_names = [d.adapter_name for d in result.found]
        assert "myagent" in found_names

    def test_scan_result_structure(self) -> None:
        result = ScanResult()
        assert result.found == []
        assert result.missing == []


class TestDetectedAdapter:
    def test_fields(self) -> None:
        da = DetectedAdapter(
            adapter_name="claude",
            binary_name="claude",
            binary_path="/usr/bin/claude",
        )
        assert da.adapter_name == "claude"
        assert da.binary_path == "/usr/bin/claude"


class TestAutoRegisterAdapters:
    def test_auto_register_no_crash(self) -> None:
        with patch("bernstein.core.adapter_autodetect.shutil.which", return_value=None):
            result = auto_register_adapters()
        assert isinstance(result, ScanResult)

    def test_known_binaries_mapping(self) -> None:
        # Verify the mapping contains expected entries
        assert "claude" in _KNOWN_BINARIES
        assert "codex" in _KNOWN_BINARIES
        assert "gemini" in _KNOWN_BINARIES
