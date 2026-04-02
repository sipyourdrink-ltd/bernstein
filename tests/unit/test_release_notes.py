"""Tests for release_notes — release notes fetch and display."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.release_notes import (
    _fetch_remote,
    _find_local_changelog,
    fetch_release_notes,
    format_for_terminal,
)

# --- Fixtures ---


@pytest.fixture()
def project_with_changelog(tmp_path: Path) -> Path:
    """Create project with CHANGELOG.md."""
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## v1.0.0\n- Initial release\n\n## v0.9.0\n- Pre-release\n",
        encoding="utf-8",
    )
    return tmp_path


# --- TestFetchRemote ---


class TestFetchRemote:
    def test_success(self) -> None:
        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = "# Remote Changelog"
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = fake_resp
            result = _fetch_remote("http://example.com/cl.md")
        assert result == "# Remote Changelog"

    def test_http_error_returns_none(self) -> None:
        fake_resp = MagicMock()
        fake_resp.status_code = 404
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = fake_resp
            result = _fetch_remote("http://bad-url")
        assert result is None

    def test_exception_returns_none(self) -> None:
        with patch("httpx.Client", side_effect=ConnectionError):
            result = _fetch_remote("http://bad-url")
        assert result is None


# --- TestFindLocalChangelog ---


class TestFindLocalChangelog:
    def test_finds_changelog_md(self, project_with_changelog: Path) -> None:
        content = _find_local_changelog(project_with_changelog)
        assert content is not None
        assert "# Changelog" in content

    def test_returns_none_if_missing(self, tmp_path: Path) -> None:
        assert _find_local_changelog(tmp_path) is None


# --- TestFetchReleaseNotes ---


class TestFetchReleaseNotes:
    def test_remote_success(self) -> None:
        with patch("bernstein.release_notes._fetch_remote", return_value="# Remote"):
            result = fetch_release_notes()
        assert "# Remote" in result

    def test_falls_back_to_local(self, project_with_changelog: Path) -> None:
        with patch("bernstein.release_notes._fetch_remote", return_value=None):
            result = fetch_release_notes(workdir=project_with_changelog)
        assert "# Changelog" in result

    def test_error_message_when_all_fail(self, tmp_path: Path) -> None:
        with patch("bernstein.release_notes._fetch_remote", return_value=None):
            result = fetch_release_notes(workdir=tmp_path)
        assert "not available" in result or "release notes" in result.lower()


# --- TestFormatForTerminal ---


class TestFormatForTerminal:
    def test_strips_heading_markers(self) -> None:
        raw = "# Changelog\n\n## v1.0\n\n- item\n"
        formatted = format_for_terminal(raw)
        assert "Changelog" in formatted
        assert "#" not in formatted.split("\n")[0]

    def test_respects_max_lines(self) -> None:
        raw = "\n".join(f"line {i}" for i in range(200))
        formatted = format_for_terminal(raw, max_lines=10)
        assert len(formatted.splitlines()) == 11  # 10 + truncation
        assert "... (truncated)" in formatted

    def test_skips_empty_lines(self) -> None:
        raw = "# Heading\n\n## Sub\n\n\n- item\n"
        formatted = format_for_terminal(raw)
        lines = formatted.splitlines()
        # Non-empty lines only
        assert all(line.strip() for line in lines)
