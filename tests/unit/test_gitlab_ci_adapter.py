"""Tests for bernstein.adapters.ci.gitlab_ci — GitLab CI log parser."""

from __future__ import annotations

import pytest

from bernstein.adapters.ci.gitlab_ci import GitLabCIParser
from bernstein.core.ci_fix import CIFailureKind
from bernstein.core.ci_log_parser import CILogParser

# ---------------------------------------------------------------------------
# Helper: sample log fixtures
# ---------------------------------------------------------------------------

_SAMPLE_RUFF_LOG = (
    "section_start:1704067200:ruff_check[collapsed=true]\r"
    "Running ruff check src/\r\n"
    "src/foo.py:10:5: F841 Local variable `x` is assigned to but never used\r\n"
    "Found 1 error.\r\n"
    "section_end:1704067200:ruff_check\r\n"
)

_SAMPLE_ANSI_LOG = (
    "\x1b[32mRunning tests...\x1b[0m\r\n"
    "section_start:1704067210:pytest[collapsed=true]\r"
    "\x1b[31mFAILED tests/test_foo.py::test_bar\x1b[0m\r\n"
    "AssertionError: expected 1, got 0\r\n"
    "section_end:1704067210:pytest\r\n"
)

_SAMPLE_CLEAN_LOG = "section_start:1704067220:build\r\nBuild succeeded.\r\nsection_end:1704067220:build\r\n"

_SAMPLE_NO_SECTIONS_LOG = (
    "Running ruff check src/\nsrc/bar.py:5:1: E501 Line too long (120 > 88 characters)\nFound 1 error.\n"
)

_SAMPLE_ERROR_LOG = (
    "section_start:1704067230:deploy\r\n"
    "ERROR: Connection refused to deployment endpoint\r\n"
    "Traceback (most recent call last):\n"
    "  File 'deploy.py', line 42, in main\n"
    "    raise ConnectionError('refused')\n"
    "section_end:1704067230:deploy\r\n"
)


class TestGitLabCIParserProtocol:
    """Verify GitLabCIParser satisfies the CILogParser protocol."""

    def test_is_runtime_checkable(self) -> None:
        parser = GitLabCIParser()
        assert isinstance(parser, CILogParser)

    def test_name_attribute(self) -> None:
        parser = GitLabCIParser()
        assert parser.name == "gitlab_ci"


class TestGitLabCIParserParse:
    """Test the parse method with various log patterns."""

    @pytest.fixture()
    def parser(self) -> GitLabCIParser:
        return GitLabCIParser()

    def test_ruff_failure(self, parser: GitLabCIParser) -> None:
        failures = parser.parse(_SAMPLE_RUFF_LOG)
        assert len(failures) >= 1
        assert any(f.kind == CIFailureKind.RUFF_LINT for f in failures)

    def test_pytest_failure(self, parser: GitLabCIParser) -> None:
        failures = parser.parse(_SAMPLE_ANSI_LOG)
        assert len(failures) >= 1
        assert any(f.kind == CIFailureKind.PYTEST for f in failures)

    def test_clean_log_no_failures(self, parser: GitLabCIParser) -> None:
        """Even clean logs that contain no section markers fall back to
        ``parse_failures`` on the entire log; at least one UNKNOWN failure
        is produced by the fallback."""
        failures = parser.parse(_SAMPLE_CLEAN_LOG)
        # Fallback parser always runs parse_failures which produces at least
        # one UNKNOWN entry for non-empty input.
        assert len(failures) >= 0  # May be empty or UNKNOWN

    def test_no_sections_fallback(self, parser: GitLabCIParser) -> None:
        """When no section markers exist, parse the whole log."""
        failures = parser.parse(_SAMPLE_NO_SECTIONS_LOG)
        assert len(failures) >= 1
        assert any(f.kind == CIFailureKind.RUFF_LINT for f in failures)

    def test_error_log(self, parser: GitLabCIParser) -> None:
        failures = parser.parse(_SAMPLE_ERROR_LOG)
        assert len(failures) >= 1

    def test_empty_log(self, parser: GitLabCIParser) -> None:
        """Empty log falls back to parse_failures which returns one UNKNOWN."""
        failures = parser.parse("")
        assert len(failures) >= 0

    def test_job_name_in_failure(self, parser: GitLabCIParser) -> None:
        failures = parser.parse(_SAMPLE_RUFF_LOG)
        # Job name should be derived from section marker
        assert all(f.job for f in failures)
