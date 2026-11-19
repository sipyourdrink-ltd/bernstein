"""Tests for release_notes - release notes fetch and display."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from packaging.utils import canonicalize_version

from bernstein.cli.release_notes import (
    _fetch_remote,
    _find_local_changelog,
    fetch_release_notes,
    format_for_terminal,
    installed_version,
    latest_release_notes_path,
    release_notes_url,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# --- Fixtures ---


@pytest.fixture()
def project_with_changelog(tmp_path: Path) -> Path:
    """Create a project holding only the root CHANGELOG.md pointer document."""
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\nRelease history lives in docs/release-notes/.\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def project_with_release_notes(tmp_path: Path) -> Path:
    """Create a project whose maintained surface is docs/release-notes/."""
    notes = tmp_path / "docs" / "release-notes"
    notes.mkdir(parents=True)
    for name, body in (
        ("v3.9.0.md", "# v3.9.0\n- older\n"),
        ("v3.12.0.md", "# v3.12.0\n- newest release\n"),
        ("unreleased.md", "# Unreleased\n- not tagged yet\n"),
    ):
        (notes / name).write_text(body, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\npointer\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def project_with_prerelease_notes(project_with_release_notes: Path) -> Path:
    notes = project_with_release_notes / "docs" / "release-notes"
    (notes / "v4.0.0-rc1.md").write_text("# v4.0.0-rc1\n- release candidate\n", encoding="utf-8")
    return project_with_release_notes


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


# --- TestReleaseNotesUrl ---


class TestReleaseNotesUrl:
    def test_defaults_to_installed_version(self) -> None:
        assert release_notes_url().endswith(f"/docs/release-notes/v{installed_version()}.md")

    def test_explicit_version(self) -> None:
        assert release_notes_url("1.2.3").endswith("/docs/release-notes/v1.2.3.md")


# --- TestLatestReleaseNotesPath ---


class TestLatestReleaseNotesPath:
    def test_picks_highest_version_not_lexicographic_max(self, project_with_release_notes: Path) -> None:
        latest = latest_release_notes_path(project_with_release_notes)
        assert latest is not None
        # A lexicographic max over the file names would return v3.9.0.md.
        assert latest.name == "v3.12.0.md"

    def test_ignores_unversioned_pages(self, project_with_release_notes: Path) -> None:
        latest = latest_release_notes_path(project_with_release_notes)
        assert latest is not None
        assert latest.name != "unreleased.md"

    def test_returns_none_without_directory(self, tmp_path: Path) -> None:
        assert latest_release_notes_path(tmp_path) is None


# --- TestFindLocalChangelog ---


class TestFindLocalChangelog:
    def test_prefers_release_notes_over_pointer(self, project_with_release_notes: Path) -> None:
        content = _find_local_changelog(project_with_release_notes)
        assert content is not None
        assert "newest release" in content

    def test_falls_back_to_changelog_md(self, project_with_changelog: Path) -> None:
        content = _find_local_changelog(project_with_changelog)
        assert content is not None
        assert "# Changelog" in content

    def test_returns_none_if_missing(self, tmp_path: Path) -> None:
        assert _find_local_changelog(tmp_path) is None


# --- TestFetchReleaseNotes ---


class TestFetchReleaseNotes:
    def test_remote_success(self) -> None:
        with patch("bernstein.cli.release_notes._fetch_remote", return_value="# Remote"):
            result = fetch_release_notes()
        assert "# Remote" in result

    def test_falls_back_to_local_release_notes(self, project_with_release_notes: Path) -> None:
        with patch("bernstein.cli.release_notes._fetch_remote", return_value=None):
            result = fetch_release_notes(workdir=project_with_release_notes)
        assert "newest release" in result

    @pytest.mark.parametrize("version", ["4.0.0-rc1", "4.0.0rc1"])
    def test_offline_prerelease_matches_equivalent_notes(
        self, project_with_prerelease_notes: Path, version: str
    ) -> None:
        with (
            patch("bernstein.cli.release_notes.installed_version", return_value=version),
            patch("bernstein.cli.release_notes._fetch_remote", return_value=None),
        ):
            result = fetch_release_notes(workdir=project_with_prerelease_notes)
        assert "release candidate" in result

    def test_offline_prerelease_prefers_exact_filename(self, project_with_prerelease_notes: Path) -> None:
        notes = project_with_prerelease_notes / "docs" / "release-notes"
        (notes / "v4.0.0rc1.md").write_text("# Exact release candidate\n", encoding="utf-8")
        with (
            patch("bernstein.cli.release_notes.installed_version", return_value="4.0.0rc1"),
            patch("bernstein.cli.release_notes._fetch_remote", return_value=None),
        ):
            result = fetch_release_notes(workdir=project_with_prerelease_notes)
        assert result == "# Exact release candidate\n"

    @pytest.mark.parametrize("stable_page_exists", [True, False])
    def test_offline_unreadable_prerelease_uses_stable_or_pointer(
        self, project_with_prerelease_notes: Path, stable_page_exists: bool
    ) -> None:
        notes = project_with_prerelease_notes / "docs" / "release-notes"
        (notes / "v4.0.0-rc1.md").write_bytes(b"\xff")
        if not stable_page_exists:
            (notes / "v3.9.0.md").unlink()
            (notes / "v3.12.0.md").unlink()
        with (
            patch("bernstein.cli.release_notes.installed_version", return_value="4.0.0-rc1"),
            patch("bernstein.cli.release_notes._fetch_remote", return_value=None),
        ):
            result = fetch_release_notes(workdir=project_with_prerelease_notes)
        assert ("newest release" if stable_page_exists else "pointer") in result

    def test_offline_override_url_uses_stable_notes(self, project_with_prerelease_notes: Path) -> None:
        with (
            patch("bernstein.cli.release_notes.installed_version", return_value="4.0.0-rc1"),
            patch("bernstein.cli.release_notes._fetch_remote", return_value=None) as fetch,
        ):
            result = fetch_release_notes(url="https://example.com/notes", workdir=project_with_prerelease_notes)
        fetch.assert_called_once_with("https://example.com/notes")
        assert "newest release" in result

    @pytest.mark.parametrize("version", ["3.9.0", "4.0.0-rc2"])
    def test_offline_without_matching_prerelease_uses_latest_stable(
        self, project_with_prerelease_notes: Path, version: str
    ) -> None:
        with (
            patch("bernstein.cli.release_notes.installed_version", return_value=version),
            patch("bernstein.cli.release_notes._fetch_remote", return_value=None),
        ):
            result = fetch_release_notes(workdir=project_with_prerelease_notes)
        assert "newest release" in result

    def test_falls_back_to_local(self, project_with_changelog: Path) -> None:
        with patch("bernstein.cli.release_notes._fetch_remote", return_value=None):
            result = fetch_release_notes(workdir=project_with_changelog)
        assert "# Changelog" in result

    def test_error_message_when_all_fail(self, tmp_path: Path) -> None:
        with patch("bernstein.cli.release_notes._fetch_remote", return_value=None):
            result = fetch_release_notes(workdir=tmp_path)
        assert "not available" in result or "release notes" in result.lower()

    def test_served_notes_match_the_current_version(self) -> None:
        """Offline notes use the installed version, including normalized metadata."""
        version = installed_version()
        latest = latest_release_notes_path(_REPO_ROOT)
        with patch("bernstein.cli.release_notes._fetch_remote", return_value=None):
            served = fetch_release_notes(workdir=_REPO_ROOT)
        normalized = canonicalize_version(version)
        matching_pages = (
            page
            for page in (_REPO_ROOT / "docs" / "release-notes").glob("v*.md")
            if canonicalize_version(page.stem[1:]) == normalized
        )
        assert any(page.read_text(encoding="utf-8") == served for page in matching_pages), (
            f"the release-notes page for version {version} was not served; "
            f"newest stable page is {latest.name if latest else 'missing'}."
        )


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
