"""Unit tests for DevinTerminalAdapter (Cognition).

Mirrors the contract used by ``test_adapter_codex.py`` /
``test_adapter_droid.py``: assert command construction, env isolation,
missing-binary handling, and the inherited ``is_alive`` / ``kill``
plumbing without ever spawning a real subprocess.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.devin_terminal import DevinTerminalAdapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


# ---------------------------------------------------------------------------
# DevinTerminalAdapter.spawn() - command construction
# ---------------------------------------------------------------------------


class TestDevinTerminalSpawn:
    """spawn() builds the documented ``devin --print`` invocation."""

    def test_is_subclass_of_cli_adapter(self) -> None:
        assert issubclass(DevinTerminalAdapter, CLIAdapter)
        assert isinstance(DevinTerminalAdapter(), CLIAdapter)

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(800)
        with patch(
            "bernstein.adapters.devin_terminal.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-s1",
            )
        cmd = popen.call_args.args[0]
        inner = inner_cmd(cmd)
        assert inner[0] == "devin"

    def test_print_flag_present(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(801)
        with patch(
            "bernstein.adapters.devin_terminal.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-s2",
            )
        inner = inner_cmd(popen.call_args.args[0])
        # ``--print`` is the documented non-interactive flag - without it
        # devin opens a TTY session and never returns.
        assert "--print" in inner

    def test_permission_mode_bypass(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(802)
        with patch(
            "bernstein.adapters.devin_terminal.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-s3",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--permission-mode" in inner
        assert inner[inner.index("--permission-mode") + 1] == "bypass"

    def test_model_flag_passthrough(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(803)
        with patch(
            "bernstein.adapters.devin_terminal.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hi",
                workdir=tmp_path,
                model_config=ModelConfig(model="opus", effort="high"),
                session_id="devin-s4",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--model" in inner
        assert inner[inner.index("--model") + 1] == "opus"

    def test_blank_model_omits_flag(self, tmp_path: Path) -> None:
        """Empty ``model`` must not produce a bare ``--model`` flag."""
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(804)
        with patch(
            "bernstein.adapters.devin_terminal.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hi",
                workdir=tmp_path,
                model_config=ModelConfig(model="", effort="high"),
                session_id="devin-s5",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--model" not in inner

    def test_prompt_appended_last(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(805)
        with patch(
            "bernstein.adapters.devin_terminal.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="my-unique-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-s6",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner[-1] == "my-unique-prompt"

    def test_system_addendum_appended_to_prompt(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(806)
        with patch(
            "bernstein.adapters.devin_terminal.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="solve x",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-s7",
                system_addendum="POST done to /complete",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "solve x" in inner[-1]
        assert "POST done to /complete" in inner[-1]

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(807)
        with patch(
            "bernstein.adapters.devin_terminal.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-s8",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid_and_log_path(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(808)
        with patch(
            "bernstein.adapters.devin_terminal.subprocess.Popen",
            return_value=proc_mock,
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="my-devin-session",
            )
        assert result.pid == 808
        assert result.log_path.name == "my-devin-session.log"

    def test_start_new_session_enabled(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(809)
        with patch(
            "bernstein.adapters.devin_terminal.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-s9",
            )
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# spawn() - env isolation
# ---------------------------------------------------------------------------


class TestDevinTerminalEnvIsolation:
    """spawn() forwards only Devin-specific keys to the subprocess."""

    def test_env_contains_devin_api_key(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(900)
        with (
            patch(
                "bernstein.adapters.devin_terminal.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {"DEVIN_API_KEY": "apk_test", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("DEVIN_API_KEY") == "apk_test"

    def test_env_contains_devin_org_id(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(901)
        with (
            patch(
                "bernstein.adapters.devin_terminal.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {
                    "DEVIN_API_KEY": "apk_test",
                    "DEVIN_ORG_ID": "cog_org_42",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("DEVIN_ORG_ID") == "cog_org_42"

    def test_env_contains_windsurf_api_key(self, tmp_path: Path) -> None:
        """Windsurf-bundled distribution exposes credentials under
        ``WINDSURF_API_KEY``; the adapter must forward it."""
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(902)
        with (
            patch(
                "bernstein.adapters.devin_terminal.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {"WINDSURF_API_KEY": "ws-secret", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-env3",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("WINDSURF_API_KEY") == "ws-secret"

    def test_env_excludes_unrelated_keys(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(903)
        with (
            patch(
                "bernstein.adapters.devin_terminal.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {
                    "DEVIN_API_KEY": "apk_test",
                    "ANTHROPIC_API_KEY": "ant-secret",
                    "DATABASE_URL": "postgres://x",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-env4",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "ANTHROPIC_API_KEY" not in env
        assert "DATABASE_URL" not in env

    def test_env_includes_path(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(904)
        with (
            patch(
                "bernstein.adapters.devin_terminal.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {"PATH": "/usr/bin", "DEVIN_API_KEY": "apk_x"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-env5",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "PATH" in env

    def test_warns_when_credentials_missing(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(905)
        with (
            patch(
                "bernstein.adapters.devin_terminal.subprocess.Popen",
                return_value=proc_mock,
            ),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            caplog.at_level("WARNING"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-warn",
            )
        assert "DEVIN_API_KEY" in caplog.text
        assert "WINDSURF_API_KEY" in caplog.text


# ---------------------------------------------------------------------------
# DevinTerminalAdapter.name()
# ---------------------------------------------------------------------------


class TestDevinTerminalName:
    def test_name(self) -> None:
        assert DevinTerminalAdapter().name() == "devin_terminal"


# ---------------------------------------------------------------------------
# Missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestDevinTerminalMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        with (
            patch(
                "bernstein.adapters.devin_terminal.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError) as excinfo,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-missing",
            )
        message = str(excinfo.value)
        assert "devin not found" in message
        assert "cli.devin.ai/install.sh" in message

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        with (
            patch(
                "bernstein.adapters.devin_terminal.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-perm",
            )


# ---------------------------------------------------------------------------
# Network policy: declared external endpoints
# ---------------------------------------------------------------------------


class TestDevinTerminalEndpoints:
    def test_declares_devin_api_endpoint(self) -> None:
        endpoints = DevinTerminalAdapter.external_endpoints
        hosts = {host for host, _ in endpoints}
        assert "api.devin.ai" in hosts

    def test_endpoints_use_https_port(self) -> None:
        for _, port in DevinTerminalAdapter.external_endpoints:
            assert port == 443


# ---------------------------------------------------------------------------
# is_alive() / kill() - inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestDevinTerminalIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = DevinTerminalAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=True) as mock_alive:
            assert adapter.is_alive(1234) is True
        mock_alive.assert_called_once_with(1234)

    def test_false_when_process_dead(self) -> None:
        adapter = DevinTerminalAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=False):
            assert adapter.is_alive(9999) is False


class TestDevinTerminalKill:
    def test_calls_killpg(self) -> None:
        adapter = DevinTerminalAdapter()
        with patch("bernstein.adapters.base.reap_process_group") as mock_killpg:
            adapter.kill(555)
        mock_killpg.assert_called_once_with(555)

    def test_does_not_raise_on_oserror(self) -> None:
        adapter = DevinTerminalAdapter()
        with patch("bernstein.adapters.base.reap_process_group", return_value=False):
            adapter.kill(556)  # must not raise


# ---------------------------------------------------------------------------
# Fast-exit probe - early non-zero exit surfaces as SpawnError
# ---------------------------------------------------------------------------


class TestDevinTerminalFastExit:
    def test_fast_exit_non_zero_raises(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(910)
        proc_mock.wait.return_value = 1
        with (
            patch(
                "bernstein.adapters.devin_terminal.subprocess.Popen",
                return_value=proc_mock,
            ),
            patch.object(
                DevinTerminalAdapter,
                "_read_last_lines",
                return_value=["fatal: bad request"],
            ),
            pytest.raises(RuntimeError) as excinfo,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-fast-exit",
            )
        # SpawnError is a RuntimeError subclass; default tail surfaces.
        assert "exited early" in str(excinfo.value)

    def test_fast_exit_rate_limit_raises_rate_limit_error(self, tmp_path: Path) -> None:
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(911)
        proc_mock.wait.return_value = 1
        with (
            patch(
                "bernstein.adapters.devin_terminal.subprocess.Popen",
                return_value=proc_mock,
            ),
            patch.object(
                DevinTerminalAdapter,
                "_read_last_lines",
                return_value=["429 rate limit exceeded"],
            ),
            pytest.raises(RuntimeError, match="rate-limited"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-rate-limit",
            )

    def test_fast_exit_clean_does_not_raise(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Exit code 0 from the probe must let spawn() return cleanly."""
        adapter = DevinTerminalAdapter()
        proc_mock = make_popen_mock(912)
        proc_mock.wait.return_value = 0
        with patch(
            "bernstein.adapters.devin_terminal.subprocess.Popen",
            return_value=proc_mock,
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="devin-clean",
            )
        assert result.pid == 912


# ---------------------------------------------------------------------------
# detect_tier() - base default returns None for this adapter.
# ---------------------------------------------------------------------------


class TestDevinTerminalDetectTier:
    def test_default_returns_none(self) -> None:
        # Cognition does not yet expose a tier-discovery endpoint, and
        # ``ProviderType`` lacks a Devin entry. The adapter therefore
        # opts out of tier detection until the orchestrator gains a
        # provider enum and a documented tier API.
        assert DevinTerminalAdapter().detect_tier() is None
