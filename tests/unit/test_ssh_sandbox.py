"""Unit tests for the SSH sandbox backend and ``bernstein remote`` CLI.

Every remote call is mocked at the :mod:`subprocess` boundary so the
tests run without any network or a real ``ssh`` binary.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from bernstein.cli.commands.remote_cmd import remote_group
from bernstein.core.sandbox.ssh_backend import (
    SandboxConnectionError,
    SSHSandboxBackend,
)


def _completed(returncode: int, stderr: bytes = b"", stdout: bytes = b"") -> MagicMock:
    """Return a stand-in for :class:`subprocess.CompletedProcess`."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# ---------------------------------------------------------------------------
# _build_ssh_cmd / flag assembly
# ---------------------------------------------------------------------------


def test_build_ssh_cmd_includes_identity_file(tmp_path: Path) -> None:
    key = tmp_path / "id_test"
    key.write_text("fake")
    backend = SSHSandboxBackend(
        host="example.com",
        user="alice",
        path="/srv/bern",
        identity_file=key,
    )
    argv = backend._build_ssh_cmd("echo hi")
    assert argv[0] == "ssh"
    assert "-i" in argv
    assert str(key) in argv
    assert argv[-2] == "alice@example.com"


def test_build_ssh_cmd_omits_identity_when_unset() -> None:
    backend = SSHSandboxBackend(host="example.com", path="/srv/bern")
    argv = backend._build_ssh_cmd("echo hi")
    assert "-i" not in argv


def test_build_ssh_cmd_wraps_command_in_sh_c() -> None:
    backend = SSHSandboxBackend(host="example.com", path="/srv/bern")
    argv = backend._build_ssh_cmd("ls /")
    # Last argv element is always the remote command wrapped in sh -c.
    assert argv[-1].startswith("sh -c ")


def test_connect_timeout_always_present() -> None:
    backend = SSHSandboxBackend(host="example.com", path="/srv/bern")
    argv = backend._build_ssh_cmd("uptime")
    joined = " ".join(argv)
    assert "ConnectTimeout=10" in joined
    assert "ServerAliveInterval=30" in joined


def test_strict_host_key_checking_respected() -> None:
    strict = SSHSandboxBackend(host="example.com", path="/srv/bern", strict_host_key_checking=True)
    lax = SSHSandboxBackend(host="example.com", path="/srv/bern", strict_host_key_checking=False)
    assert "StrictHostKeyChecking=yes" in " ".join(strict._build_ssh_cmd("echo"))
    assert "StrictHostKeyChecking=accept-new" in " ".join(lax._build_ssh_cmd("echo"))


def test_port_flag_used() -> None:
    backend = SSHSandboxBackend(host="example.com", path="/srv/bern", port=2222)
    argv = backend._build_ssh_cmd("echo hi")
    assert "-p" in argv
    assert "2222" in argv


# ---------------------------------------------------------------------------
# ControlMaster socket deterministic path
# ---------------------------------------------------------------------------


def test_control_socket_deterministic_for_host_and_pid() -> None:
    path_a = SSHSandboxBackend._build_control_socket_path("example.com", 12345)
    path_b = SSHSandboxBackend._build_control_socket_path("example.com", 12345)
    path_c = SSHSandboxBackend._build_control_socket_path("example.com", 99)
    assert path_a == path_b
    assert path_a != path_c
    assert path_a.name == "bernstein-example.com-12345.sock"
    assert path_a.parent.name == ".ssh"


def test_control_socket_sanitises_dangerous_host_chars() -> None:
    path = SSHSandboxBackend._build_control_socket_path("bad/host:22", 1)
    assert "/" not in path.name.split("-", 1)[1][: -len("-1.sock")]
    assert ":" not in path.name


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


def test_connection_refused_stderr_raises_sandbox_connection_error() -> None:
    backend = SSHSandboxBackend(host="example.com", path="/srv/bern")
    with patch(
        "bernstein.core.sandbox.ssh_backend.subprocess.run",
        return_value=_completed(255, stderr=b"ssh: connect to host example.com port 22: Connection refused"),
    ):
        try:
            backend.ensure_control_master()
        except SandboxConnectionError as exc:
            assert exc.host == "example.com"
            assert "connection refused" in exc.reason
        else:
            raise AssertionError("SandboxConnectionError not raised")


