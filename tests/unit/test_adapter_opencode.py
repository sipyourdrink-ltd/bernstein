"""Unit tests for OpenCodeAdapter."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ApiTier, ModelConfig, ProviderType

from bernstein.adapters._contract import AdapterStrategy, DangerousModeStrategy, ResumeStrategy
from bernstein.adapters.base import SpawnError
from bernstein.adapters.opencode import (
    _ESCALATED_PERMISSION,
    _ESCALATED_PERMISSION_FLAG,
    _RESTRICTED_PERMISSION,
    OpenCodeAdapter,
    _qualify_model,
)

if TYPE_CHECKING:
    from pathlib import Path


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


def test_opencode_prompt_carries_the_completion_protocol(tmp_path: Path) -> None:
    """OpenCode has no system-prompt flag; a non-empty addendum must still
    reach the agent by riding on the positional prompt argument (issue
    #5325), or the completion / heartbeat / signal-check protocol never
    reaches a ``--cli opencode`` run.
    """
    adapter = OpenCodeAdapter()
    proc_mock = _make_popen_mock(109)

    with patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="fix the bug",
            workdir=tmp_path,
            model_config=ModelConfig(model="openai/gpt-5.4-mini", effort="high"),
            session_id="oc-s9",
            system_addendum="When done, POST /complete. Heartbeat every 30s.",
        )

    inner = _inner_cmd(popen.call_args.args[0])
    assert "When done, POST /complete. Heartbeat every 30s." in inner[-1]


def test_opencode_addendum_appended_after_task_brief(tmp_path: Path) -> None:
    """A truncated prompt must lose the addendum, never the task brief."""
    adapter = OpenCodeAdapter()
    proc_mock = _make_popen_mock(110)

    with patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="primary task brief",
            workdir=tmp_path,
            model_config=ModelConfig(model="openai/gpt-5.4-mini", effort="high"),
            session_id="oc-s10",
            system_addendum="HEARTBEAT every 30s",
        )

    inner = _inner_cmd(popen.call_args.args[0])
    full_prompt = inner[-1]
    assert full_prompt.index("primary task brief") < full_prompt.index("HEARTBEAT every 30s")


def test_opencode_empty_addendum_leaves_prompt_untouched(tmp_path: Path) -> None:
    adapter = OpenCodeAdapter()
    proc_mock = _make_popen_mock(111)

    with patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen:
        adapter.spawn(
            prompt="just the task",
            workdir=tmp_path,
            model_config=ModelConfig(model="openai/gpt-5.4-mini", effort="high"),
            session_id="oc-s11",
            system_addendum="",
        )

    inner = _inner_cmd(popen.call_args.args[0])
    assert inner[-1] == "just the task"


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


class TestModelIdQualification:
    """Bare model ids (no /) must be qualified from the opencode config or refused."""

    def test_qualified_id_passed_through_unchanged(self, tmp_path: Path) -> None:
        adapter = OpenCodeAdapter()
        proc_mock = _make_popen_mock(300)

        with patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="do it",
                workdir=tmp_path,
                model_config=ModelConfig(model="anthropic/claude-sonnet-4", effort="high"),
                session_id="oc-mq1",
            )

        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[3] == "anthropic/claude-sonnet-4"

    def test_bare_id_qualified_when_exactly_one_provider_matches(self, tmp_path: Path) -> None:
        adapter = OpenCodeAdapter()
        proc_mock = _make_popen_mock(301)

        cfg = tmp_path / "opencode.jsonc"
        cfg.write_text(
            """\
            // my config
            { "provider": { "anthropic": { "models": ["claude-sonnet-4"] } } }
            """
        )
        with (
            patch("bernstein.adapters.opencode._OPENCODE_CONFIG_FILE", cfg),
            patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="do it",
                workdir=tmp_path,
                model_config=ModelConfig(model="claude-sonnet-4", effort="high"),
                session_id="oc-mq2",
            )

        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[3] == "anthropic/claude-sonnet-4"

    def test_bare_id_refused_when_zero_providers_match(self, tmp_path: Path) -> None:
        cfg = tmp_path / "opencode.jsonc"
        cfg.write_text('{ "provider": { "openai": { "models": ["gpt-5"] } } }')

        with patch("bernstein.adapters.opencode._OPENCODE_CONFIG_FILE", cfg):
            with pytest.raises(Exception) as exc_info:
                _qualify_model("claude-sonnet-4")

            msg = str(exc_info.value)
            assert "claude-sonnet-4" in msg
            assert str(cfg) in msg
            assert "provider" in msg and "models" in msg

    def test_bare_id_refused_when_two_providers_match(self, tmp_path: Path) -> None:
        cfg = tmp_path / "opencode.jsonc"
        cfg.write_text(
            '{ "provider": { "provider-a": { "models": ["my-model"] }, "provider-b": { "models": ["my-model"] } } }'
        )

        with patch("bernstein.adapters.opencode._OPENCODE_CONFIG_FILE", cfg):
            with pytest.raises(Exception) as exc_info:
                _qualify_model("my-model")

            msg = str(exc_info.value)
            assert "my-model" in msg
            assert str(cfg) in msg

    def test_bare_id_refused_when_config_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-such-config.jsonc"
        assert not missing.exists()

        with patch("bernstein.adapters.opencode._OPENCODE_CONFIG_FILE", missing):
            with pytest.raises(Exception) as exc_info:
                _qualify_model("claude-sonnet-4")

            msg = str(exc_info.value)
            assert "claude-sonnet-4" in msg
            assert str(missing) in msg

    def test_bare_id_refused_when_config_unparseable(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.jsonc"
        bad.write_text("not valid json at all {")

        with patch("bernstein.adapters.opencode._OPENCODE_CONFIG_FILE", bad):
            with pytest.raises(Exception) as exc_info:
                _qualify_model("claude-sonnet-4")

            msg = str(exc_info.value)
            assert "claude-sonnet-4" in msg
            assert str(bad) in msg

    def test_spawn_refuses_bare_id_before_popen(self, tmp_path: Path) -> None:
        cfg = tmp_path / "opencode.jsonc"
        cfg.write_text('{ "provider": { "openai": { "models": ["gpt-5"] } } }')

        adapter = OpenCodeAdapter()
        with (
            patch("bernstein.adapters.opencode._OPENCODE_CONFIG_FILE", cfg),
            patch("bernstein.adapters.opencode.subprocess.Popen") as popen,
        ):
            with pytest.raises(SpawnError) as exc_info:
                adapter.spawn(
                    prompt="do it",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="claude-sonnet-4", effort="high"),
                    session_id="oc-mq3",
                )

            popen.assert_not_called()
            msg = str(exc_info.value)
            assert "claude-sonnet-4" in msg
            assert str(cfg) in msg

    def test_refusal_text_is_what_the_spawner_extracts(self, tmp_path: Path) -> None:
        """The spawner's _diagnose_spawn_failure falls back to str(exc) when the
        per-session log does not exist yet -- which is the case here, because the
        refusal raises before the adapter opens the log file. The refusal text
        must therefore survive into the extracted reason verbatim."""
        from bernstein.core.agents.spawner_core import _diagnose_spawn_failure

        cfg = tmp_path / "opencode.jsonc"
        cfg.write_text('{ "provider": { "openai": { "models": ["gpt-5"] } } }')

        adapter = OpenCodeAdapter()
        with patch("bernstein.adapters.opencode._OPENCODE_CONFIG_FILE", cfg):
            with pytest.raises(SpawnError) as exc_info:
                adapter.spawn(
                    prompt="do it",
                    workdir=tmp_path,
                    model_config=ModelConfig(model="claude-sonnet-4", effort="high"),
                    session_id="oc-mq7",
                )

        reason = _diagnose_spawn_failure("oc-mq7", tmp_path, "OpenCode", exc_info.value)
        assert "claude-sonnet-4" in reason
        assert str(cfg) in reason
        assert "provider" in reason and "models" in reason

    def test_env_var_opencode_config_honored(self, tmp_path: Path) -> None:
        cfg = tmp_path / "custom.jsonc"
        cfg.write_text('{ "provider": { "google": { "models": ["gemini-pro"] } } }')

        adapter = OpenCodeAdapter()
        proc_mock = _make_popen_mock(302)

        with (
            patch.dict("os.environ", {"OPENCODE_CONFIG": str(cfg)}),
            patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="do it",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-pro", effort="high"),
                session_id="oc-mq4",
            )

        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[3] == "google/gemini-pro"

    def test_env_var_opencode_config_dir_honored(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "oc-config"
        cfg_dir.mkdir()
        cfg = cfg_dir / "opencode.jsonc"
        cfg.write_text('{ "provider": { "mistral": { "models": ["mistral-large"] } } }')

        adapter = OpenCodeAdapter()
        proc_mock = _make_popen_mock(303)

        with (
            patch.dict("os.environ", {"OPENCODE_CONFIG_DIR": str(cfg_dir)}),
            patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="do it",
                workdir=tmp_path,
                model_config=ModelConfig(model="mistral-large", effort="high"),
                session_id="oc-mq5",
            )

        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[3] == "mistral/mistral-large"

    def test_models_as_dict_schema_accepted(self, tmp_path: Path) -> None:
        cfg = tmp_path / "opencode.jsonc"
        cfg.write_text('{ "provider": { "openai": { "models": { "gpt-5": { "temperature": 0.7 } } } } }')

        adapter = OpenCodeAdapter()
        proc_mock = _make_popen_mock(304)

        with (
            patch("bernstein.adapters.opencode._OPENCODE_CONFIG_FILE", cfg),
            patch("bernstein.adapters.opencode.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="do it",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5", effort="high"),
                session_id="oc-mq6",
            )

        inner = _inner_cmd(popen.call_args.args[0])
        assert inner[3] == "openai/gpt-5"

    def test_config_path_constant_can_be_patched_in_tests(self, tmp_path: Path) -> None:
        cfg = tmp_path / "opencode.jsonc"
        cfg.write_text('{ "provider": { "test": { "models": ["model-x"] } } }')

        from bernstein.adapters import opencode as oc_module

        original = oc_module._OPENCODE_CONFIG_FILE
        oc_module._OPENCODE_CONFIG_FILE = cfg
        try:
            qualified = oc_module._qualify_model("model-x")
            assert qualified == "test/model-x"
        finally:
            oc_module._OPENCODE_CONFIG_FILE = original
