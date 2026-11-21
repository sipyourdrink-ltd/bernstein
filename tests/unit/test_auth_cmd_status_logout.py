"""CLI checks for masked token status and offline logout."""

from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from click.testing import CliRunner

from bernstein.cli.commands import auth_cmd
from bernstein.cli.main import cli


def test_status_masks_legacy_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_cmd, "_TOKEN_FILE", tmp_path / "missing.json")
    token = "example-token-1234"
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", token)

    result = CliRunner().invoke(cli, ["auth", "status"])

    assert result.exit_code == 0, result.output
    assert "Legacy Token Authentication" in result.output
    assert "********...1234" in result.output
    assert token not in result.output


def test_logout_clears_cached_token_when_revocation_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_dir = tmp_path / ".bernstein"
    monkeypatch.setattr(auth_cmd, "_TOKEN_DIR", token_dir)
    monkeypatch.setattr(auth_cmd, "_TOKEN_FILE", token_dir / "token.json")
    auth_cmd._save_token("test-session", "http://server.test")
    post = Mock(side_effect=httpx.ConnectError("offline"))
    monkeypatch.setattr(auth_cmd.httpx, "post", post)

    result = CliRunner().invoke(cli, ["auth", "logout"])

    assert result.exit_code == 0, result.output
    assert "Could not reach server" in result.output
    assert "Local token cleared" in result.output
    assert not (token_dir / "token.json").exists()
    post.assert_called_once_with(
        "http://server.test/auth/logout",
        headers={"Authorization": "Bearer test-session"},
        timeout=5.0,
    )
