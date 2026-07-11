"""Unit tests for ``QDevAdapter`` (AWS Q Developer CLI).

Mirrors the contract used by ``test_adapter_kiro.py`` /
``test_adapter_devin_terminal.py``: assert command construction, env
isolation, login-cache pre-flight, and the inherited ``is_alive`` /
``kill`` plumbing without ever spawning a real subprocess.
"""

from __future__ import annotations

import platform
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.models import ModelConfig

from bernstein.adapters.base import CLIAdapter, SpawnError
from bernstein.adapters.q_dev import QDevAdapter
from bernstein.adapters.registry import get_adapter
from tests.unit._adapter_test_helpers import inner_cmd, make_popen_mock

if TYPE_CHECKING:
    from pathlib import Path


pytestmark = pytest.mark.usefixtures("no_watchdog_threads")


@pytest.fixture
def fake_q_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand up a fake ``q login`` cache so spawn() doesn't reject the run.

    Points ``Path.home`` at a temporary directory and pre-creates *both* the
    Linux/macOS layout (``~/.local/share/amazon-q``) and the Windows layout
    (``~/AppData/Local/amazon-q``) so the fixture is platform-agnostic -
    ``_has_q_login_cache()`` branches on ``platform.system()`` and we don't
    want the test outcome to depend on which OS the runner happens to be.
    Clears the XDG and Windows env hints so the cache lookup deterministically
    lands on the home-rooted candidate paths.
    """
    home = tmp_path / "home"
    (home / ".local" / "share" / "amazon-q").mkdir(parents=True)
    (home / "AppData" / "Local" / "amazon-q").mkdir(parents=True)
    monkeypatch.setattr("bernstein.adapters.q_dev.Path.home", lambda: home)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    return home


# ---------------------------------------------------------------------------
# QDevAdapter - registry / contract surface
# ---------------------------------------------------------------------------


class TestQDevRegistry:
    """Adapter is reachable through the public registry as ``q_dev``."""

    def test_registered_under_q_dev_slug(self) -> None:
        adapter = get_adapter("q_dev")
        assert isinstance(adapter, QDevAdapter)

    def test_subclasses_cli_adapter(self) -> None:
        assert issubclass(QDevAdapter, CLIAdapter)
        assert isinstance(QDevAdapter(), CLIAdapter)

    def test_name_returns_q_dev(self) -> None:
        assert QDevAdapter().name() == "q_dev"


# ---------------------------------------------------------------------------
# QDevAdapter.spawn() - command construction
# ---------------------------------------------------------------------------


class TestQDevSpawnCommand:
    """spawn() builds the documented ``q chat`` non-interactive invocation."""

    def test_wrapped_with_worker(self, tmp_path: Path, fake_q_login: Path) -> None:
        adapter = QDevAdapter()
        proc_mock = make_popen_mock(900)
        with patch("bernstein.adapters.q_dev.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-s1",
            )
        cmd = popen.call_args.args[0]
        assert cmd[0] == sys.executable
        assert cmd[1:3] == ["-m", "bernstein.core.orchestration.worker"]
        inner = inner_cmd(cmd)
        assert inner[0] == "q"
        assert inner[1] == "chat"

    def test_canonical_command_line(self, tmp_path: Path, fake_q_login: Path) -> None:
        """Lock the documented ``q chat --no-interactive --trust-all-tools <prompt>`` shape."""
        adapter = QDevAdapter()
        proc_mock = make_popen_mock(901)
        with patch("bernstein.adapters.q_dev.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="fix the bug",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-s2",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner == ["q", "chat", "--no-interactive", "--trust-all-tools", "fix the bug"]

    def test_prompt_appended_last(self, tmp_path: Path, fake_q_login: Path) -> None:
        adapter = QDevAdapter()
        proc_mock = make_popen_mock(902)
        with patch("bernstein.adapters.q_dev.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="my-unique-prompt",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-s3",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert inner[-1] == "my-unique-prompt"

    def test_system_addendum_grafted_onto_prompt(self, tmp_path: Path, fake_q_login: Path) -> None:
        """``q`` accepts a single positional - addendum must reach the agent."""
        adapter = QDevAdapter()
        proc_mock = make_popen_mock(903)
        with patch("bernstein.adapters.q_dev.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="primary task",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-s4",
                system_addendum="HEARTBEAT every 30s",
            )
        inner = inner_cmd(popen.call_args.args[0])
        assert "primary task" in inner[-1]
        assert "HEARTBEAT every 30s" in inner[-1]

    def test_creates_log_dir(self, tmp_path: Path, fake_q_login: Path) -> None:
        adapter = QDevAdapter()
        proc_mock = make_popen_mock(904)
        with patch("bernstein.adapters.q_dev.subprocess.Popen", return_value=proc_mock):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-s5",
            )
        assert (tmp_path / ".sdd" / "runtime").is_dir()

    def test_log_path_uses_session_id(self, tmp_path: Path, fake_q_login: Path) -> None:
        adapter = QDevAdapter()
        proc_mock = make_popen_mock(905)
        with patch("bernstein.adapters.q_dev.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="my-q-session",
            )
        assert result.log_path.name == "my-q-session.log"

    def test_start_new_session_enabled(self, tmp_path: Path, fake_q_login: Path) -> None:
        adapter = QDevAdapter()
        proc_mock = make_popen_mock(906)
        with patch("bernstein.adapters.q_dev.subprocess.Popen", return_value=proc_mock) as popen:
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-s6",
            )
        kwargs = popen.call_args.kwargs
        assert kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# QDevAdapter.spawn() - login cache pre-flight
# ---------------------------------------------------------------------------


class TestQDevLoginCachePreflight:
    """spawn() refuses to run when no ``q login`` cache exists.

    Letting the spawn through would cause q to deadlock on its OAuth
    browser handshake, with the failure buried inside the agent log.
    """

    def test_missing_cache_raises_spawn_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = QDevAdapter()
        empty_home = tmp_path / "fresh"
        empty_home.mkdir()
        monkeypatch.setattr("bernstein.adapters.q_dev.Path.home", lambda: empty_home)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)

        with pytest.raises(SpawnError, match="q login"):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-no-auth",
            )

    def test_missing_cache_does_not_invoke_subprocess(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The error must surface BEFORE Popen is touched."""
        adapter = QDevAdapter()
        empty_home = tmp_path / "fresh"
        empty_home.mkdir()
        monkeypatch.setattr("bernstein.adapters.q_dev.Path.home", lambda: empty_home)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)

        with patch("bernstein.adapters.q_dev.subprocess.Popen") as popen, pytest.raises(SpawnError):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-no-auth-no-popen",
            )
        popen.assert_not_called()

    @pytest.mark.skipif(
        platform.system() == "Windows",
        reason="XDG_DATA_HOME is a Linux/macOS convention; the Windows branch of "
        "_has_q_login_cache() reads %LOCALAPPDATA% / AppData and ignores XDG.",
    )
    def test_xdg_data_home_cache_satisfies_preflight(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """XDG_DATA_HOME is honoured when set."""
        adapter = QDevAdapter()
        xdg = tmp_path / "xdg-data"
        (xdg / "amazon-q").mkdir(parents=True)
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
        monkeypatch.setattr("bernstein.adapters.q_dev.Path.home", lambda: tmp_path / "elsewhere")

        proc_mock = make_popen_mock(950)
        with patch("bernstein.adapters.q_dev.subprocess.Popen", return_value=proc_mock):
            # Should not raise - XDG cache is enough.
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-xdg",
            )


