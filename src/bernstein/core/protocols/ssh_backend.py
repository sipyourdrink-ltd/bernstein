"""SSH execution backend — run agents on remote machines.

Agents are spawned on a remote host via SSH. The local workspace is
synced to the remote using rsync before execution and synced back
after the agent exits.

Configuration in bernstein.yaml::

    remote:
      host: "agent-box.example.com"
      user: "ubuntu"
      port: 22
      key: "~/.ssh/id_ed25519"
      remote_dir: "~/bernstein-workdir"
      rsync_excludes:
        - ".git"
        - ".venv"
        - "__pycache__"
      env:
        ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"

Usage::

    backend = SSHBackend(config)
    proc = backend.spawn(cmd=["claude", "-p", prompt], workdir=Path("."))
    proc.wait()
    backend.sync_back(workdir=Path("."))
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from string import Template
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# How long to wait between process-alive polls (seconds).
_POLL_INTERVAL_S: float = 5.0

# Default rsync excludes — keeps sync fast and avoids transmitting large dirs.
_DEFAULT_RSYNC_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".venv",
    "__pycache__",
    "*.pyc",
    ".sdd/runtime/",
    "node_modules",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
)


class SSHError(RuntimeError):
    """Raised when an SSH or rsync operation fails."""


@dataclass(frozen=True)
class SSHHostConfig:
    """Connection parameters for a remote SSH host.

    Attributes:
        host: Hostname or IP address of the remote machine.
        user: SSH username. Defaults to the local $USER.
        port: SSH port. Defaults to 22.
        key: Path to the SSH private key file. Empty string means use ssh-agent.
        remote_dir: Base directory on the remote host where workspaces live.
        rsync_excludes: Patterns excluded from rsync sync. Merged with defaults.
        env: Extra environment variables passed to the remote agent process.
            Values may reference local env vars via ``${VAR}`` syntax.
        connect_timeout: SSH connect timeout in seconds.
    """

    host: str
    user: str = ""
    port: int = 22
    key: str = ""
    remote_dir: str = "~/bernstein-workdir"
    rsync_excludes: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=lambda: {})
    connect_timeout: int = 15

    def ssh_target(self) -> str:
        """Return user@host (or host when user is unset)."""
        if self.user:
            return f"{self.user}@{self.host}"
        return self.host

    def resolved_env(self) -> dict[str, str]:
        """Expand ``${VAR}`` references in env values from the local environment.

        Returns:
            Dict with all variable references resolved from os.environ.
        """
        result: dict[str, str] = {}
        for key, value in self.env.items():
            try:
                result[key] = Template(value).substitute(os.environ)
            except (KeyError, ValueError):
                logger.warning("SSH env var %r: could not expand %r — skipping", key, value)
        return result

    def all_rsync_excludes(self) -> tuple[str, ...]:
        """Merge caller-supplied excludes with the built-in defaults."""
        return _DEFAULT_RSYNC_EXCLUDES + self.rsync_excludes


def parse_ssh_config(raw: object | None) -> SSHHostConfig | None:
    """Parse the optional ``remote`` section from a seed file.

    Args:
        raw: Raw value from ``bernstein.yaml``.

    Returns:
        Parsed :class:`SSHHostConfig`, or ``None`` when the section is absent.

    Raises:
        ValueError: If required fields are missing or have wrong types.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("remote: must be a mapping")

    cfg = cast("dict[str, Any]", raw)

    host = cfg.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ValueError("remote.host is required and must be a non-empty string")

    user_raw = cfg.get("user", "")
    if not isinstance(user_raw, str):
        raise ValueError("remote.user must be a string")

    port_raw = cfg.get("port", 22)
    if not isinstance(port_raw, int):
        raise ValueError("remote.port must be an integer")

    key_raw = cfg.get("key", "")
    if not isinstance(key_raw, str):
        raise ValueError("remote.key must be a string path")

    remote_dir_raw = cfg.get("remote_dir", "~/bernstein-workdir")
    if not isinstance(remote_dir_raw, str):
        raise ValueError("remote.remote_dir must be a string")

    excludes_raw: Any = cfg.get("rsync_excludes") or []
    if not isinstance(excludes_raw, list):
        raise ValueError("remote.rsync_excludes must be a list")
    excludes: tuple[str, ...] = tuple(str(e) for e in cast("list[object]", excludes_raw))

    env_raw: Any = cfg.get("env") or {}
    if not isinstance(env_raw, dict):
        raise ValueError("remote.env must be a mapping")
    env: dict[str, str] = {str(k): str(v) for k, v in cast("dict[str, object]", env_raw).items()}

    timeout_raw = cfg.get("connect_timeout", 15)
    if not isinstance(timeout_raw, int):
        raise ValueError("remote.connect_timeout must be an integer")

    return SSHHostConfig(
        host=host.strip(),
        user=user_raw.strip(),
        port=port_raw,
        key=key_raw.strip(),
        remote_dir=remote_dir_raw.strip(),
        rsync_excludes=excludes,
        env=env,
        connect_timeout=timeout_raw,
    )


