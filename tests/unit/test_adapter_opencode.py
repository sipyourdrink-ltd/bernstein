"""Unit tests for OpenCodeAdapter."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from bernstein.core.models import ApiTier, ModelConfig, ProviderType

from bernstein.adapters._contract import AdapterStrategy, DangerousModeStrategy, ResumeStrategy
from bernstein.adapters.opencode import (
    _ESCALATED_PERMISSION,
    _ESCALATED_PERMISSION_FLAG,
    _RESTRICTED_PERMISSION,
    OpenCodeAdapter,
)

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
    assert cmd[1:3] == ["-m", "bernstein.core.orchestration.worker"]
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


class TestPermissionPosture:
    """The spawn pins its own permission posture instead of inheriting the host's.

    Without this the worker runs under whatever the operator's personal
    ``opencode`` config says, so two operators running the same plan get
    different agent behaviour. The posture is derived from the adapter's
    declared dangerous-mode strategy, which is what makes that declaration
    load-bearing rather than descriptive.
    """

    def test_escalated_permission_flag_present_when_dangerous_mode_declared(self, tmp_path: Path) -> None:
        adapter = OpenCodeAdapter()
        proc_mock = _make_popen_mock(200)

        with patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="ship it",
                workdir=tmp_path,
                model_config=ModelConfig(model="openai/gpt-5.4-mini", effort="high"),
                session_id="oc-perm1",
            )

        inner = _inner_cmd(popen.call_args.args[0])
        assert _ESCALATED_PERMISSION_FLAG in inner

    def test_restricted_form_replaces_the_flag_when_dangerous_mode_is_off(self, tmp_path: Path) -> None:
        adapter = OpenCodeAdapter()
        adapter.strategy_override = AdapterStrategy(
            resume=ResumeStrategy.FLAG,
            dangerous_mode=DangerousModeStrategy.UNSUPPORTED,
        )
        proc_mock = _make_popen_mock(201)

        with patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="ship it",
                workdir=tmp_path,
                model_config=ModelConfig(model="openai/gpt-5.4-mini", effort="high"),
                session_id="oc-perm2",
            )

        inner = _inner_cmd(popen.call_args.args[0])
        assert _ESCALATED_PERMISSION_FLAG not in inner
        env = popen.call_args.kwargs["env"]
        assert json.loads(env["OPENCODE_PERMISSION"]) == _RESTRICTED_PERMISSION

    def test_permission_env_is_pinned_in_both_directions(self, tmp_path: Path) -> None:
        """Escalated spawns pin the env too, so config-file precedence cannot decide."""
        adapter = OpenCodeAdapter()
        proc_mock = _make_popen_mock(202)

        with patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="ship it",
                workdir=tmp_path,
                model_config=ModelConfig(model="openai/gpt-5.4-mini", effort="high"),
                session_id="oc-perm3",
            )

        env = popen.call_args.kwargs["env"]
        assert json.loads(env["OPENCODE_PERMISSION"]) == _ESCALATED_PERMISSION

    def test_host_permission_config_never_reaches_the_worker(self, tmp_path: Path) -> None:
        """An operator's own OPENCODE_PERMISSION must not survive into the spawn."""
        adapter = OpenCodeAdapter()
        proc_mock = _make_popen_mock(203)

        with (
            patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict("os.environ", {"PATH": "/usr/bin", "OPENCODE_PERMISSION": '{"bash": "ask"}'}),
        ):
            adapter.spawn(
                prompt="ship it",
                workdir=tmp_path,
                model_config=ModelConfig(model="openai/gpt-5.4-mini", effort="high"),
                session_id="oc-perm4",
            )

        env = popen.call_args.kwargs["env"]
        assert json.loads(env["OPENCODE_PERMISSION"]) == _ESCALATED_PERMISSION

    def test_no_permission_axis_resolves_to_ask(self) -> None:
        """``ask`` hangs a headless run forever (upstream #36762), so neither form uses it."""
        for posture in (_ESCALATED_PERMISSION, _RESTRICTED_PERMISSION):
            assert "ask" not in posture.values()


def test_contract_pins_the_flags_the_adapter_now_depends_on() -> None:
    """An upstream rename of the permission or resume surface fails here first."""
    from bernstein.adapters._contract import ContractSpec

    spec = ContractSpec.load("opencode")
    assert spec.binary == "opencode"
    assert _ESCALATED_PERMISSION_FLAG in spec.required_flags
    assert "--continue" in spec.required_flags


class TestSessionContinuation:
    """``resume=flag`` has to describe a flag the adapter actually passes."""

    def test_adapter_opts_into_the_continuation_path(self) -> None:
        assert OpenCodeAdapter.supports_session_continuation is True

    def test_continuation_args_reenter_the_prior_session(self) -> None:
        assert OpenCodeAdapter().continuation_args("oc-s1") == ["--continue"]

    def test_continuation_flag_lands_in_the_constructed_command(self) -> None:
        adapter = OpenCodeAdapter()
        cmd = adapter._build_command(
            model="openai/gpt-5.4-mini",
            prompt="carry on",
            continuation_args=adapter.continuation_args("oc-s1"),
        )

        assert "--continue" in cmd
        assert cmd[-1] == "carry on", "the prompt stays positional and last"

    def test_fresh_spawn_carries_no_continuation_flag(self, tmp_path: Path) -> None:
        adapter = OpenCodeAdapter()
        proc_mock = _make_popen_mock(204)

        with patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="start fresh",
                workdir=tmp_path,
                model_config=ModelConfig(model="openai/gpt-5.4-mini", effort="high"),
                session_id="oc-s3",
            )

        assert "--continue" not in _inner_cmd(popen.call_args.args[0])


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