# ---------------------------------------------------------------------------
# QDevAdapter.spawn() - env isolation
# ---------------------------------------------------------------------------


class TestQDevEnvIsolation:
    """spawn() exposes ONLY profile/region hints to the spawned q process.

    Q reads its bearer token from the on-disk login cache, so long-lived
    AWS access keys must NEVER be propagated.
    """

    def test_aws_access_keys_stripped(self, tmp_path: Path, fake_q_login: Path) -> None:
        adapter = QDevAdapter()
        proc_mock = make_popen_mock(910)
        with (
            patch("bernstein.adapters.q_dev.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {
                    "AWS_ACCESS_KEY_ID": "AKIATESTKEY",
                    "AWS_SECRET_ACCESS_KEY": "supersecret",
                    "AWS_SESSION_TOKEN": "longlivedtoken",
                    "AWS_PROFILE": "dev",
                    "AWS_REGION": "us-east-1",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-env1",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "AWS_ACCESS_KEY_ID" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "AWS_SESSION_TOKEN" not in env
        # Profile/region hints DO pass through - they're metadata, not
        # authentication material.
        assert env.get("AWS_PROFILE") == "dev"
        assert env.get("AWS_REGION") == "us-east-1"

    def test_unrelated_secrets_excluded(self, tmp_path: Path, fake_q_login: Path) -> None:
        adapter = QDevAdapter()
        proc_mock = make_popen_mock(911)
        with (
            patch("bernstein.adapters.q_dev.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_API_KEY": "ant-secret",
                    "OPENAI_API_KEY": "sk-test",
                    "DATABASE_URL": "postgres://x",
                    "PATH": "/usr/bin",
                },
                clear=True,
            ),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-env2",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert "DATABASE_URL" not in env

    def test_path_propagated(self, tmp_path: Path, fake_q_login: Path) -> None:
        adapter = QDevAdapter()
        proc_mock = make_popen_mock(912)
        with (
            patch("bernstein.adapters.q_dev.subprocess.Popen", return_value=proc_mock) as popen,
            patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-env3",
            )
        env = popen.call_args.kwargs.get("env", {})
        assert "PATH" in env


# ---------------------------------------------------------------------------
# QDevAdapter - external endpoint declaration
# ---------------------------------------------------------------------------


class TestQDevExternalEndpoints:
    """Adapter must declare its AWS-side endpoints for the network policy."""

    def test_amazonaws_endpoint_declared(self) -> None:
        endpoints = {host for host, _port in QDevAdapter.external_endpoints}
        assert "*.amazonaws.com" in endpoints

    def test_aws_dev_endpoint_declared(self) -> None:
        endpoints = {host for host, _port in QDevAdapter.external_endpoints}
        assert "*.aws.dev" in endpoints

    def test_endpoints_use_https_port(self) -> None:
        ports = {port for _host, port in QDevAdapter.external_endpoints}
        assert ports == {443}


# ---------------------------------------------------------------------------
# QDevAdapter - missing binary / PermissionError
# ---------------------------------------------------------------------------


class TestQDevSpawnMissingBinary:
    def test_file_not_found_raises_runtime_error(self, tmp_path: Path, fake_q_login: Path) -> None:
        adapter = QDevAdapter()
        with (
            patch(
                "bernstein.adapters.q_dev.subprocess.Popen",
                side_effect=FileNotFoundError("No such file"),
            ),
            pytest.raises(RuntimeError, match="not found in PATH"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-missing",
            )

    def test_permission_error_raises_runtime_error(self, tmp_path: Path, fake_q_login: Path) -> None:
        adapter = QDevAdapter()
        with (
            patch(
                "bernstein.adapters.q_dev.subprocess.Popen",
                side_effect=PermissionError("Permission denied"),
            ),
            pytest.raises(RuntimeError, match="[Pp]ermission"),
        ):
            adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="auto", effort="high"),
                session_id="qdev-perm",
            )


# ---------------------------------------------------------------------------
# is_alive() and kill() - inherited from CLIAdapter base
# ---------------------------------------------------------------------------


class TestQDevIsAlive:
    def test_true_when_process_exists(self) -> None:
        adapter = QDevAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=True):
            assert adapter.is_alive(1234) is True

    def test_false_when_dead(self) -> None:
        adapter = QDevAdapter()
        with patch("bernstein.adapters.base.process_alive", return_value=False):
            assert adapter.is_alive(9999) is False


class TestQDevKill:
    def test_calls_kill_process_group_graceful(self) -> None:
        adapter = QDevAdapter()
        with patch("bernstein.adapters.base.reap_process_group") as mock_kill:
            adapter.kill(555)
        mock_kill.assert_called_once_with(555)

    def test_does_not_raise_on_dead_process(self) -> None:
        adapter = QDevAdapter()
        with patch("bernstein.adapters.base.reap_process_group", return_value=False):
            adapter.kill(556)  # must not raise
