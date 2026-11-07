"""Cloudflare Access service-token headers on server-facing requests.

A central published through a Cloudflare tunnel behind an Access application
rejects at the edge any request without ``CF-Access-Client-Id`` and
``CF-Access-Client-Secret``. The worker's own requests, the CLI calls agents
make back to the server, and the agent env allowlist must all carry them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.adapters.env_isolation import build_filtered_env
from bernstein.cli import helpers
from bernstein.cli.commands.worker_cmd import WorkerLoop

if TYPE_CHECKING:
    from pathlib import Path

_ID = "service-token-id.access"
_SECRET = "service-token-secret-value"  # NOSONAR - test fixture, not a real credential


@pytest.fixture
def no_access_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.delenv("CF_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("CF_ACCESS_CLIENT_SECRET", raising=False)
    return monkeypatch


def test_access_headers_sent_when_both_vars_set(no_access_env: pytest.MonkeyPatch) -> None:
    no_access_env.setenv("CF_ACCESS_CLIENT_ID", _ID)
    no_access_env.setenv("CF_ACCESS_CLIENT_SECRET", _SECRET)
    assert helpers.access_proxy_headers() == {
        "CF-Access-Client-Id": _ID,
        "CF-Access-Client-Secret": _SECRET,
    }


@pytest.mark.parametrize("only", ["CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"])
def test_half_configured_access_sends_nothing(no_access_env: pytest.MonkeyPatch, only: str) -> None:
    no_access_env.setenv(only, "value")
    assert helpers.access_proxy_headers() == {}


def test_cli_auth_headers_carry_access_and_bearer(tmp_path: Path, no_access_env: pytest.MonkeyPatch) -> None:
    no_access_env.chdir(tmp_path)
    no_access_env.setenv("BERNSTEIN_AUTH_TOKEN", "env-token")
    no_access_env.setenv("CF_ACCESS_CLIENT_ID", _ID)
    no_access_env.setenv("CF_ACCESS_CLIENT_SECRET", _SECRET)
    assert helpers.auth_headers() == {
        "CF-Access-Client-Id": _ID,
        "CF-Access-Client-Secret": _SECRET,
        "Authorization": "Bearer env-token",
    }


def test_cli_auth_headers_unchanged_without_access(tmp_path: Path, no_access_env: pytest.MonkeyPatch) -> None:
    no_access_env.chdir(tmp_path)
    no_access_env.setenv("BERNSTEIN_AUTH_TOKEN", "env-token")
    assert helpers.auth_headers() == {"Authorization": "Bearer env-token"}


def test_worker_requests_carry_access_and_bearer(tmp_path: Path, no_access_env: pytest.MonkeyPatch) -> None:
    no_access_env.setenv("CF_ACCESS_CLIENT_ID", _ID)
    no_access_env.setenv("CF_ACCESS_CLIENT_SECRET", _SECRET)
    loop = WorkerLoop(server_url="https://central.example.com", auth_token="tok", adapter="qwen", workdir=tmp_path)
    assert loop._headers() == {
        "Content-Type": "application/json",
        "CF-Access-Client-Id": _ID,
        "CF-Access-Client-Secret": _SECRET,
        "Authorization": "Bearer tok",
    }


def test_worker_requests_unchanged_without_access(tmp_path: Path, no_access_env: pytest.MonkeyPatch) -> None:
    loop = WorkerLoop(server_url="http://central:8052", auth_token="tok", adapter="qwen", workdir=tmp_path)
    assert loop._headers() == {"Content-Type": "application/json", "Authorization": "Bearer tok"}


def test_spawned_agents_inherit_access_vars() -> None:
    env = build_filtered_env(
        base_env={
            "CF_ACCESS_CLIENT_ID": _ID,
            "CF_ACCESS_CLIENT_SECRET": _SECRET,
            "AWS_SECRET_ACCESS_KEY": "not-for-agents",
        }
    )
    assert env["CF_ACCESS_CLIENT_ID"] == _ID
    assert env["CF_ACCESS_CLIENT_SECRET"] == _SECRET
    assert "AWS_SECRET_ACCESS_KEY" not in env
