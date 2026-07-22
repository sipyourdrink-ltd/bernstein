"""Regression tests for issue #2794.

A detached run auto-generates a Bearer token that historically lived only in
the launcher process environment. Out-of-process CLI monitors then hit ``401``
and misreported it as "server unreachable". These tests pin the fix:

* the launcher persists the auto-generated token to a ``0600`` file under
  ``.sdd/runtime``;
* :func:`bernstein.cli.helpers.auth_headers` falls back to that file when the
  caller's env has no ``BERNSTEIN_AUTH_TOKEN``;
* ``server_get`` distinguishes a ``401`` (server up, bad creds) from an
  unreachable server, so monitor commands print distinct diagnostics.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from bernstein.cli import helpers
from bernstein.core.defaults import SDD_AUTH_TOKEN
from bernstein.core.run_auth_token import (
    persist_run_auth_token,
    read_run_auth_token,
    run_auth_token_path,
)


def test_persist_run_auth_token_creates_0600_file(tmp_path: Path) -> None:
    written = persist_run_auth_token(tmp_path, "s3cr3t-token")

    assert written == run_auth_token_path(tmp_path)
    assert written is not None
    mode = stat.S_IMODE(written.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    assert written.read_text(encoding="utf-8") == "s3cr3t-token"


def test_persist_then_read_round_trips(tmp_path: Path) -> None:
    persist_run_auth_token(tmp_path, "round-trip-token")
    assert read_run_auth_token(tmp_path) == "round-trip-token"


def test_read_run_auth_token_missing_returns_none(tmp_path: Path) -> None:
    assert read_run_auth_token(tmp_path) is None


def test_ensure_sdd_gitignores_auth_token(tmp_path: Path) -> None:
    """The token file must be git-ignored so the secret never lands in a commit (#2762)."""
    from bernstein.core.server_launch import ensure_sdd

    ensure_sdd(tmp_path)
    gi = (tmp_path / ".sdd" / "runtime" / ".gitignore").read_text(encoding="utf-8")
    assert "auth.token" in gi


def test_ensure_sdd_appends_auth_token_to_existing_gitignore(tmp_path: Path) -> None:
    from bernstein.core.server_launch import ensure_sdd

    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / ".gitignore").write_text("session.json\n", encoding="utf-8")

    ensure_sdd(tmp_path)
    gi = (runtime / ".gitignore").read_text(encoding="utf-8")
    assert "auth.token" in gi
    assert "session.json" in gi


def test_persist_overwrites_previous_session_token(tmp_path: Path) -> None:
    persist_run_auth_token(tmp_path, "old-token")
    persist_run_auth_token(tmp_path, "new-token")
    assert read_run_auth_token(tmp_path) == "new-token"
    assert stat.S_IMODE(run_auth_token_path(tmp_path).stat().st_mode) == 0o600


def test_auth_headers_falls_back_to_token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh shell with no env token still authenticates via the on-disk token."""
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    persist_run_auth_token(tmp_path, "file-token")

    assert helpers.auth_headers() == {"Authorization": "Bearer file-token"}


def test_auth_headers_env_wins_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "env-token")
    persist_run_auth_token(tmp_path, "file-token")

    assert helpers.auth_headers() == {"Authorization": "Bearer env-token"}


def test_auth_headers_none_when_no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)

    assert helpers.auth_headers() == {}


def _make_401_response() -> httpx.Response:
    request = httpx.Request("GET", "http://127.0.0.1:8052/status")
    return httpx.Response(401, request=request, json={"detail": "Missing or invalid Authorization header"})


def test_server_get_raises_server_auth_error_on_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return _make_401_response()

    with patch.object(helpers.httpx, "get", fake_get), pytest.raises(helpers.ServerAuthError):
        helpers.server_get("/status", raise_on_auth_error=True)


def test_server_get_returns_none_on_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with patch.object(helpers.httpx, "get", fake_get):
        assert helpers.server_get("/status", raise_on_auth_error=True) is None


def test_server_get_401_without_optin_stays_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Back-compat: the 69 existing callers must keep receiving ``None`` on 401."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BERNSTEIN_SERVER_URL", raising=False)

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        return _make_401_response()

    with patch.object(helpers.httpx, "get", fake_get):
        assert helpers.server_get("/status") is None


def test_resolve_auth_token_persists_to_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)

    from bernstein.core.server_launch import _resolve_auth_token

    token = _resolve_auth_token(tmp_path)

    assert token is not None
    token_file = tmp_path / SDD_AUTH_TOKEN
    assert token_file.read_text(encoding="utf-8") == token
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    # And still exported to env for the in-process server + client.
    import os

    assert os.environ.get("BERNSTEIN_AUTH_TOKEN") == token


def test_status_command_distinguishes_401_from_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 prints a credentials-specific message, not "Is Bernstein running?"."""
    from click.testing import CliRunner

    from bernstein.cli.commands import status_cmd

    runner = CliRunner()

    def raise_auth(path: str, **kwargs: object) -> None:
        raise helpers.ServerAuthError(401)

    with patch.object(status_cmd, "server_get", raise_auth):
        auth_result = runner.invoke(status_cmd.status, [])

    def return_none(path: str, **kwargs: object) -> None:
        return None

    with patch.object(status_cmd, "server_get", return_none):
        unreachable_result = runner.invoke(status_cmd.status, [])

    assert auth_result.exit_code == 1
    assert unreachable_result.exit_code == 1
    combined_auth = auth_result.output.lower()
    combined_unreachable = unreachable_result.output.lower()
    assert "credential" in combined_auth or "rejected" in combined_auth
    assert "is bernstein running" not in combined_auth
    assert "is bernstein running" in combined_unreachable


def test_recap_command_distinguishes_401_from_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bernstein recap`` reports a 401 distinctly from an unreachable server."""
    from click.testing import CliRunner

    from bernstein.cli.commands import advanced_cmd

    runner = CliRunner()

    def raise_auth(path: str, **kwargs: object) -> None:
        raise helpers.ServerAuthError(401)

    with patch.object(advanced_cmd, "server_get", raise_auth):
        auth_result = runner.invoke(advanced_cmd.recap, [])

    def return_none(path: str, **kwargs: object) -> None:
        return None

    with patch.object(advanced_cmd, "server_get", return_none):
        unreachable_result = runner.invoke(advanced_cmd.recap, [])

    assert auth_result.exit_code != 0
    assert unreachable_result.exit_code != 0
    assert "credential" in auth_result.output.lower() or "rejected" in auth_result.output.lower()
    assert "credential" not in unreachable_result.output.lower()