def test_permission_denied_stderr_hints_ssh_add() -> None:
    backend = SSHSandboxBackend(host="example.com", path="/srv/bern")
    with patch(
        "bernstein.core.sandbox.ssh_backend.subprocess.run",
        return_value=_completed(255, stderr=b"Permission denied (publickey)."),
    ):
        try:
            backend.ensure_control_master()
        except SandboxConnectionError as exc:
            assert exc.reason == "permission denied"
            assert exc.hint is not None
            assert "ssh-add" in exc.hint
        else:
            raise AssertionError("SandboxConnectionError not raised")


# ---------------------------------------------------------------------------
# spawn_agent wraps subprocess.Popen
# ---------------------------------------------------------------------------


def test_spawn_agent_routes_through_ssh() -> None:
    backend = SSHSandboxBackend(host="example.com", path="/srv/bern")

    fake_popen = MagicMock()
    with (
        patch.object(backend, "ensure_control_master"),
        patch("subprocess.Popen", return_value=fake_popen) as popen,
    ):
        handle = backend.spawn_agent(
            ["agent", "--task", "hello"],
            cwd="/tmp/bern/sbx-123",
        )

    assert handle is fake_popen
    argv = popen.call_args.args[0]
    assert argv[0] == "ssh"
    script = argv[-1]
    assert "cd /tmp/bern/sbx-123" in script
    assert "agent" in script
    assert "hello" in script


# ---------------------------------------------------------------------------
# CLI: remote test
# ---------------------------------------------------------------------------


def test_remote_test_calls_ssh_uptime_and_returns_exit_code() -> None:
    runner = CliRunner()
    with (
        patch(
            "bernstein.cli.commands.remote_cmd.shutil.which",
            return_value="/usr/bin/ssh",
        ),
        patch(
            "bernstein.cli.commands.remote_cmd.subprocess.run",
            return_value=_completed(0, stdout=b" 12:00  up 3 days\n"),
        ) as run,
    ):
        result = runner.invoke(remote_group, ["test", "server.example.com"])

    assert result.exit_code == 0
    argv: list[str] = run.call_args.args[0]
    assert argv[0] == "ssh"
    assert argv[-1] == "uptime"
    assert "server.example.com" in argv


def test_remote_test_surfaces_permission_denied_hint() -> None:
    runner = CliRunner()
    with (
        patch(
            "bernstein.cli.commands.remote_cmd.shutil.which",
            return_value="/usr/bin/ssh",
        ),
        patch(
            "bernstein.cli.commands.remote_cmd.subprocess.run",
            return_value=_completed(255, stderr=b"Permission denied (publickey)."),
        ),
    ):
        result = runner.invoke(remote_group, ["test", "server.example.com"])

    assert result.exit_code != 0
    assert "ssh-add" in result.output or "IdentityFile" in result.output


# ---------------------------------------------------------------------------
# CLI: remote forget
# ---------------------------------------------------------------------------


def test_remote_forget_removes_control_socket(tmp_path: Path) -> None:
    runner = CliRunner()
    fake_sock = MagicMock(spec=Path)
    fake_sock.__str__.return_value = "/home/u/.ssh/bernstein-h-1.sock"
    fake_sock.unlink = MagicMock()

    with patch(
        "bernstein.cli.commands.remote_cmd._control_socket_candidates",
        return_value=[fake_sock],
    ):
        result = runner.invoke(remote_group, ["forget", "server.example.com"])

    assert result.exit_code == 0
    fake_sock.unlink.assert_called_once()
    assert "forgot 1 socket" in result.output


def test_remote_forget_without_candidates_is_no_op() -> None:
    runner = CliRunner()
    with patch(
        "bernstein.cli.commands.remote_cmd._control_socket_candidates",
        return_value=[],
    ):
        result = runner.invoke(remote_group, ["forget", "unknown.example"])

    assert result.exit_code == 0
    assert "no cached sockets" in result.output
