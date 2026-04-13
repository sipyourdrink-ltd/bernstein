"""Unit tests for OpenCodeAdapter."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from bernstein.core.models import ApiTier, ModelConfig, ProviderType

from bernstein.adapters.opencode import OpenCodeAdapter

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _make_popen_mock(pid: int) -> MagicMock:
    mock = MagicMock(spec=subprocess.Popen)
    mock.pid = pid
    mock.wait.return_value = None
    return mock


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


def test_spawn_builds_run_command(tmp_path: Path) -> None:
    adapter = OpenCodeAdapter()
    proc_mock = _make_popen_mock(100)

    with patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="openai/gpt-5.4-mini", effort="high"),
            session_id="oc-s1",
        )

    cmd = popen.call_args.args[0]
    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-m", "bernstein.core.worker"]
    inner = _inner_cmd(cmd)
    assert inner[:5] == ["opencode", "run", "-m", "openai/gpt-5.4-mini", "--format"]
    assert inner[5] == "json"
    assert inner[-1] == "fix the bug"


def test_spawn_warns_when_auth_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    adapter = OpenCodeAdapter()
    proc_mock = _make_popen_mock(101)

    with (
        patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock),
        patch("bernstein.adapters.opencode._OPENCODE_AUTH_FILE", tmp_path / "missing-auth.json"),
        patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
        caplog.at_level("WARNING"),
    ):
        adapter.spawn(
            prompt="hello",
            workdir=tmp_path,
            model_config=ModelConfig(model="openai/gpt-5.4-mini", effort="high"),
            session_id="oc-s2",
        )

    assert "no OpenCode/provider auth detected" in caplog.text


def test_detect_tier_none_without_auth(tmp_path: Path) -> None:
    adapter = OpenCodeAdapter()
    with (
        patch("bernstein.adapters.opencode._OPENCODE_AUTH_FILE", tmp_path / "missing-auth.json"),
        patch.dict("os.environ", {}, clear=True),
    ):
        assert adapter.detect_tier() is None


def test_detect_tier_with_auth_file(tmp_path: Path) -> None:
    adapter = OpenCodeAdapter()
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    with patch("bernstein.adapters.opencode._OPENCODE_AUTH_FILE", auth_file):
        info = adapter.detect_tier()

    assert info is not None
    assert info.tier == ApiTier.PRO
    assert info.provider == ProviderType.OPENCODE
