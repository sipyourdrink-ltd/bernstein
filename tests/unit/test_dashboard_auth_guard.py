"""Regression tests for `bernstein dashboard` on an auth-gated server (#2794).

A detached run gates every non-public route behind an auto-generated Bearer
token that a plain browser navigation cannot supply, so ``GET /dashboard``
answers ``401`` and the printed URL used to dead-end on raw JSON. The command
must probe the dashboard as a browser would and, when it is auth-gated, guide
the operator to a working surface instead of launching a broken page.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
from click.testing import CliRunner

from bernstein.cli.commands import advanced_cmd


def _response(status_code: int) -> httpx.Response:
    request = httpx.Request("GET", "http://localhost:8052/probe")
    return httpx.Response(status_code, request=request, json={"detail": "x"})


def _fake_get(dashboard_status: int):
    def _get(url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/health"):
            return _response(200)
        if url.endswith("/dashboard"):
            return _response(dashboard_status)
        return _response(200)

    return _get


def test_dashboard_401_does_not_open_broken_page() -> None:
    """An auth-gated /dashboard is not launched; the operator is guided instead."""
    runner = CliRunner()
    with (
        patch.object(advanced_cmd.httpx, "get", _fake_get(401)),
        patch("webbrowser.open") as wb_open,
    ):
        result = runner.invoke(advanced_cmd.dashboard, [])

    assert result.exit_code == 1
    wb_open.assert_not_called()
    out = result.output.lower()
    assert "auth" in out or "credential" in out or "token" in out
    # Points at a surface that actually authenticates.
    assert "bernstein live" in result.output or "bernstein status" in result.output
    # No raw 401 JSON body echoed to the operator.
    assert '{"detail"' not in result.output


def test_dashboard_open_when_not_auth_gated() -> None:
    """When /dashboard is reachable anonymously the browser is opened as before."""
    runner = CliRunner()
    with (
        patch.object(advanced_cmd.httpx, "get", _fake_get(200)),
        patch("webbrowser.open") as wb_open,
    ):
        result = runner.invoke(advanced_cmd.dashboard, [])

    assert result.exit_code == 0
    wb_open.assert_called_once()


def test_dashboard_no_open_flag_still_guards_401() -> None:
    """--no-open must not suppress the auth guidance on an auth-gated server."""
    runner = CliRunner()
    with (
        patch.object(advanced_cmd.httpx, "get", _fake_get(401)),
        patch("webbrowser.open") as wb_open,
    ):
        result = runner.invoke(advanced_cmd.dashboard, ["--no-open"])

    assert result.exit_code == 1
    wb_open.assert_not_called()