class SSHBackend:
    """Execute commands on a remote machine via SSH.

    Each instance manages a single remote session workspace. The typical
    lifecycle is::

        backend = SSHBackend(config, session_id="abc123")
        backend.sync_to_remote(local_workdir)
        proc = backend.spawn(cmd=["claude", "-p", prompt], workdir=local_workdir)
        proc.wait()
        backend.sync_from_remote(local_workdir)

    Args:
        config: SSH host configuration.
        session_id: Unique identifier for this agent session. Used to
            namespace the remote working directory so parallel agents
            do not collide.
    """

    def __init__(self, config: SSHHostConfig, session_id: str) -> None:
        self._config = config
        self._session_id = session_id
        self._remote_session_dir = f"{config.remote_dir}/{session_id}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_remote_dir(self) -> None:
        """Create the remote session directory if it does not exist.

        Raises:
            SSHError: If the SSH command fails.
        """
        cmd = [*self._ssh_cmd(), "mkdir", "-p", self._remote_session_dir]
        self._run(cmd, desc="create remote dir")

    def sync_to_remote(self, workdir: Path) -> None:
        """Push the local workspace to the remote host with rsync.

        Args:
            workdir: Local project root to sync.

        Raises:
            SSHError: If rsync fails.
        """
        src = str(workdir).rstrip("/") + "/"
        dest = f"{self._config.ssh_target()}:{self._remote_session_dir}/"
        cmd = self._rsync_cmd(src=src, dest=dest)
        logger.info("ssh-backend: syncing to remote %s", self._config.host)
        self._run(cmd, desc="rsync to remote")

    def sync_from_remote(self, workdir: Path) -> None:
        """Pull the remote workspace back to the local host.

        Args:
            workdir: Local project root to write results into.

        Raises:
            SSHError: If rsync fails.
        """
        src = f"{self._config.ssh_target()}:{self._remote_session_dir}/"
        dest = str(workdir).rstrip("/") + "/"
        cmd = self._rsync_cmd(src=src, dest=dest)
        logger.info("ssh-backend: syncing from remote %s", self._config.host)
        self._run(cmd, desc="rsync from remote")

    def spawn(
        self,
        cmd: list[str],
        workdir: Path,
        log_path: Path | None = None,
        timeout_seconds: int = 1800,
    ) -> subprocess.Popen[bytes]:
        """Spawn a command on the remote host via SSH.

        The command runs in the remote session directory. Stdout and stderr
        are streamed back via the SSH connection. If ``log_path`` is given,
        output is tee'd to that file.

        Args:
            cmd: Command to execute on the remote host.
            workdir: Local workdir (used to resolve relative paths only;
                the command runs in the remote session directory).
            log_path: Optional local file to capture combined stdout/stderr.
            _timeout_seconds: Process timeout (part of interface, tracked
                externally by the caller).

        Returns:
            The :class:`subprocess.Popen` object for the SSH process.

        Raises:
            SSHError: If the SSH process cannot be started.
        """
        _ = timeout_seconds  # Part of interface; tracked externally by the caller
        remote_cmd = self._wrap_remote_cmd(cmd)
        full_cmd = [*self._ssh_cmd(), self._config.ssh_target(), remote_cmd]

        stdout: int | None = None
        stderr: int | None = None

        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fh = log_path.open("wb")
            stdout = log_fh.fileno()
            stderr = log_fh.fileno()

        logger.info(
            "ssh-backend: spawning on %s — session=%s cmd=%s",
            self._config.host,
            self._session_id,
            shlex.join(cmd[:3]),
        )
        try:
            proc = subprocess.Popen(
                full_cmd,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        except OSError as exc:
            raise SSHError(f"Failed to start SSH process: {exc}") from exc

        return proc

    def cleanup_remote(self) -> None:
        """Remove the remote session directory.

        This is a best-effort operation; errors are logged but not raised.
        """
        cmd = [*self._ssh_cmd(), "rm", "-rf", self._remote_session_dir]
        try:
            self._run(cmd, desc="cleanup remote dir")
        except SSHError:
            logger.warning("ssh-backend: failed to clean up remote dir %s", self._remote_session_dir)

    def test_connectivity(self) -> bool:
        """Return True if the remote host is reachable via SSH.

        Performs a no-op SSH command (``true``) with a short timeout.

        Returns:
            True if the connection succeeded, False otherwise.
        """
        cmd = [*self._ssh_cmd(), self._config.ssh_target(), "true"]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=self._config.connect_timeout)
            return result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ssh_cmd(self) -> list[str]:
        """Build the base ssh command (without the remote host or command)."""
        args = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={self._config.connect_timeout}",
            "-o",
            "BatchMode=yes",
            "-p",
            str(self._config.port),
        ]
        if self._config.key:
            key_path = os.path.expanduser(self._config.key)
            args += ["-i", key_path]
        return args

    def _rsync_cmd(self, *, src: str, dest: str) -> list[str]:
        """Build an rsync command with the configured excludes."""
        args = [
            "rsync",
            "-az",
            "--delete",
            f"--rsh=ssh -p {self._config.port} -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
        ]
        if self._config.key:
            key_path = os.path.expanduser(self._config.key)
            args[3] += f" -i {key_path}"
        for pattern in self._config.all_rsync_excludes():
            args += ["--exclude", pattern]
        args += [src, dest]
        return args

    def _wrap_remote_cmd(self, cmd: list[str]) -> str:
        """Wrap the agent command with cd + env exports for the remote shell.

        Args:
            cmd: The agent command to run remotely.

        Returns:
            A shell command string suitable for ``ssh host <cmd>``.
        """
        parts: list[str] = [f"cd {shlex.quote(self._remote_session_dir)}"]

        env = self._config.resolved_env()
        for key, value in env.items():
            parts.append(f"export {key}={shlex.quote(value)}")

        parts.append(shlex.join(cmd))
        return " && ".join(parts)

    @staticmethod
    def _run(cmd: list[str], *, desc: str, timeout: int = 120) -> None:
        """Run a subprocess and raise SSHError on failure.

        Args:
            cmd: Command to execute.
            desc: Human-readable description for error messages.
            timeout: Timeout in seconds.

        Raises:
            SSHError: If the command exits non-zero or times out.
        """
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise SSHError(f"{desc}: timed out after {timeout}s") from exc
        except OSError as exc:
            raise SSHError(f"{desc}: OS error — {exc}") from exc
        if result.returncode != 0:
            stderr_text = result.stderr.decode(errors="replace").strip()
            raise SSHError(f"{desc}: exit {result.returncode} — {stderr_text[:300]}")


