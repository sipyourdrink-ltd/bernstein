"""Tests for the ``bernstein cloud`` CLI commands."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bernstein.cli.commands import cloud_cmd
from bernstein.cli.commands.cloud_cmd import cloud_group


def _redirect_token_paths(tmp_path: Path) -> None:
    """Point token storage at *tmp_path* so tests never touch ``~/.config``."""
    cloud_cmd._CONFIG_DIR = tmp_path / ".config" / "bernstein"
    cloud_cmd._TOKEN_FILE = cloud_cmd._CONFIG_DIR / "cloud-token.json"


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_cloud_login_saves_token(tmp_path: Path) -> None:
    """``cloud login --api-key`` persists the token file."""
    _redirect_token_paths(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cloud_group, ["login", "--api-key", "sk-test-123"])
    assert result.exit_code == 0
    assert "Authenticated" in result.output
    assert cloud_cmd._TOKEN_FILE.exists()
    data = json.loads(cloud_cmd._TOKEN_FILE.read_text())
    assert data["api_key"] == "sk-test-123"


def test_cloud_login_prompts_when_no_key(tmp_path: Path) -> None:
    """``cloud login`` without --api-key prompts the user interactively."""
    _redirect_token_paths(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cloud_group, ["login"], input="sk-prompted\n")
    assert result.exit_code == 0
    assert "Authenticated" in result.output
    data = json.loads(cloud_cmd._TOKEN_FILE.read_text())
    assert data["api_key"] == "sk-prompted"


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------


def test_cloud_logout_removes_token(tmp_path: Path) -> None:
    """``cloud logout`` deletes the stored token file."""
    _redirect_token_paths(tmp_path)
    cloud_cmd._save_token("sk-test", "https://api.bernstein.run")
    assert cloud_cmd._TOKEN_FILE.exists()

    runner = CliRunner()
    result = runner.invoke(cloud_group, ["logout"])
    assert result.exit_code == 0
    assert "Logged out" in result.output
    assert not cloud_cmd._TOKEN_FILE.exists()


def test_cloud_logout_when_not_logged_in(tmp_path: Path) -> None:
    """``cloud logout`` without a stored token prints a message."""
    _redirect_token_paths(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cloud_group, ["logout"])
    assert result.exit_code == 0
    assert "Not logged in" in result.output


# ---------------------------------------------------------------------------
# run (auth guard)
# ---------------------------------------------------------------------------


def test_cloud_run_without_login_shows_error(tmp_path: Path) -> None:
    """``cloud run`` exits with error when not logged in."""
    _redirect_token_paths(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cloud_group, ["run", "build the feature"])
    assert result.exit_code != 0
    assert "Not logged in" in result.output


# ---------------------------------------------------------------------------
# status (auth guard)
# ---------------------------------------------------------------------------


def test_cloud_status_without_login_shows_error(tmp_path: Path) -> None:
    """``cloud status`` exits with error when not logged in."""
    _redirect_token_paths(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cloud_group, ["status"])
    assert result.exit_code != 0
    assert "Not logged in" in result.output


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


def test_cloud_runs_lists_runs(tmp_path: Path) -> None:
    """``cloud runs`` lists recent runs from the API."""
    _redirect_token_paths(tmp_path)
    cloud_cmd._save_token("sk-test", "https://api.bernstein.run")

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = [
        {"id": "run-1", "status": "complete", "goal": "fix bug"},
        {"id": "run-2", "status": "running", "goal": "add feature"},
    ]

    runner = CliRunner()
    with patch.object(
        cloud_cmd.httpx.Client, "__enter__", return_value=MagicMock(request=MagicMock(return_value=mock_resp))
    ):
        result = runner.invoke(cloud_group, ["runs"])

    assert result.exit_code == 0
    assert "run-1" in result.output
    assert "run-2" in result.output


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


def test_cloud_cost_shows_usage(tmp_path: Path) -> None:
    """``cloud cost`` shows billing usage."""
    _redirect_token_paths(tmp_path)
    cloud_cmd._save_token("sk-test", "https://api.bernstein.run")

    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {
        "period": "2026-04",
        "total_cost": 42.50,
        "run_count": 15,
    }

    runner = CliRunner()
    with patch.object(
        cloud_cmd.httpx.Client, "__enter__", return_value=MagicMock(request=MagicMock(return_value=mock_resp))
    ):
        result = runner.invoke(cloud_group, ["cost"])

    assert result.exit_code == 0
    assert "42.50" in result.output
    assert "15" in result.output


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


def test_cloud_deploy_shows_instructions() -> None:
    """``cloud deploy`` prints deployment instructions."""
    runner = CliRunner()
    result = runner.invoke(cloud_group, ["deploy"])
    assert result.exit_code == 0
    assert "wrangler deploy" in result.output
    assert "bernstein-agent" in result.output


# ---------------------------------------------------------------------------
# _save_token / _load_token
# ---------------------------------------------------------------------------


def test_save_token_creates_file_with_600_permissions(tmp_path: Path) -> None:
    """``_save_token`` creates the token file with 0o600 permissions."""
    _redirect_token_paths(tmp_path)
    cloud_cmd._save_token("sk-test", "https://api.bernstein.run")
    file_stat = cloud_cmd._TOKEN_FILE.stat()
    mode = stat.S_IMODE(file_stat.st_mode)
    assert mode == 0o600


def test_load_token_returns_none_when_no_file(tmp_path: Path) -> None:
    """``_load_token`` returns None when no token file exists."""
    _redirect_token_paths(tmp_path)
    assert cloud_cmd._load_token() is None


def test_load_token_returns_data_when_valid(tmp_path: Path) -> None:
    """``_load_token`` returns stored credentials."""
    _redirect_token_paths(tmp_path)
    cloud_cmd._save_token("sk-test", "https://api.bernstein.run")
    token = cloud_cmd._load_token()
    assert token is not None
    assert token["api_key"] == "sk-test"
    assert token["url"] == "https://api.bernstein.run"


def test_load_token_returns_none_on_corrupt_file(tmp_path: Path) -> None:
    """``_load_token`` returns None when the token file is corrupt."""
    _redirect_token_paths(tmp_path)
    cloud_cmd._CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cloud_cmd._TOKEN_FILE.write_text("not-json{{{", encoding="utf-8")
    assert cloud_cmd._load_token() is None


# ---------------------------------------------------------------------------
# _cloud_request
# ---------------------------------------------------------------------------


def test_cloud_request_builds_correct_url_and_headers() -> None:
    """``_cloud_request`` constructs the URL and auth header correctly."""
    token = {"api_key": "sk-test-key", "url": "https://api.bernstein.run"}

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_client.request.return_value = mock_resp

    with patch("bernstein.cli.commands.cloud_cmd.httpx.Client") as mock_cls:
        mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = cloud_cmd._cloud_request("GET", "/runs", token)

    mock_client.request.assert_called_once()
    call_args = mock_client.request.call_args
    assert call_args[0] == ("GET", "https://api.bernstein.run/runs")
    headers = call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer sk-test-key"
    assert headers["Content-Type"] == "application/json"
    assert result is mock_resp
