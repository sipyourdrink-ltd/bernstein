"""Focused tests for preflight checks."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.preflight import _check_api_key, _check_binary, _check_port_free, preflight_checks


def test_check_binary_exits_with_actionable_error_when_missing() -> None:
    """_check_binary raises SystemExit when the requested CLI binary is not in PATH."""
    error = MagicMock()

    with (
        patch("bernstein.core.orchestration.preflight.shutil.which", return_value=None),
        patch("bernstein.cli.errors.BernsteinError", return_value=error),
        pytest.raises(SystemExit),
    ):
        _check_binary("codex")

    error.print.assert_called_once()


def test_check_api_key_accepts_codex_login_without_env_key() -> None:
    """_check_api_key accepts Codex when CLI login is already active."""
    with (
        patch.dict(os.environ, {}, clear=True),
        patch("bernstein.core.orchestration.preflight._codex_has_auth", return_value=(True, "ChatGPT login")),
    ):
        _check_api_key("codex")


def test_check_api_key_exits_for_qwen_without_supported_keys() -> None:
    """_check_api_key raises SystemExit when Qwen has none of its supported API keys configured."""
    error = MagicMock()

    with (
        patch.dict(os.environ, {}, clear=True),
        patch("bernstein.cli.errors.BernsteinError", return_value=error),
        pytest.raises(SystemExit),
    ):
        _check_api_key("qwen")

    error.print.assert_called_once()


def test_check_port_free_exits_when_bind_fails() -> None:
    """_check_port_free raises SystemExit when the chosen TCP port is already occupied."""
    fake_socket = MagicMock()
    fake_socket.__enter__.return_value = fake_socket
    fake_socket.bind.side_effect = OSError("in use")
    error = MagicMock()

    with (
        patch("bernstein.core.orchestration.preflight.socket.socket", return_value=fake_socket),
        patch("bernstein.cli.errors.port_in_use", return_value=error),
        pytest.raises(SystemExit),
    ):
        _check_port_free(8052)

    error.print.assert_called_once()


def test_preflight_checks_auto_mode_fails_when_no_agents_are_installed() -> None:
    """preflight_checks(auto) exits with SystemExit when discovery finds no available CLI agents."""
    discovery = SimpleNamespace(agents=[], warnings=[])

    with (
        patch("bernstein.core.orchestration.preflight._check_port_free"),
        patch("bernstein.core.agent_discovery.discover_agents_cached", return_value=discovery),
        pytest.raises(SystemExit),
    ):
        preflight_checks("auto", 8052)