def run_agent_over_ssh(
    *,
    config: SSHHostConfig,
    session_id: str,
    cmd: list[str],
    workdir: Path,
    log_path: Path | None = None,
    timeout_seconds: int = 1800,
    cleanup_after: bool = True,
) -> int:
    """High-level helper: sync, spawn, wait, sync back, return exit code.

    This is the simplest integration point for callers that just want to
    run an agent remotely and get the result back.

    Args:
        config: SSH host configuration.
        session_id: Unique identifier for the agent session.
        cmd: Agent command to run remotely.
        workdir: Local project root.
        log_path: Optional path for SSH output capture.
        timeout_seconds: Maximum time the agent may run.
        cleanup_after: Remove remote session directory when done.

    Returns:
        Exit code of the remote agent process.

    Raises:
        SSHError: If sync or SSH operations fail.
    """
    backend = SSHBackend(config, session_id=session_id)

    backend.ensure_remote_dir()
    backend.sync_to_remote(workdir)

    proc = backend.spawn(cmd=cmd, workdir=workdir, log_path=log_path, timeout_seconds=timeout_seconds)

    deadline = time.monotonic() + timeout_seconds
    exit_code: int | None = None
    while exit_code is None:
        exit_code = proc.poll()
        if exit_code is not None:
            break
        if time.monotonic() > deadline:
            proc.kill()
            logger.warning("ssh-backend: agent timed out after %ds — killed", timeout_seconds)
            exit_code = -1
            break
        time.sleep(_POLL_INTERVAL_S)

    backend.sync_from_remote(workdir)

    if cleanup_after:
        backend.cleanup_remote()

    return exit_code
