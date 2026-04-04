"""Unit tests for the SSH execution backend."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from bernstein.core.ssh_backend import SSHBackend, SSHError, SSHHostConfig, parse_ssh_config


# ---------------------------------------------------------------------------
# parse_ssh_config
# ---------------------------------------------------------------------------


def test_parse_ssh_config_minimal() -> None:
    """Only host is required; all other fields use defaults."""
    config = parse_ssh_config({"host": "builder.example.com"})
    assert config is not None
    assert config.host == "builder.example.com"
    assert config.port == 22
    assert config.user == ""
    assert config.key == ""
    assert config.remote_dir == "~/bernstein-workdir"
    assert config.env == {}


def test_parse_ssh_config_full() -> None:
    """All fields are parsed correctly."""
    config = parse_ssh_config(
        {
            "host": "10.0.0.5",
            "user": "ci",
            "port": 2222,
            "key": "~/.ssh/ci_key",
            "remote_dir": "/tmp/agents",
            "rsync_excludes": [".venv", "dist/"],
            "env": {"ANTHROPIC_API_KEY": "sk-test"},
            "connect_timeout": 30,
        }
    )
    assert config is not None
    assert config.host == "10.0.0.5"
    assert config.user == "ci"
    assert config.port == 2222
    assert config.key == "~/.ssh/ci_key"
    assert config.remote_dir == "/tmp/agents"
    assert ".venv" in config.rsync_excludes
    assert config.env == {"ANTHROPIC_API_KEY": "sk-test"}
    assert config.connect_timeout == 30


def test_parse_ssh_config_none_returns_none() -> None:
    assert parse_ssh_config(None) is None


def test_parse_ssh_config_missing_host() -> None:
    with pytest.raises(ValueError, match="remote.host"):
        parse_ssh_config({"user": "ubuntu"})


def test_parse_ssh_config_empty_host() -> None:
    with pytest.raises(ValueError, match="remote.host"):
        parse_ssh_config({"host": "   "})


def test_parse_ssh_config_invalid_type() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        parse_ssh_config("not-a-dict")


def test_parse_ssh_config_invalid_port() -> None:
    with pytest.raises((ValueError, TypeError)):
        parse_ssh_config({"host": "h", "port": "not-an-int"})


# ---------------------------------------------------------------------------
# SSHHostConfig helpers
# ---------------------------------------------------------------------------


def test_ssh_target_with_user() -> None:
    config = SSHHostConfig(host="myhost", user="alice")
    assert config.ssh_target() == "alice@myhost"


def test_ssh_target_without_user() -> None:
    config = SSHHostConfig(host="myhost")
    assert config.ssh_target() == "myhost"


def test_resolved_env_expands_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_KEY", "secret-value")
    config = SSHHostConfig(host="h", env={"API_KEY": "${MY_KEY}", "PLAIN": "hello"})
    resolved = config.resolved_env()
    assert resolved["API_KEY"] == "secret-value"
    assert resolved["PLAIN"] == "hello"


def test_resolved_env_skips_missing_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
    config = SSHHostConfig(host="h", env={"X": "${NONEXISTENT_VAR}"})
    # Should not raise; missing vars are skipped
    resolved = config.resolved_env()
    assert "X" not in resolved


def test_all_rsync_excludes_merges_defaults() -> None:
    config = SSHHostConfig(host="h", rsync_excludes=("custom/",))
    all_excludes = config.all_rsync_excludes()
    assert "custom/" in all_excludes
    assert ".git" in all_excludes  # from defaults
    assert "__pycache__" in all_excludes


# ---------------------------------------------------------------------------
# SSHBackend
# ---------------------------------------------------------------------------


def _make_backend(extra: dict | None = None) -> SSHBackend:
    cfg = SSHHostConfig(host="testhost", user="agent", **(extra or {}))
    return SSHBackend(cfg, session_id="sess-abc123")


def test_ssh_cmd_no_key() -> None:
    backend = _make_backend()
    cmd = backend._ssh_cmd()
    assert "ssh" in cmd
    assert "-i" not in cmd
    assert "-p" in cmd
    assert "22" in cmd


def test_ssh_cmd_with_key() -> None:
    backend = _make_backend({"key": "~/.ssh/mykey"})
    cmd = backend._ssh_cmd()
    assert "-i" in cmd
    expanded_key = os.path.expanduser("~/.ssh/mykey")
    assert expanded_key in cmd


def test_rsync_cmd_contains_excludes() -> None:
    backend = _make_backend()
    cmd = backend._rsync_cmd(src="/local/", dest="agent@testhost:/remote/")
    cmd_str = " ".join(cmd)
    assert "--exclude" in cmd_str
    assert ".git" in cmd_str


def test_wrap_remote_cmd_includes_cd_and_env() -> None:
    cfg = SSHHostConfig(host="h", user="u", env={"FOO": "bar"})
    backend = SSHBackend(cfg, session_id="s1")
    result = backend._wrap_remote_cmd(["claude", "-p", "do stuff"])
    assert "cd " in result
    assert "export FOO=" in result
    assert "claude" in result


def test_run_raises_on_nonzero(tmp_path: Path) -> None:
    with pytest.raises(SSHError, match="exit 1"):
        SSHBackend._run(["false"], desc="test")


def test_run_raises_on_missing_binary() -> None:
    with pytest.raises(SSHError, match="OS error"):
        SSHBackend._run(["nonexistent_binary_xyz"], desc="test")


def test_ensure_remote_dir_calls_ssh(tmp_path: Path) -> None:
    backend = _make_backend()
    with patch.object(SSHBackend, "_run") as mock_run:
        backend.ensure_remote_dir()
        assert mock_run.called
        args = mock_run.call_args[0][0]
        assert "mkdir" in args
        assert backend._remote_session_dir in args


def test_sync_to_remote_calls_rsync(tmp_path: Path) -> None:
    backend = _make_backend()
    with patch.object(SSHBackend, "_run") as mock_run:
        backend.sync_to_remote(tmp_path)
        assert mock_run.called
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "rsync"


def test_sync_from_remote_calls_rsync(tmp_path: Path) -> None:
    backend = _make_backend()
    with patch.object(SSHBackend, "_run") as mock_run:
        backend.sync_from_remote(tmp_path)
        assert mock_run.called
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "rsync"


def test_test_connectivity_returns_false_on_timeout() -> None:
    backend = _make_backend()
    import subprocess

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=1)):
        assert backend.test_connectivity() is False


def test_test_connectivity_returns_false_on_os_error() -> None:
    backend = _make_backend()
    with patch("subprocess.run", side_effect=OSError("no route")):
        assert backend.test_connectivity() is False


def test_test_connectivity_returns_true_on_zero_exit() -> None:
    backend = _make_backend()
    mock_result = MagicMock()
    mock_result.returncode = 0
    with patch("subprocess.run", return_value=mock_result):
        assert backend.test_connectivity() is True


def test_spawn_creates_popen(tmp_path: Path) -> None:
    backend = _make_backend()
    mock_proc = MagicMock()
    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
        result = backend.spawn(cmd=["claude", "-p", "hi"], workdir=tmp_path)
    assert result is mock_proc
    assert mock_popen.called
    cmd_used = mock_popen.call_args[0][0]
    assert "ssh" in cmd_used[0]
