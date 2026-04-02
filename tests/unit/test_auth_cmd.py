"""Tests for CLI authentication flows and token caching."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from click.testing import CliRunner

from bernstein.cli import auth_cmd
from bernstein.cli.main import cli


def _set_token_paths(tmp_path: Path) -> None:
    """Redirect the auth token cache into a temporary directory."""
    auth_cmd._TOKEN_DIR = tmp_path / ".bernstein"  # pyright: ignore[reportPrivateUsage]
    auth_cmd._TOKEN_FILE = auth_cmd._TOKEN_DIR / "token.json"  # pyright: ignore[reportPrivateUsage]


def test_save_and_load_cached_token_roundtrip(tmp_path: Path) -> None:
    """Cached tokens persist expiry metadata and load back as typed entries."""
    _set_token_paths(tmp_path)

    saved = auth_cmd._save_token(  # pyright: ignore[reportPrivateUsage]
        "header.payload.signature",
        "http://server.test",
        expires_at=time.time() + 3600,
        refresh_token="refresh-1",
    )
    loaded = auth_cmd._load_token()  # pyright: ignore[reportPrivateUsage]

    assert loaded == saved
    assert loaded is not None
    assert loaded.expires_at is not None
    assert loaded.expires_at > time.time()
    assert loaded.refresh_token == "refresh-1"


def test_load_cached_token_refreshes_when_expired(tmp_path: Path) -> None:
    """Expired cached tokens are refreshed when a refresh token is available."""
    _set_token_paths(tmp_path)
    auth_cmd._save_token(  # pyright: ignore[reportPrivateUsage]
        "expired-token",
        "http://server.test",
        expires_at=time.time() - 10,
        refresh_token="refresh-1",
    )

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "access_token": "fresh-token",
        "expires_at": time.time() + 3600,
        "refresh_token": "refresh-2",
    }

    from unittest.mock import patch

    with patch("bernstein.cli.auth_cmd.httpx.post", return_value=response):
        loaded = auth_cmd._load_token()  # pyright: ignore[reportPrivateUsage]

    assert loaded is not None
    assert loaded.token == "fresh-token"
    assert loaded.refresh_token == "refresh-2"


def test_top_level_login_sso_opens_browser_and_caches_token(tmp_path: Path) -> None:
    """The top-level ``bernstein login --sso`` alias opens the browser and caches the token."""
    _set_token_paths(tmp_path)
    providers_response = MagicMock()
    providers_response.raise_for_status.return_value = None
    providers_response.json.return_value = {"device_flow_enabled": True, "oidc_enabled": True}

    device_response = MagicMock()
    device_response.raise_for_status.return_value = None
    device_response.json.return_value = {
        "device_code": "device-code",
        "user_code": "USER-CODE",
        "verification_uri": "http://server.test/auth/login",
        "expires_in": 600,
        "interval": 0,
    }

    token_response = MagicMock()
    token_response.raise_for_status.return_value = None
    token_response.json.return_value = {
        "status": "complete",
        "access_token": "header.payload.signature",
        "expires_at": time.time() + 3600,
    }

    runner = CliRunner()

    from unittest.mock import patch

    with (
        patch("bernstein.cli.auth_cmd.httpx.get", return_value=providers_response),
        patch("bernstein.cli.auth_cmd.httpx.post", side_effect=[device_response, token_response]),
        patch("bernstein.cli.auth_cmd.time.sleep", return_value=None),
        patch("bernstein.cli.auth_cmd.webbrowser.open", return_value=True) as open_browser,
    ):
        result = runner.invoke(cli, ["login", "--sso", "--server", "http://server.test"])

    assert result.exit_code == 0
    open_browser.assert_called_once_with("http://server.test/auth/login")
    cached = auth_cmd._load_token()  # pyright: ignore[reportPrivateUsage]
    assert cached is not None
    assert cached.server_url == "http://server.test"
