"""Tests for bernstein.cli.self_update_cmd — self-update command."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bernstein.cli.self_update_cmd import (
    _fetch_changelog,
    _get_installed_version,
    _parse_version,
    _pip_install,
    _read_previous_version,
    _save_previous_version,
    self_update_cmd,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# _parse_version
# ---------------------------------------------------------------------------


class TestParseVersion:
    def test_semver(self) -> None:
        assert _parse_version("1.2.3") == (1, 2, 3)

    def test_two_part(self) -> None:
        assert _parse_version("2.0") == (2, 0)

    def test_four_part(self) -> None:
        assert _parse_version("1.2.3.4") == (1, 2, 3, 4)

    def test_non_numeric_segment(self) -> None:
        # Non-numeric segments become 0
        result = _parse_version("1.2.alpha")
        assert result[0] == 1
        assert result[1] == 2
        assert result[2] == 0

    def test_comparison_newer(self) -> None:
        assert _parse_version("1.2.0") < _parse_version("1.3.0")

    def test_comparison_same(self) -> None:
        assert _parse_version("1.1.3") == _parse_version("1.1.3")


# ---------------------------------------------------------------------------
# _get_installed_version
# ---------------------------------------------------------------------------


class TestGetInstalledVersion:
    def test_returns_version_string(self) -> None:
        with patch("bernstein.cli.self_update_cmd._pkg_version", return_value="1.2.3"):
            assert _get_installed_version() == "1.2.3"

    def test_returns_unknown_on_missing(self) -> None:
        from importlib.metadata import PackageNotFoundError

        with patch("bernstein.cli.self_update_cmd._pkg_version", side_effect=PackageNotFoundError):
            assert _get_installed_version() == "unknown"


# ---------------------------------------------------------------------------
# _save_previous_version / _read_previous_version
# ---------------------------------------------------------------------------


class TestPreviousVersionFile:
    def test_round_trip(self, tmp_path: Path) -> None:
        prev_file = tmp_path / ".bernstein" / "previous-version"
        with patch("bernstein.cli.self_update_cmd._PREV_VERSION_FILE", prev_file):
            _save_previous_version("1.0.0")
            assert _read_previous_version() == "1.0.0"

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / ".bernstein" / "previous-version"
        with patch("bernstein.cli.self_update_cmd._PREV_VERSION_FILE", nonexistent):
            assert _read_previous_version() is None

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        prev_file = tmp_path / "deep" / "nested" / "previous-version"
        with patch("bernstein.cli.self_update_cmd._PREV_VERSION_FILE", prev_file):
            _save_previous_version("2.3.4")
            assert prev_file.read_text().strip() == "2.3.4"


# ---------------------------------------------------------------------------
# _fetch_changelog
# ---------------------------------------------------------------------------


class TestFetchChangelog:
    def _make_release(self, tag: str, body: str = "") -> dict[str, str]:
        return {"tag_name": tag, "body": body}

    def test_returns_entries_between_versions(self) -> None:
        releases = [
            self._make_release("v1.3.0", "New feature"),
            self._make_release("v1.2.0", "Bug fix"),
            self._make_release("v1.1.0", "Initial"),
        ]
        raw_response = json.dumps(releases).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = raw_response
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            entries = _fetch_changelog("1.1.0", "1.3.0")

        assert len(entries) == 2
        assert any("1.3.0" in e for e in entries)
        assert any("1.2.0" in e for e in entries)
        # 1.1.0 is the current version — should not be included
        assert not any("1.1.0" in e for e in entries)

    def test_returns_empty_on_network_error(self) -> None:
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            entries = _fetch_changelog("1.0.0", "1.1.0")

        assert entries == []

    def test_truncates_long_body(self) -> None:
        long_body = "x" * 400
        releases = [self._make_release("v2.0.0", long_body)]
        raw = json.dumps(releases).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = raw
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            entries = _fetch_changelog("1.0.0", "2.0.0")

        assert len(entries) == 1
        assert "…" in entries[0]


# ---------------------------------------------------------------------------
# _pip_install
# ---------------------------------------------------------------------------


class TestPipInstall:
    def test_returns_true_on_success(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("bernstein.cli.self_update_cmd.subprocess.run", return_value=mock_result):
            assert _pip_install("bernstein==1.2.3") is True

    def test_returns_false_on_failure(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "ERROR: Could not find version"

        with patch("bernstein.cli.self_update_cmd.subprocess.run", return_value=mock_result):
            assert _pip_install("bernstein==99.0.0") is False


# ---------------------------------------------------------------------------
# self_update_cmd — CLI integration
# ---------------------------------------------------------------------------


class TestSelfUpdateCmd:
    def _runner(self) -> CliRunner:
        return CliRunner()

    def test_check_only_shows_versions(self) -> None:
        runner = self._runner()
        with (
            patch("bernstein.cli.self_update_cmd._get_installed_version", return_value="1.1.0"),
            patch("bernstein.cli.self_update_cmd._fetch_latest_pypi_version", return_value="1.2.0"),
        ):
            result = runner.invoke(self_update_cmd, ["--check"])

        assert result.exit_code == 0
        assert "1.1.0" in result.output
        assert "1.2.0" in result.output

    def test_up_to_date_message(self) -> None:
        runner = self._runner()
        with (
            patch("bernstein.cli.self_update_cmd._get_installed_version", return_value="1.2.0"),
            patch("bernstein.cli.self_update_cmd._fetch_latest_pypi_version", return_value="1.2.0"),
        ):
            result = runner.invoke(self_update_cmd, [])

        assert result.exit_code == 0
        assert "up to date" in result.output.lower()

    def test_upgrade_auto_yes(self, tmp_path: Path) -> None:
        prev_file = tmp_path / "previous-version"
        runner = self._runner()
        with (
            patch("bernstein.cli.self_update_cmd._get_installed_version", return_value="1.1.0"),
            patch("bernstein.cli.self_update_cmd._fetch_latest_pypi_version", return_value="1.2.0"),
            patch("bernstein.cli.self_update_cmd._fetch_changelog", return_value=[]),
            patch("bernstein.cli.self_update_cmd._PREV_VERSION_FILE", prev_file),
            patch("bernstein.cli.self_update_cmd._pip_install", return_value=True) as mock_pip,
        ):
            result = runner.invoke(self_update_cmd, ["--yes"])

        assert result.exit_code == 0
        assert "Successfully upgraded" in result.output
        mock_pip.assert_called_once_with("bernstein==1.2.0")
        assert prev_file.read_text().strip() == "1.1.0"

    def test_upgrade_cancelled_at_prompt(self) -> None:
        runner = self._runner()
        with (
            patch("bernstein.cli.self_update_cmd._get_installed_version", return_value="1.1.0"),
            patch("bernstein.cli.self_update_cmd._fetch_latest_pypi_version", return_value="1.2.0"),
            patch("bernstein.cli.self_update_cmd._fetch_changelog", return_value=[]),
            patch("bernstein.cli.self_update_cmd._pip_install") as mock_pip,
        ):
            result = runner.invoke(self_update_cmd, [], input="n\n")

        assert result.exit_code == 0
        assert "cancelled" in result.output.lower()
        mock_pip.assert_not_called()

    def test_pypi_unreachable(self) -> None:
        runner = self._runner()
        with (
            patch("bernstein.cli.self_update_cmd._get_installed_version", return_value="1.0.0"),
            patch("bernstein.cli.self_update_cmd._fetch_latest_pypi_version", return_value=None),
        ):
            result = runner.invoke(self_update_cmd, [])

        assert result.exit_code != 0

    def test_pip_failure_exits_nonzero(self, tmp_path: Path) -> None:
        prev_file = tmp_path / "previous-version"
        runner = self._runner()
        with (
            patch("bernstein.cli.self_update_cmd._get_installed_version", return_value="1.1.0"),
            patch("bernstein.cli.self_update_cmd._fetch_latest_pypi_version", return_value="1.2.0"),
            patch("bernstein.cli.self_update_cmd._fetch_changelog", return_value=[]),
            patch("bernstein.cli.self_update_cmd._PREV_VERSION_FILE", prev_file),
            patch("bernstein.cli.self_update_cmd._pip_install", return_value=False),
        ):
            result = runner.invoke(self_update_cmd, ["--yes"])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


class TestRollbackCmd:
    def _runner(self) -> CliRunner:
        return CliRunner()

    def test_rollback_no_previous_version(self, tmp_path: Path) -> None:
        prev_file = tmp_path / "previous-version"
        runner = self._runner()

        with patch("bernstein.cli.self_update_cmd._PREV_VERSION_FILE", prev_file):
            result = runner.invoke(self_update_cmd, ["--rollback"])

        assert result.exit_code != 0
        assert "No previous version" in result.output

    def test_rollback_success(self, tmp_path: Path) -> None:
        prev_file = tmp_path / "previous-version"
        prev_file.write_text("1.0.0")
        runner = self._runner()

        with (
            patch("bernstein.cli.self_update_cmd._PREV_VERSION_FILE", prev_file),
            patch("bernstein.cli.self_update_cmd._get_installed_version", return_value="1.2.0"),
            patch("bernstein.cli.self_update_cmd._pip_install", return_value=True),
        ):
            result = runner.invoke(self_update_cmd, ["--rollback"])

        assert result.exit_code == 0
        assert "Rolled back" in result.output
        # Rollback file should be removed after success
        assert not prev_file.exists()

    def test_rollback_pip_failure(self, tmp_path: Path) -> None:
        prev_file = tmp_path / "previous-version"
        prev_file.write_text("1.0.0")
        runner = self._runner()

        with (
            patch("bernstein.cli.self_update_cmd._PREV_VERSION_FILE", prev_file),
            patch("bernstein.cli.self_update_cmd._get_installed_version", return_value="1.2.0"),
            patch("bernstein.cli.self_update_cmd._pip_install", return_value=False),
        ):
            result = runner.invoke(self_update_cmd, ["--rollback"])

        assert result.exit_code != 0
        # Rollback file should still exist on failure
        assert prev_file.exists()
