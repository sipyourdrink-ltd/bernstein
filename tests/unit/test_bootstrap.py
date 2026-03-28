"""Tests for bernstein.core.bootstrap."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import httpx
import pytest

from bernstein.core.bootstrap import (
    SDD_DIRS,
    _check_api_key,
    _check_binary,
    _check_port_free,
    _clean_stale_runtime,
    _ensure_sdd,
    _is_alive,
    _read_pid,
    _resolve_auth_token,
    _resolve_bind_host,
    _resolve_server_url,
    _send_webhook,
    _start_server,
    _start_spawner,
    _wait_for_server,
    preflight_checks,
)
from bernstein.core.seed import NotifyConfig

# ---------------------------------------------------------------------------
# _read_pid
# ---------------------------------------------------------------------------


class TestReadPid:
    def test_returns_pid_from_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "server.pid"
        pid_file.write_text("12345")
        assert _read_pid(pid_file) == 12345

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert _read_pid(tmp_path / "nonexistent.pid") is None

    def test_returns_none_for_invalid_content(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "bad.pid"
        pid_file.write_text("not-a-number")
        assert _read_pid(pid_file) is None

    def test_returns_none_for_empty_file(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "empty.pid"
        pid_file.write_text("")
        assert _read_pid(pid_file) is None


# ---------------------------------------------------------------------------
# _is_alive
# ---------------------------------------------------------------------------


class TestIsAlive:
    def test_alive_process_returns_true(self) -> None:
        # os.getpid() is guaranteed to be alive
        assert _is_alive(os.getpid()) is True

    def test_dead_process_returns_false(self) -> None:
        # PID 0 is never a real user process; sending signal 0 raises OSError
        with patch("os.kill", side_effect=OSError):
            assert _is_alive(99999999) is False


# ---------------------------------------------------------------------------
# _clean_stale_runtime
# ---------------------------------------------------------------------------


class TestCleanStaleRuntime:
    def test_no_runtime_dir_is_noop(self, tmp_path: Path) -> None:
        # Should not raise even when .sdd/runtime does not exist
        _clean_stale_runtime(tmp_path)

    def test_removes_stale_pid_file(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        pid_file = runtime / "server.pid"
        pid_file.write_text("999999999")  # almost certainly dead

        with patch("bernstein.core.bootstrap._is_alive", return_value=False):
            _clean_stale_runtime(tmp_path)

        assert not pid_file.exists()

    def test_keeps_alive_pid_file(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        pid_file = runtime / "server.pid"
        pid_file.write_text(str(os.getpid()))

        with patch("bernstein.core.bootstrap._is_alive", return_value=True):
            _clean_stale_runtime(tmp_path)

        assert pid_file.exists()

    def test_removes_log_files(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        log = runtime / "server.log"
        log.write_text("old log")

        _clean_stale_runtime(tmp_path)

        assert not log.exists()

    def test_removes_tasks_jsonl(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        jsonl = runtime / "tasks.jsonl"
        jsonl.write_text('{"id":"t1"}\n')

        _clean_stale_runtime(tmp_path)

        assert not jsonl.exists()

    def test_pid_file_with_invalid_content_is_removed(self, tmp_path: Path) -> None:
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        pid_file = runtime / "spawner.pid"
        pid_file.write_text("garbage")

        _clean_stale_runtime(tmp_path)

        # _read_pid returns None for invalid content → treated as stale → removed
        assert not pid_file.exists()


# ---------------------------------------------------------------------------
# _ensure_sdd
# ---------------------------------------------------------------------------


class TestEnsureSdd:
    def test_creates_all_sdd_dirs(self, tmp_path: Path) -> None:
        _ensure_sdd(tmp_path)
        for d in SDD_DIRS:
            assert (tmp_path / d).is_dir(), f"Missing {d}"

    def test_writes_default_config(self, tmp_path: Path) -> None:
        _ensure_sdd(tmp_path)
        config = (tmp_path / ".sdd" / "config.yaml").read_text()
        assert "server_port: 8052" in config
        assert "max_workers: 4" in config

    def test_writes_gitignore(self, tmp_path: Path) -> None:
        _ensure_sdd(tmp_path)
        gi = (tmp_path / ".sdd" / "runtime" / ".gitignore").read_text()
        assert "*.pid" in gi
        assert "*.log" in gi

    def test_returns_true_when_newly_created(self, tmp_path: Path) -> None:
        assert _ensure_sdd(tmp_path) is True

    def test_returns_false_when_already_exists(self, tmp_path: Path) -> None:
        _ensure_sdd(tmp_path)
        assert _ensure_sdd(tmp_path) is False

    def test_does_not_overwrite_existing_config(self, tmp_path: Path) -> None:
        _ensure_sdd(tmp_path)
        config_path = tmp_path / ".sdd" / "config.yaml"
        config_path.write_text("custom: true\n")
        _ensure_sdd(tmp_path)
        assert config_path.read_text() == "custom: true\n"


# ---------------------------------------------------------------------------
# _start_server
# ---------------------------------------------------------------------------


class TestStartServer:
    def _setup_runtime(self, workdir: Path) -> None:
        (workdir / ".sdd" / "runtime").mkdir(parents=True)

    def test_spawns_uvicorn_process(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 42

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            pid = _start_server(tmp_path, port=8052)

        assert pid == 42
        args = mock_popen.call_args[0][0]
        assert "uvicorn" in args

    def test_writes_pid_file(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 1234

        with patch("subprocess.Popen", return_value=mock_proc):
            _start_server(tmp_path, port=8052)

        pid_file = tmp_path / ".sdd" / "runtime" / "server.pid"
        assert pid_file.read_text() == "1234"

    def test_raises_if_server_already_running(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        pid_file = tmp_path / ".sdd" / "runtime" / "server.pid"
        pid_file.write_text(str(os.getpid()))

        with (
            patch("bernstein.core.bootstrap._is_alive", return_value=True),
            pytest.raises(RuntimeError, match="already running"),
        ):
            _start_server(tmp_path, port=8052)

    def test_uses_specified_port(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 99

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            _start_server(tmp_path, port=9999)

        args = mock_popen.call_args[0][0]
        assert "9999" in args


# ---------------------------------------------------------------------------
# _wait_for_server
# ---------------------------------------------------------------------------


class TestWaitForServer:
    def test_returns_true_when_server_responds(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.get", return_value=mock_resp), patch("time.sleep"):
            result = _wait_for_server(8052)

        assert result is True

    def test_returns_false_on_timeout(self) -> None:
        # Always raise ConnectError, simulate time advancing past deadline
        call_count = 0

        def fake_monotonic() -> float:
            nonlocal call_count
            call_count += 1
            # First call: deadline setup; subsequent calls: already past deadline
            return 0.0 if call_count == 1 else 999.0

        with (
            patch("httpx.get", side_effect=httpx.ConnectError("refused")),
            patch("time.sleep"),
            patch("time.monotonic", side_effect=fake_monotonic),
        ):
            result = _wait_for_server(8052)

        assert result is False

    def test_retries_on_connect_error_then_succeeds(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        side_effects = [httpx.ConnectError("refused"), mock_resp]

        monotonic_values = iter([0.0, 1.0, 2.0, 100.0])

        with (
            patch("httpx.get", side_effect=side_effects),
            patch("time.sleep"),
            patch("time.monotonic", side_effect=monotonic_values),
        ):
            result = _wait_for_server(8052)

        assert result is True


# ---------------------------------------------------------------------------
# _start_spawner
# ---------------------------------------------------------------------------


class TestStartSpawner:
    def _setup_runtime(self, workdir: Path) -> None:
        (workdir / ".sdd" / "runtime").mkdir(parents=True)

    def test_spawns_orchestrator_process(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 77

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            pid = _start_spawner(tmp_path, port=8052)

        assert pid == 77
        args = mock_popen.call_args[0][0]
        assert "orchestrator" in " ".join(args)

    def test_writes_spawner_pid_file(self, tmp_path: Path) -> None:
        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 555

        with patch("subprocess.Popen", return_value=mock_proc):
            _start_spawner(tmp_path, port=8052)

        pid_file = tmp_path / ".sdd" / "runtime" / "spawner.pid"
        assert pid_file.read_text() == "555"


# ---------------------------------------------------------------------------
# _send_webhook
# ---------------------------------------------------------------------------


class TestSendWebhook:
    def test_posts_json_to_webhook_url(self) -> None:
        config = NotifyConfig(webhook_url="https://hooks.example.com/notify")
        payload = {"event": "complete", "goal": "Build a REST API"}
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            _send_webhook(config, payload)

        mock_post.assert_called_once_with(
            "https://hooks.example.com/notify",
            json=payload,
            timeout=10.0,
        )

    def test_no_op_when_webhook_url_is_none(self) -> None:
        config = NotifyConfig(webhook_url=None)
        with patch("httpx.post") as mock_post:
            _send_webhook(config, {"event": "complete"})
        mock_post.assert_not_called()

    def test_swallows_http_errors(self) -> None:
        config = NotifyConfig(webhook_url="https://hooks.example.com/notify")
        with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
            # Must not raise
            _send_webhook(config, {"event": "complete"})

    def test_swallows_unexpected_exceptions(self) -> None:
        config = NotifyConfig(webhook_url="https://hooks.example.com/notify")
        with patch("httpx.post", side_effect=RuntimeError("boom")):
            _send_webhook(config, {"event": "complete"})


# ---------------------------------------------------------------------------
# _check_binary
# ---------------------------------------------------------------------------


class TestCheckBinary:
    def test_passes_when_binary_found(self) -> None:
        with patch("shutil.which", return_value="/usr/local/bin/claude"):
            _check_binary("claude")  # must not raise

    def test_exits_when_binary_missing(self) -> None:
        with patch("shutil.which", return_value=None), pytest.raises(SystemExit):
            _check_binary("claude")

    def test_exits_for_unknown_cli(self) -> None:
        with patch("shutil.which", return_value=None), pytest.raises(SystemExit):
            _check_binary("nonexistent-cli")

    def test_exit_for_codex_when_missing(self) -> None:
        with patch("shutil.which", return_value=None), pytest.raises(SystemExit):
            _check_binary("codex")

    def test_exit_for_gemini_when_missing(self) -> None:
        with patch("shutil.which", return_value=None), pytest.raises(SystemExit):
            _check_binary("gemini")


# ---------------------------------------------------------------------------
# _check_api_key
# ---------------------------------------------------------------------------


class TestCheckApiKey:
    def test_passes_when_claude_key_set(self) -> None:
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}):
            _check_api_key("claude")  # must not raise

    @patch("bernstein.core.bootstrap._claude_has_oauth_session", return_value=False)
    def test_exits_when_claude_key_missing(self, _mock_oauth: MagicMock) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit):
            _check_api_key("claude")

    @patch("bernstein.core.bootstrap._claude_has_oauth_session", return_value=True)
    def test_passes_when_claude_oauth_active_no_key(self, _mock_oauth: MagicMock) -> None:
        """Claude with active OAuth session should pass even without ANTHROPIC_API_KEY."""
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            _check_api_key("claude")  # must not raise

    def test_passes_when_codex_key_set(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            _check_api_key("codex")

    def test_exits_when_codex_key_missing(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit):
            _check_api_key("codex")

    def test_passes_when_gemini_key_set(self) -> None:
        with patch.dict(os.environ, {"GOOGLE_API_KEY": "AIza-test"}):
            _check_api_key("gemini")

    def test_exits_when_gemini_key_missing(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "GOOGLE_API_KEY"}
        with patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit):
            _check_api_key("gemini")

    def test_qwen_passes_with_openai_key(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            _check_api_key("qwen")

    def test_qwen_passes_with_openrouter_key(self) -> None:
        with patch.dict(os.environ, {"OPENROUTER_API_KEY_PAID": "or-test"}):
            _check_api_key("qwen")

    def test_qwen_exits_when_no_keys_set(self) -> None:
        qwen_vars = (
            "OPENROUTER_API_KEY_PAID",
            "OPENROUTER_API_KEY_FREE",
            "OPENAI_API_KEY",
            "TOGETHERAI_USER_KEY",
            "OXen_API_KEY",
            "G4F_API_KEY",
        )
        env = {k: v for k, v in os.environ.items() if k not in qwen_vars}
        with patch.dict(os.environ, env, clear=True), pytest.raises(SystemExit):
            _check_api_key("qwen")


# ---------------------------------------------------------------------------
# _check_port_free
# ---------------------------------------------------------------------------


class TestCheckPortFree:
    def test_passes_when_port_is_free(self) -> None:
        # Mock socket.socket so bind succeeds
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.bind = MagicMock()

        with patch("socket.socket", return_value=mock_sock):
            _check_port_free(8052)  # must not raise

    def test_exits_when_port_is_occupied(self) -> None:
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.bind = MagicMock(side_effect=OSError("address in use"))

        with patch("socket.socket", return_value=mock_sock), pytest.raises(SystemExit):
            _check_port_free(8052)


# ---------------------------------------------------------------------------
# preflight_checks
# ---------------------------------------------------------------------------


class TestPreflightChecks:
    def test_passes_all_checks(self) -> None:
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.bind = MagicMock()

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}),
            patch("socket.socket", return_value=mock_sock),
        ):
            preflight_checks("claude", 8052)  # must not raise

    def test_fails_on_missing_binary(self) -> None:
        with patch("shutil.which", return_value=None), pytest.raises(SystemExit):
            preflight_checks("claude", 8052)

    @patch("bernstein.core.bootstrap._claude_has_oauth_session", return_value=False)
    def test_fails_on_missing_api_key(self, _mock_oauth: MagicMock) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.dict(os.environ, env, clear=True),
            pytest.raises(SystemExit),
        ):
            preflight_checks("claude", 8052)

    def test_fails_on_port_conflict(self) -> None:
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.bind = MagicMock(side_effect=OSError("address in use"))

        with (
            patch("shutil.which", return_value="/usr/bin/claude"),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}),
            patch("socket.socket", return_value=mock_sock),
            pytest.raises(SystemExit),
        ):
            preflight_checks("claude", 8052)

    def test_binary_check_runs_before_api_key_check(self) -> None:
        """If binary is missing, should exit before even checking API key."""
        with patch("shutil.which", return_value=None), pytest.raises(SystemExit):
            # No API key set either, but binary check should fire first
            preflight_checks("claude", 8052)

    def test_checks_all_adapters(self) -> None:
        """preflight_checks works for all supported adapters."""
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        mock_sock.bind = MagicMock()

        for cli, key_var, key_val in [
            ("claude", "ANTHROPIC_API_KEY", "sk-ant-test"),
            ("codex", "OPENAI_API_KEY", "sk-test"),
            ("gemini", "GOOGLE_API_KEY", "AIza-test"),
            ("qwen", "OPENROUTER_API_KEY_PAID", "or-test"),
        ]:
            with (
                patch("shutil.which", return_value=f"/usr/bin/{cli}"),
                patch.dict(os.environ, {key_var: key_val}),
                patch("socket.socket", return_value=mock_sock),
            ):
                preflight_checks(cli, 8052)  # must not raise


# ---------------------------------------------------------------------------
# Server timeout is fatal
# ---------------------------------------------------------------------------


class TestServerTimeoutFatal:
    """Verify that server startup timeout raises SystemExit, not a warning."""

    def test_wait_for_server_returns_false_on_timeout(self) -> None:
        """_wait_for_server returns False (not True) when server never responds."""
        call_count = 0

        def fake_monotonic() -> float:
            nonlocal call_count
            call_count += 1
            return 0.0 if call_count == 1 else 999.0

        with (
            patch("httpx.get", side_effect=httpx.ConnectError("refused")),
            patch("time.sleep"),
            patch("time.monotonic", side_effect=fake_monotonic),
        ):
            result = _wait_for_server(8052)

        # Must return False — callers must treat this as a fatal error
        assert result is False


# ---------------------------------------------------------------------------
# Cluster env var helpers
# ---------------------------------------------------------------------------


class TestResolveServerUrl:
    def test_default_uses_port(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = _resolve_server_url(9999)
        assert result == "http://127.0.0.1:9999"

    def test_env_var_overrides_port(self) -> None:
        with patch.dict(os.environ, {"BERNSTEIN_SERVER_URL": "http://remote.example.com:8052"}):
            result = _resolve_server_url(8052)
        assert result == "http://remote.example.com:8052"


class TestResolveBindHost:
    def test_default_is_localhost(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_bind_host() == "127.0.0.1"

    def test_env_var_overrides_default(self) -> None:
        with patch.dict(os.environ, {"BERNSTEIN_BIND_HOST": "0.0.0.0"}):
            assert _resolve_bind_host() == "0.0.0.0"


class TestResolveAuthToken:
    def test_returns_none_when_not_set(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert _resolve_auth_token() is None

    def test_returns_token_when_set(self) -> None:
        with patch.dict(os.environ, {"BERNSTEIN_AUTH_TOKEN": "my-secret"}):
            assert _resolve_auth_token() == "my-secret"
