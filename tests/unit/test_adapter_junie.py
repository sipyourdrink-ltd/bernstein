"""Unit tests for JunieAdapter (JetBrains Junie CLI).

Mirrors the contract used by ``test_adapter_devin_terminal.py`` /
``test_adapter_aider.py``: assert command construction, env isolation,
provider-aware endpoint declaration, missing-binary handling, and the
inherited ``is_alive`` / ``kill`` plumbing without ever spawning a
real subprocess.

Junie's CLI surface is still in beta - the assertions here pin the
``run --headless --model <id> --prompt-file <path>`` shape documented
at https://junie.jetbrains.com/ on 2026-05-06. If the public surface
drifts, update both the adapter constants and these expectations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.junie import JunieAdapter
from bernstein.core.tasks.models import ModelConfig
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


# ---------------------------------------------------------------------------
# JunieAdapter.spawn() - command construction
# ---------------------------------------------------------------------------


class TestJunieSpawn:
    """spawn() builds the documented ``junie run --headless`` invocation."""

    def test_is_subclass_of_cli_adapter(self) -> None:
        assert issubclass(JunieAdapter, CLIAdapter)
        assert isinstance(JunieAdapter(), CLIAdapter)

    def test_wrapped_with_worker(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(700)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-s1",
            )
        cmd = popen.call_args.args[0]
        inner = inner_cmd(cmd)
        assert inner[0] == "junie"

    def test_run_subcommand(self, tmp_path: Path) -> None:
        """First positional argument after ``junie`` must be ``run``."""
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(701)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-s2",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner[1] == "run"

    def test_headless_flag_present(self, tmp_path: Path) -> None:
        """``--headless`` is the documented non-interactive mode."""
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(702)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-s3",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--headless" in inner

    def test_model_flag_passthrough(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(703)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hi",
                workdir=tmp_path,
                model_config=ModelConfig(model="opus", effort="high"),
                session_id="junie-s4",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--model" in inner
        assert inner[inner.index("--model") + 1] == "opus"

    def test_blank_model_omits_flag(self, tmp_path: Path) -> None:
        """Empty ``model`` must not produce a bare ``--model`` flag."""
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(704)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hi",
                workdir=tmp_path,
                model_config=ModelConfig(model="", effort="high"),
                session_id="junie-s5",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--model" not in inner

    def test_prompt_file_flag_present(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(705)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="my-unique-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-s6",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "--prompt-file" in inner

    def test_prompt_file_written_to_runtime(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(706)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="fixture-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-s7",
            )
        prompt_path = tmp_path / ".sdd" / "runtime" / "junie-s7-prompt.txt"
        assert prompt_path.is_file()
        assert "fixture-prompt" in prompt_path.read_text(encoding="utf-8")

    def test_prompt_file_contains_system_addendum(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(707)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="solve x",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-s8",
                system_addendum="POST done to /complete",
            )
        prompt_path = tmp_path / ".sdd" / "runtime" / "junie-s8-prompt.txt"
        body = prompt_path.read_text(encoding="utf-8")
        assert "solve x" in body
        assert "POST done to /complete" in body

    def test_full_command_shape(self, tmp_path: Path) -> None:
        """Entire constructed argv matches the documented spec.

        Pins the spec from the open ticket (KF-B) so a CLI drift forces
        a single visible diff in this test before the adapter ships
        broken to users.
        """
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(708)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-shape",
            )
        inner = inner_cmd(popen.call_args.args[0])
        prompt_file = str(tmp_path / ".sdd" / "runtime" / "junie-shape-prompt.txt")
        assert inner == [
            "junie",
            "run",
            "--headless",
            "--model",
            "sonnet",
            "--prompt-file",
            prompt_file,
        ]

    def test_creates_log_dir(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(709)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-s9",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid_and_log_path(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(710)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="my-junie-session",
            )
        assert result.pid == 710
        assert result.log_path.name == "my-junie-session.log"

    def test_start_new_session_enabled(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(711)
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-s10",
            )
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# spawn() - env isolation (BYOK pattern: forward routed-provider key,
# always forward JUNIE_API_KEY, never leak master credentials).
# ---------------------------------------------------------------------------


class TestJunieEnvIsolation:
    """spawn() forwards Junie + routed-provider keys, drops everything else."""

    def test_env_contains_junie_api_key(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(800)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {"JUNIE_API_KEY": "junie-test", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("JUNIE_API_KEY") == "junie-test"

    def test_env_contains_junie_provider(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(801)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {
                    "JUNIE_API_KEY": "j-test",
                    "JUNIE_PROVIDER": "anthropic",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("JUNIE_PROVIDER") == "anthropic"

    def test_env_forwards_routed_provider_key_anthropic(self, tmp_path: Path) -> None:
        """JUNIE_PROVIDER=anthropic ⇒ ANTHROPIC_API_KEY is forwarded."""
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(802)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {
                    "JUNIE_PROVIDER": "anthropic",
                    "ANTHROPIC_API_KEY": "ant-test",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-env3",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("ANTHROPIC_API_KEY") == "ant-test"

    def test_env_forwards_routed_provider_key_openai(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(803)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {
                    "JUNIE_PROVIDER": "openai",
                    "OPENAI_API_KEY": "sk-test",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.5", effort="high"),
                session_id="junie-env4",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("OPENAI_API_KEY") == "sk-test"

    def test_env_filters_master_credentials(self, tmp_path: Path) -> None:
        """Master / unrelated keys must never reach the Junie subprocess."""
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(804)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {
                    "JUNIE_API_KEY": "junie-test",
                    "JUNIE_PROVIDER": "openai",
                    "OPENAI_API_KEY": "sk-test",
                    # Master / unrelated secrets that must be filtered.
                    "OPENAI_MASTER_KEY": "sk-master-DO-NOT-LEAK",
                    "DATABASE_URL": "postgres://x",
                    "AWS_SECRET_ACCESS_KEY": "aws-secret",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.5", effort="high"),
                session_id="junie-env5",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "OPENAI_MASTER_KEY" not in env
        assert "DATABASE_URL" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        # Sanity: forwarding still works.
        assert env.get("OPENAI_API_KEY") == "sk-test"

    def test_env_excludes_unrouted_provider_key(self, tmp_path: Path) -> None:
        """JUNIE_PROVIDER=openai must NOT forward ANTHROPIC_API_KEY."""
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(805)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {
                    "JUNIE_PROVIDER": "openai",
                    "OPENAI_API_KEY": "sk-test",
                    "ANTHROPIC_API_KEY": "ant-not-routed",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.5", effort="high"),
                session_id="junie-env6",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "ANTHROPIC_API_KEY" not in env

    def test_env_includes_path(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(806)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ) as popen,
            patch.dict(
                "os.environ",
                {"PATH": "/usr/bin", "JUNIE_API_KEY": "j-x"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-env7",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "PATH" in env

    def test_warns_when_credentials_missing(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(807)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            caplog.at_level("WARNING"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-warn",
            )
        assert "JUNIE_API_KEY" in caplog.text


# ---------------------------------------------------------------------------
# JunieAdapter.name()
# ---------------------------------------------------------------------------


class TestJunieName:
    def test_name(self) -> None:
        assert JunieAdapter().name() == "junie"


# ---------------------------------------------------------------------------
# Missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestJunieMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError) as excinfo,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-missing",
            )
        message = str(excinfo.value)
        assert "junie not found" in message
        assert "junie.jetbrains.com/install.sh" in message

    def test_permission_error_raises_runtime_error(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-perm",
            )


# ---------------------------------------------------------------------------
# Network policy: external endpoints depend on routed provider
# ---------------------------------------------------------------------------


class TestJunieEndpoints:
    """external_endpoints is populated dynamically per-spawn from the provider."""

    def test_class_default_endpoints_empty(self) -> None:
        """Static endpoints stay empty so per-spawn resolution is authoritative."""
        assert JunieAdapter.external_endpoints == ()

    def test_endpoints_populated_for_anthropic_provider(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(900)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ),
            patch.dict(
                "os.environ",
                {"JUNIE_PROVIDER": "anthropic", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-ep1",
            )
        hosts = {host for host, _ in adapter.external_endpoints}
        assert "api.anthropic.com" in hosts

    def test_endpoints_populated_for_openai_provider(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(901)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ),
            patch.dict(
                "os.environ",
                {"JUNIE_PROVIDER": "openai", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.5", effort="high"),
                session_id="junie-ep2",
            )
        hosts = {host for host, _ in adapter.external_endpoints}
        assert "api.openai.com" in hosts

    def test_endpoints_https_only(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(902)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ),
            patch.dict(
                "os.environ",
                {"JUNIE_PROVIDER": "openrouter", "PATH": "/usr/bin"},
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-5.5", effort="high"),
                session_id="junie-ep3",
            )
        for _, port in adapter.external_endpoints:
            assert port == 443

    def test_endpoints_empty_when_provider_unknown(self, tmp_path: Path) -> None:
        """Unknown provider falls back to the empty allow-list (default policy)."""
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(903)
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-ep4",
            )
        assert adapter.external_endpoints == ()


# ---------------------------------------------------------------------------
# is_alive() / kill() - inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestJunieIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = JunieAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=True) as mock_alive:
            assert adapter.is_alive(1234) is True
        mock_alive.assert_called_once_with(1234)

    def test_false_when_process_dead(self) -> None:
        adapter = JunieAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=False):
            assert adapter.is_alive(9999) is False


class TestJunieKill:
    def test_calls_killpg(self) -> None:
        adapter = JunieAdapter()
        with patch("bernstein.adapters.base.reap_process_group") as mock_killpg:
            adapter.kill(555)
        mock_killpg.assert_called_once_with(555)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestJunieRegistry:
    def test_junie_in_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        adapter = get_adapter("junie")
        assert isinstance(adapter, JunieAdapter)

    def test_junie_name_via_registry(self) -> None:
        from bernstein.adapters.registry import get_adapter

        assert get_adapter("junie").name() == "junie"


# ---------------------------------------------------------------------------
# Fast-exit probe - early non-zero exit surfaces as SpawnError
# ---------------------------------------------------------------------------


class TestJunieFastExit:
    def test_fast_exit_non_zero_raises(self, tmp_path: Path) -> None:
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(910)
        proc_mock.wait.return_value = 1
        with (
            patch(
                "bernstein.adapters.junie.subprocess.Popen",
                return_value=proc_mock,
            ),
            patch.object(
                JunieAdapter,
                "_read_last_lines",
                return_value=["fatal: bad request"],
            ),
            pytest.raises(RuntimeError) as excinfo,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-fast-exit",
            )
        # SpawnError is a RuntimeError subclass; default tail surfaces.
        assert "exited early" in str(excinfo.value)

    def test_fast_exit_clean_does_not_raise(self, tmp_path: Path) -> None:
        """Exit code 0 from the probe must let spawn() return cleanly."""
        adapter = JunieAdapter()
        proc_mock = make_popen_mock(911)
        proc_mock.wait.return_value = 0
        with patch(
            "bernstein.adapters.junie.subprocess.Popen",
            return_value=proc_mock,
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="junie-clean",
            )
        assert result.pid == 911


# ---------------------------------------------------------------------------
# detect_tier() - base default returns None for this adapter.
# ---------------------------------------------------------------------------


class TestJunieDetectTier:
    def test_default_returns_none(self) -> None:
        # JetBrains does not expose a tier-discovery endpoint. Bernstein's
        # ``ProviderType`` enum has no entry for Junie either, so the
        # adapter opts out of tier detection until both surfaces exist.
        assert JunieAdapter().detect_tier() is None
