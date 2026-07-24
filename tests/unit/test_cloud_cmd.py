"""Tests for the ``bernstein cloud`` CLI commands."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
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


def test_cloud_deploy_does_not_point_at_repo_only_template() -> None:
    """``cloud deploy`` must not reference the repo-only template path.

    Wheel users have no ``templates/bernstein-cloud/``; the honest pointer is
    ``bernstein cloud init`` (issue #2784).
    """
    runner = CliRunner()
    result = runner.invoke(cloud_group, ["deploy"])
    assert result.exit_code == 0
    assert "templates/bernstein-cloud" not in result.output
    assert "cloud init" in result.output


# ---------------------------------------------------------------------------
# init - scaffold a runnable, free-tier worker
# ---------------------------------------------------------------------------


def test_cloud_init_scaffolds_runnable_worker(tmp_path: Path) -> None:
    """``cloud init`` writes a wrangler.toml AND the worker its ``main`` names."""
    out = tmp_path / "wrangler.toml"
    runner = CliRunner()
    result = runner.invoke(cloud_group, ["init", "--output", str(out)])
    assert result.exit_code == 0
    assert out.exists()

    toml_text = out.read_text(encoding="utf-8")
    # main names src/index.js, which must now exist (issue #2784 entry-point bug).
    assert 'main = "src/index.js"' in toml_text
    worker = tmp_path / "src" / "index.js"
    assert worker.exists()
    assert "fetch" in worker.read_text(encoding="utf-8")


def test_cloud_init_default_template_is_free_tier(tmp_path: Path) -> None:
    """The scaffolded wrangler.toml has no paid bindings by default."""
    out = tmp_path / "wrangler.toml"
    runner = CliRunner()
    result = runner.invoke(cloud_group, ["init", "--output", str(out)])
    assert result.exit_code == 0
    toml_text = out.read_text(encoding="utf-8")
    # Queues are a Workers Paid feature; they must not be active by default.
    assert "[[queues.producers]]" not in toml_text
    assert "[[queues.consumers]]" not in toml_text


def test_cloud_init_does_not_clobber_existing_worker(tmp_path: Path) -> None:
    """An existing worker file is left untouched by ``cloud init``."""
    out = tmp_path / "wrangler.toml"
    worker = tmp_path / "src" / "index.js"
    worker.parent.mkdir(parents=True, exist_ok=True)
    worker.write_text("// custom worker\n", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cloud_group, ["init", "--output", str(out)])
    assert result.exit_code == 0
    assert worker.read_text(encoding="utf-8") == "// custom worker\n"


# ---------------------------------------------------------------------------
# login / hosted-service honesty
# ---------------------------------------------------------------------------


def test_cloud_login_notes_experimental(tmp_path: Path) -> None:
    """``cloud login`` warns that the hosted service is experimental."""
    _redirect_token_paths(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cloud_group, ["login", "--api-key", "sk-test-123"])
    assert result.exit_code == 0
    assert "Authenticated" in result.output
    assert "experimental" in result.output.lower()


def test_cloud_runs_reports_hosted_service_unavailable(tmp_path: Path) -> None:
    """A connection failure to the hosted API yields a clean message, not a traceback."""
    _redirect_token_paths(tmp_path)
    cloud_cmd._save_token("sk-test", "https://api.bernstein.run")

    failing_client = MagicMock(request=MagicMock(side_effect=httpx.ConnectError("name resolution failed")))
    runner = CliRunner()
    with patch.object(cloud_cmd.httpx.Client, "__enter__", return_value=failing_client):
        result = runner.invoke(cloud_group, ["runs"])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "not reachable" in result.output.lower() or "not currently available" in result.output.lower()


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
