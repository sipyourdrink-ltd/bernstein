"""Unit tests for GeminiAdapter spawn/kill/is_alive.

Every spawn test is parametrised over both supported binary names
(``antigravity`` and ``gemini``) so the suite proves the adapter's
discovery cascade works against either binary. A dedicated
``TestBinaryDiscoveryCascade`` block exercises the cascade itself:
both binaries present, only legacy, neither (raises
:class:`BinaryNotInstalledError`), and the operator override.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import ApiTier, ModelConfig, ProviderType

from bernstein.adapters.gemini import (
    ANTIGRAVITY_BINARY,
    BINARY_ENV_VAR,
    LEGACY_GEMINI_BINARY,
    BinaryNotInstalledError,
    GeminiAdapter,
    resolve_google_cli_binary,
)

if TYPE_CHECKING:
    from pathlib import Path

# Every spawn() call below arms a watchdog Timer thread by default. Under
# the isolated test runner's high process concurrency the OS thread ceiling
# can be hit, surfacing as "RuntimeError: can't start new thread" on an
# otherwise-trivial test. Disable the watchdog suite-wide, matching the
# other adapter test modules (rovo, auggie, clm, ralphex).
pytestmark = pytest.mark.usefixtures("no_watchdog_threads")  # suite-wide guard, see module docstring

# Parametrisation surface: both supported binary names. Every spawn-side
# test runs against both so a regression in either path is caught.
ALL_BINARIES = (ANTIGRAVITY_BINARY, LEGACY_GEMINI_BINARY)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.wait.return_value = None
    return m


def _inner_cmd(full_cmd: list[str]) -> list[str]:
    """Extract the actual CLI command after the '--' worker separator."""
    sep = full_cmd.index("--")
    return full_cmd[sep + 1 :]


def _which_only(binary: str) -> object:
    """Return a ``shutil.which`` stub that resolves only ``binary``."""

    def stub(name: str) -> str | None:
        return f"/usr/local/bin/{name}" if name == binary else None

    return stub


# ---------------------------------------------------------------------------
# GeminiAdapter.spawn() - command construction, parametrised on binary name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("binary", ALL_BINARIES)
class TestGeminiAdapterSpawn:
    """GeminiAdapter.spawn() builds correct command on each supported binary."""

    def test_wrapped_with_worker(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=100)
        with (
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="gemini-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.orchestration.worker"]
        inner = _inner_cmd(cmd)
        assert inner[0] == binary

    def test_model_flag_passthrough(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=101)
        with (
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3-flash", effort="high"),
                session_id="gemini-s2",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "-m" in inner
        assert inner[inner.index("-m") + 1] == "gemini-3-flash"

    def test_output_format_json_flag(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=102)
        with (
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="gemini-s3",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--output-format" in inner
        assert inner[inner.index("--output-format") + 1] == "json"

    def test_prompt_flag_used(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=103)
        with (
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="my-unique-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="gemini-s4",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "-p" in inner
        assert inner[inner.index("-p") + 1] == "my-unique-prompt"

    def test_yolo_flag_present(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=108)
        with (
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="gemini-s8",
            )
        inner = _inner_cmd(popen.call_args.args[0])
        assert "--yolo" in inner

    def test_creates_log_dir(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=104)
        with (
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="gemini-s5",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_spawn_result_pid(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=105)
        with (
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock),
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="gemini-s6",
            )
        assert result.pid == 105

    def test_log_path_uses_session_id(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=106)
        with (
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock),
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="my-gemini-session",
            )
        assert result.log_path.name == "my-gemini-session.log"

    def test_start_new_session_enabled(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=107)
        with (
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="gemini-s7",
            )
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# spawn() - env isolation, parametrised on binary name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("binary", ALL_BINARIES)
class TestGeminiEnvIsolation:
    """spawn() passes only Google-specific keys to subprocess."""

    def test_env_contains_google_keys(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=200)
        with (
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {"GOOGLE_API_KEY": "AIza-test", "GOOGLE_CLOUD_PROJECT": "my-proj", "PATH": "/usr/bin"},
                clear=True,
            ),
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="gemini-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "GOOGLE_API_KEY" in env
        assert env["GOOGLE_API_KEY"] == "AIza-test"

    def test_env_excludes_unrelated_keys(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=201)
        with (
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {
                    "GOOGLE_API_KEY": "AIza-test",
                    "ANTHROPIC_API_KEY": "ant-secret",
                    "OPENAI_API_KEY": "sk-secret",
                    "DATABASE_URL": "postgres://x",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="gemini-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "DATABASE_URL" not in env

    def test_env_includes_path(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=202)
        with (
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict("os.environ", {"PATH": "/usr/bin", "GOOGLE_API_KEY": "AIza-x"}, clear=True),
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="gemini-env3",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "PATH" in env


# ---------------------------------------------------------------------------
# GeminiAdapter.name()
# ---------------------------------------------------------------------------


class TestGeminiAdapterName:
    def test_name(self) -> None:
        assert GeminiAdapter().name() == "Gemini"


# ---------------------------------------------------------------------------
# Missing binary / PermissionError, parametrised on binary name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("binary", ALL_BINARIES)
class TestGeminiSpawnMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        with (
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            patch(
                "bernstein.adapters.gemini.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        with (
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            patch(
                "bernstein.adapters.gemini.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="perm-denied",
            )


@pytest.mark.parametrize("binary", ALL_BINARIES)
class TestGeminiWarnings:
    def test_logs_debug_when_no_api_key_present(
        self,
        tmp_path: Path,
        binary: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=301)
        with (
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock),
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
            caplog.at_level("DEBUG"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="warn-missing-key",
            )
        assert "no GOOGLE_API_KEY/GEMINI_API_KEY set" in caplog.text

    def test_populates_gemini_api_key_from_google_key(self, tmp_path: Path, binary: str) -> None:
        adapter = GeminiAdapter()
        proc_mock = _make_popen_mock(pid=302)
        with (
            patch("bernstein.adapters.gemini.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict("os.environ", {"GOOGLE_API_KEY": "AIza-test", "PATH": "/usr/bin"}, clear=True),
            patch("bernstein.adapters.gemini.shutil.which", side_effect=_which_only(binary)),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gemini-3.1-pro", effort="high"),
                session_id="pop-key",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert env.get("GEMINI_API_KEY") == "AIza-test"
        assert env.get("GOOGLE_API_KEY") == "AIza-test"


# ---------------------------------------------------------------------------
# Binary discovery cascade (issue #1740)
# ---------------------------------------------------------------------------


class TestBinaryDiscoveryCascade:
    """Cover the four discovery outcomes the adapter contract promises."""

    def test_antigravity_wins_when_both_present(self) -> None:
        """When both binaries resolve, ``antigravity`` is preferred."""

        def both_present(name: str) -> str | None:
            return f"/usr/local/bin/{name}" if name in {ANTIGRAVITY_BINARY, LEGACY_GEMINI_BINARY} else None

        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            assert resolve_google_cli_binary(which=both_present) == ANTIGRAVITY_BINARY

    def test_legacy_wins_when_only_legacy_present(self) -> None:
        """When only the legacy binary resolves, the cascade falls back."""

        def only_legacy(name: str) -> str | None:
            return "/usr/local/bin/gemini" if name == LEGACY_GEMINI_BINARY else None

        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            assert resolve_google_cli_binary(which=only_legacy) == LEGACY_GEMINI_BINARY

    def test_neither_raises_binary_not_installed_in_strict_mode(self) -> None:
        """Strict mode (used by ``adapters check``) raises a typed error
        when neither cascade entry resolves. The spawn path uses the
        non-strict default and lets ``subprocess.Popen`` raise.
        """
        with (
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
            pytest.raises(BinaryNotInstalledError, match="antigravity"),
        ):
            resolve_google_cli_binary(which=lambda _name: None, strict=True)

    def test_neither_returns_default_in_non_strict_mode(self) -> None:
        """Non-strict mode returns the first cascade entry as fallback
        so ``subprocess.Popen`` raises the natural ``FileNotFoundError``
        instead of the resolver raising eagerly. Matches the codex /
        aider adapter posture.
        """
        with patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            assert resolve_google_cli_binary(which=lambda _name: None) == ANTIGRAVITY_BINARY

    def test_env_override_wins_over_cascade(self) -> None:
        """``BERNSTEIN_GEMINI_BINARY`` short-circuits the cascade."""

        def both_present(name: str) -> str | None:
            return f"/usr/local/bin/{name}" if name in {"antigravity", "gemini", "vendor-cli"} else None

        with patch.dict("os.environ", {"PATH": "/usr/bin", BINARY_ENV_VAR: "vendor-cli"}, clear=True):
            assert resolve_google_cli_binary(which=both_present) == "vendor-cli"

    def test_env_override_missing_binary_raises(self) -> None:
        """An override that does not resolve is a hard error regardless of strict."""
        with (
            patch.dict("os.environ", {"PATH": "/usr/bin", BINARY_ENV_VAR: "vendor-cli"}, clear=True),
            pytest.raises(BinaryNotInstalledError, match=BINARY_ENV_VAR),
        ):
            resolve_google_cli_binary(which=lambda _name: None, strict=False)

    def test_empty_env_override_falls_through_to_cascade(self) -> None:
        """A blank override behaves as if unset (no surprise pinning)."""

        def only_antigravity(name: str) -> str | None:
            return "/usr/local/bin/antigravity" if name == ANTIGRAVITY_BINARY else None

        with patch.dict("os.environ", {"PATH": "/usr/bin", BINARY_ENV_VAR: "   "}, clear=True):
            assert resolve_google_cli_binary(which=only_antigravity) == ANTIGRAVITY_BINARY


# ---------------------------------------------------------------------------
# is_alive() and kill() - inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestGeminiIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = GeminiAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=True) as mock_alive:
            assert adapter.is_alive(1234) is True
        mock_alive.assert_called_once_with(1234)

    def test_false_when_oserror(self) -> None:
        adapter = GeminiAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=False):
            assert adapter.is_alive(9999) is False


class TestGeminiKill:
    def test_calls_killpg(self) -> None:
        adapter = GeminiAdapter()
        with patch("bernstein.adapters.base.reap_process_group") as mock_killpg:
            adapter.kill(555)
        mock_killpg.assert_called_once_with(555)

    def test_does_not_raise_on_oserror(self) -> None:
        adapter = GeminiAdapter()
        with patch("bernstein.adapters.base.reap_process_group", return_value=False):
            adapter.kill(556)  # must not raise


# ---------------------------------------------------------------------------
# detect_tier()
# ---------------------------------------------------------------------------


class TestGeminiDetectTier:
    def test_returns_none_without_api_key(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict("os.environ", {}, clear=True):
            assert adapter.detect_tier() is None

    def test_enterprise_with_gcp_project(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict(
            "os.environ",
            {"GOOGLE_API_KEY": "AIza-test", "GOOGLE_CLOUD_PROJECT": "my-project"},
            clear=True,
        ):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.ENTERPRISE
        assert info.provider == ProviderType.GEMINI

    def test_pro_with_aiza_key(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict("os.environ", {"GOOGLE_API_KEY": "AIzaSyB-test-key"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.PRO

    def test_free_with_unknown_key_format(self) -> None:
        adapter = GeminiAdapter()
        with patch.dict("os.environ", {"GOOGLE_API_KEY": "random-key"}, clear=True):
            info = adapter.detect_tier()
        assert info is not None
        assert info.tier == ApiTier.FREE
