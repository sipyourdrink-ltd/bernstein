"""SSH-backed :class:`SandboxBackend` - run agents on remote hosts.

This backend lets Bernstein provision a sandbox on an arbitrary host
reachable over SSH. Every primitive (``read`` / ``write`` / ``exec`` /
``ls``) is implemented by spawning the system ``ssh`` binary against a
long-lived ``ControlMaster`` multiplexing socket so subsequent commands
pay only the TCP-round-trip cost, not a full key exchange.

Design notes:

* Pure shell-out over ``ssh``/``sftp``; no third-party transport library
  is required. ``paramiko`` is *not* a dependency.
* File I/O uses plain shell commands (``cat``, ``base64``, ``test``)
  rather than ``sftp`` so the same connection multiplex is reused. Large
  payloads are base64-wrapped to survive 8-bit-unsafe shells.
* A dedicated ``ControlMaster`` socket is opened per
  :class:`SSHSandboxBackend` instance at
  ``~/.ssh/bernstein-<host>-<pid>.sock``; :meth:`close` tears it down.
* Worktree lifecycle (``create`` / ``attach`` / ``destroy``) runs
  ``git worktree`` remotely over the same multiplexed channel.
* Connection-level failures (``Connection refused`` / ``Permission
  denied``) are translated into :class:`SandboxConnectionError` so
  callers can render ergonomic fix-it messages.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import secrets
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from bernstein.core.sandbox.backend import (
    ExecResult,
    SandboxCapability,
    SandboxSession,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.sandbox.manifest import WorkspaceManifest

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_SECONDS = 10
_SERVER_ALIVE_INTERVAL_SECONDS = 30
_CONTROL_PERSIST_SECONDS = 600
_DEFAULT_TIMEOUT_SLACK = 5

#: A remote-path segment (session id / worktree dir name) must match this so it
#: can never escape the remote worktree root or be read as an ``ssh`` flag.
_SESSION_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_session_component(component: str) -> str:
    """Return *component* when it is a safe single remote-path segment.

    Rejects the empty string, ``.``/``..``, and anything carrying a path
    separator or a leading dash, so a caller-supplied session id can never
    traverse out of ``<path>/<session_id>`` or be mistaken for an option
    (CodeQL py/path-injection).

    Args:
        component: The candidate remote-path segment.

    Returns:
        The validated component, unchanged.

    Raises:
        ValueError: If *component* is not a safe single segment.
    """
    if component in {"", ".", ".."} or _SESSION_COMPONENT_RE.match(component) is None:
        raise ValueError(f"unsafe session id {component!r}: must match [A-Za-z0-9][A-Za-z0-9._-]*")
    return component


class SandboxConnectionError(RuntimeError):
    """Raised when the sandbox's remote transport is unreachable.

    Attributes:
        host: Remote host the backend was trying to reach.
        reason: Short human-readable reason (``"connection refused"``,
            ``"permission denied"``, etc.).
        hint: Optional actionable suggestion shown alongside ``reason``.
    """

    def __init__(self, *, host: str, reason: str, hint: str | None = None) -> None:
        self.host = host
        self.reason = reason
        self.hint = hint
        message = f"SSH sandbox unreachable ({host}): {reason}"
        if hint:
            message = f"{message} - {hint}"
        super().__init__(message)


def _classify_ssh_failure(host: str, stderr: str) -> SandboxConnectionError | None:
    """Map a stderr blob from ``ssh`` to a :class:`SandboxConnectionError`.

    Args:
        host: Remote host used in the error message.
        stderr: Captured ``ssh`` stderr.

    Returns:
        A prepared exception if ``stderr`` matches one of the known
        failure signatures, else ``None``.
    """
    lowered = stderr.lower()
    if "connection refused" in lowered:
        return SandboxConnectionError(
            host=host,
            reason="connection refused",
            hint=f"is sshd running on {host}? check firewall and `Port` in ~/.ssh/config",
        )
    if "permission denied" in lowered:
        return SandboxConnectionError(
            host=host,
            reason="permission denied",
            hint="run `ssh-add <key>` and confirm the `IdentityFile` in ~/.ssh/config",
        )
    if "could not resolve hostname" in lowered or "name or service not known" in lowered:
        return SandboxConnectionError(
            host=host,
            reason="host not resolvable",
            hint=f"add a `Host {host}` block to ~/.ssh/config or use --user/--port flags",
        )
    if "operation timed out" in lowered or "connection timed out" in lowered:
        return SandboxConnectionError(
            host=host,
            reason="connection timed out",
            hint="check network reachability and any VPN requirements",
        )
    return None


@dataclass(frozen=True)
class RemoteExec:
    """Raw outcome of one ssh transport call.

    Attributes:
        returncode: Exit code of the remote process (``-1`` if it never
            reported one).
        stdout: Raw bytes the remote process wrote to stdout.
        stderr: Raw bytes the remote process wrote to stderr.
    """

    returncode: int
    stdout: bytes
    stderr: bytes


@runtime_checkable
class SSHTransport(Protocol):
    """The external ssh boundary: run a prepared ``ssh`` argv and report back.

    The backend owns argv assembly (options, the ``[user@]host`` target, the
    ``sh -c`` wrapping); the transport owns only the process hop. The default
    transport shells out to the system ``ssh`` binary. A test or an embedding
    host can inject an in-process transport so the backend's real logic -- the
    base64 file transfer and the remote ``git worktree`` lifecycle -- runs
    without a live remote, which is how the ssh backend is proven off-host
    (issue #2352, AC4).
    """

    def run_blocking(self, argv: list[str]) -> RemoteExec:
        """Run a control-plane call (open/close the ControlMaster) and wait."""
        ...

    async def run_async(
        self,
        argv: list[str],
        *,
        timeout_seconds: int | None = None,
        stdin: bytes | None = None,
    ) -> RemoteExec:
        """Run a remote command over ssh and collect its result.

        Raises:
            TimeoutError: When *timeout_seconds* elapses before the command
                finishes.
        """
        ...

    def popen(
        self,
        argv: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> subprocess.Popen[bytes]:
        """Spawn a streaming remote command and return the live client handle."""
        ...


class _SubprocessSSHTransport:
    """Default :class:`SSHTransport`: shell out to the system ``ssh`` binary.

    Stateless -- the backend keeps every connection detail and hands over a
    fully-built argv, so this class only ever runs processes.
    """

    def run_blocking(self, argv: list[str]) -> RemoteExec:
        proc = subprocess.run(argv, capture_output=True, check=False)
        return RemoteExec(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    async def run_async(
        self,
        argv: list[str],
        *,
        timeout_seconds: int | None = None,
        stdin: bytes | None = None,
    ) -> RemoteExec:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        deadline = timeout_seconds + _DEFAULT_TIMEOUT_SLACK if timeout_seconds else None
        try:
            async with asyncio.timeout(deadline):
                stdout, stderr = await process.communicate(input=stdin)
        except TimeoutError:
            process.kill()
            try:
                async with asyncio.timeout(5):
                    await process.wait()
            except TimeoutError:
                logger.warning("ssh process did not exit after kill: %s", argv)
            raise TimeoutError(f"ssh command timed out after {timeout_seconds}s") from None
        return RemoteExec(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
        )

    def popen(
        self,
        argv: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(argv, stdin=stdin, stdout=stdout, stderr=stderr)


class SSHSandboxSession(SandboxSession):
    """An active sandbox on a remote host reached over SSH.

    Instances are produced by :meth:`SSHSandboxBackend.create`; manual
    construction is supported but most callers should go through the
    backend so the ``git worktree`` lifecycle runs.
    """

    backend_name = "ssh"

    def __init__(
        self,
        *,
        backend: SSHSandboxBackend,
        session_id: str,
        workdir: str,
        base_env: Mapping[str, str],
        default_timeout: int,
    ) -> None:
        """Initialise the session.

        Args:
            backend: Owning backend; used for shared SSH argv and the
                ``git worktree`` cleanup on :meth:`shutdown`.
            session_id: Opaque identifier - also the worktree directory
                name under the remote ``path``.
            workdir: Absolute POSIX path on the remote host of the
                session's root directory.
            base_env: Environment applied to every :meth:`exec` call.
            default_timeout: Wall-clock timeout applied when the caller
                does not supply one.
        """
        self._backend = backend
        self.session_id = session_id
        self.workdir = workdir
        self._base_env = dict(base_env)
        self._default_timeout = default_timeout
        self._closed = False

    # ------------------------------------------------------------------
    # File primitives
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> str:
        """Resolve ``path`` against :attr:`workdir` POSIX-style."""
        candidate = PurePosixPath(path)
        if candidate.is_absolute():
            return str(candidate)
        return str(PurePosixPath(self.workdir) / candidate)

    async def read(self, path: str) -> bytes:
        """Read a file from the remote host as bytes.

        Uses ``base64`` on the remote end so binary payloads survive the
        SSH stdout stream unchanged.
        """
        target = self._resolve(path)
        cmd = f"test -e {shlex.quote(target)} && base64 < {shlex.quote(target)}"
        result = await self._backend.run_ssh(cmd)
        if result.exit_code != 0:
            raise FileNotFoundError(target)
        return base64.b64decode(result.stdout)

    async def write(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        """Write ``data`` to ``path`` on the remote host.

        Parent directories are created as needed. The payload is
        base64-encoded so shell framing cannot damage binary content.
        """
        target = self._resolve(path)
        payload = base64.b64encode(data).decode("ascii")
        parent = str(PurePosixPath(target).parent)
        script = (
            f"mkdir -p {shlex.quote(parent)} && "
            f"printf '%s' {shlex.quote(payload)} | base64 -d > {shlex.quote(target)} && "
            f"chmod {mode:o} {shlex.quote(target)}"
        )
        result = await self._backend.run_ssh(script)
        if result.exit_code != 0:
            raise OSError(
                f"remote write {target!r} failed (exit={result.exit_code}): "
                f"{result.stderr.decode('utf-8', errors='replace').strip()}"
            )

    async def ls(self, path: str) -> list[str]:
        """List directory entries on the remote host.

        Returns:
            Sorted list of entry basenames. Hidden entries (``.``/``..``)
            are omitted to match the local worktree's semantics.
        """
        target = self._resolve(path)
        script = f"test -d {shlex.quote(target)} || exit 78; ls -A -1 {shlex.quote(target)}"
        result = await self._backend.run_ssh(script)
        if result.exit_code == 78:
            raise NotADirectoryError(target)
        if result.exit_code != 0:
            raise OSError(f"remote ls {target!r} failed: {result.stderr.decode('utf-8', errors='replace').strip()}")
        names = result.stdout.decode("utf-8", errors="replace").splitlines()
        return sorted(name for name in names if name)

    async def exists(self, path: str) -> bool:
        """Return ``True`` if ``path`` exists on the remote host."""
        target = self._resolve(path)
        result = await self._backend.run_ssh(f"test -e {shlex.quote(target)}")
        return result.exit_code == 0

    # ------------------------------------------------------------------
    # Exec primitive
    # ------------------------------------------------------------------

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        """Execute ``cmd`` on the remote host."""
        if self._closed:
            raise RuntimeError(f"Session {self.session_id} is closed")
        if not cmd:
            raise ValueError("cmd must be a non-empty argv list")
        effective_cwd = self._resolve(cwd) if cwd is not None else self.workdir
        effective_timeout = timeout if timeout is not None else self._default_timeout
        merged_env: dict[str, str] = self._base_env.copy()
        if env:
            merged_env.update(env)

        quoted_cmd = shlex.join(cmd)
        env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in merged_env.items())
        script = f"cd {shlex.quote(effective_cwd)} && "
        if env_prefix:
            script += f"env {env_prefix} {quoted_cmd}"
        else:
            script += quoted_cmd

        return await self._backend.run_ssh(script, timeout_seconds=effective_timeout, stdin=stdin)

    # ------------------------------------------------------------------
    # Snapshots (unsupported)
    # ------------------------------------------------------------------

    async def snapshot(self) -> str:
        """SSH backend does not advertise the SNAPSHOT capability."""
        raise NotImplementedError("SSHSandboxBackend does not declare the SNAPSHOT capability")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Remove the remote worktree and mark the session closed.

        Idempotent - repeat calls are no-ops.
        """
        if self._closed:
            return
        self._closed = True
        try:
            await self._backend.destroy_remote_worktree(self.workdir)
        except Exception as exc:
            logger.warning("ssh shutdown: remote cleanup failed for %s: %s", self.workdir, exc)


class SSHSandboxBackend:
    """:class:`SandboxBackend` that provisions sandboxes over SSH.

    The backend shells out to the system ``ssh`` binary and multiplexes
    every connection through a persistent ``ControlMaster`` socket. A
    single backend instance may serve many sequential sessions against
    the same host; call :meth:`close` to tear the socket down.
    """

    name = "ssh"
    capabilities: frozenset[SandboxCapability] = frozenset(
        {
            SandboxCapability.FILE_RW,
            SandboxCapability.EXEC,
            SandboxCapability.NETWORK,
        }
    )

    def __init__(
        self,
        host: str,
        user: str | None = None,
        *,
        path: str,
        identity_file: str | Path | None = None,
        port: int = 22,
        strict_host_key_checking: bool = True,
        transport: SSHTransport | None = None,
    ) -> None:
        """Initialise the backend.

        Args:
            host: Remote host as it would appear on the ``ssh`` CLI
                (may refer to an entry in ``~/.ssh/config``).
            user: Optional remote username. When ``None`` the local
                ``ssh`` client's default is used.
            path: Absolute POSIX path on the remote host where session
                worktrees are provisioned (``<path>/<session_id>``).
            identity_file: Optional path to a private key.
            port: Remote SSH port. Defaults to 22.
            strict_host_key_checking: When ``True`` (default) the
                corresponding ``StrictHostKeyChecking`` flag is passed
                as ``yes``; ``False`` sets it to ``accept-new``.
            transport: The ssh transport to run prepared argv through.
                Defaults to :class:`_SubprocessSSHTransport` (the system
                ``ssh`` binary). Inject an in-process transport to drive the
                backend without a live host.
        """
        if not host:
            raise ValueError("host must be non-empty")
        if not path:
            raise ValueError("path must be non-empty")
        self._host = host
        self._user = user
        self._path = path
        self._identity_file = Path(identity_file) if identity_file is not None else None
        self._port = port
        self._strict_host_key_checking = strict_host_key_checking
        self._transport: SSHTransport = transport if transport is not None else _SubprocessSSHTransport()
        self._sessions: dict[str, SSHSandboxSession] = {}
        self._control_socket = self._build_control_socket_path(host, os.getpid())
        self._master_started = False

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------

    @property
    def host(self) -> str:
        """Return the remote host."""
        return self._host

    @property
    def control_socket(self) -> Path:
        """Return the :class:`Path` to the ControlMaster socket."""
        return self._control_socket

    @staticmethod
    def _build_control_socket_path(host: str, pid: int) -> Path:
        """Deterministic ControlMaster socket path for ``(host, pid)``."""
        safe_host = host.replace("/", "_").replace(":", "_")
        return Path.home() / ".ssh" / f"bernstein-{safe_host}-{pid}.sock"

    def _remote_target(self) -> str:
        """Return the ``[user@]host`` tuple for ``ssh`` / ``sftp``."""
        return f"{self._user}@{self._host}" if self._user else self._host

    # ------------------------------------------------------------------
    # argv assembly
    # ------------------------------------------------------------------

    def _common_ssh_options(self) -> list[str]:
        """Return the ``-o`` flags every ``ssh`` invocation receives."""
        strict_value = "yes" if self._strict_host_key_checking else "accept-new"
        options: list[str] = [
            "-o",
            f"ConnectTimeout={_CONNECT_TIMEOUT_SECONDS}",
            "-o",
            f"ServerAliveInterval={_SERVER_ALIVE_INTERVAL_SECONDS}",
            "-o",
            f"StrictHostKeyChecking={strict_value}",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={self._control_socket}",
            "-o",
            f"ControlPersist={_CONTROL_PERSIST_SECONDS}",
            "-p",
            str(self._port),
        ]
        if self._identity_file is not None:
            options.extend(["-i", str(self._identity_file)])
        return options

    def _build_ssh_cmd(self, cmd: str) -> list[str]:
        """Return the full ``ssh`` argv for ``cmd``.

        Args:
            cmd: Shell command to run on the remote host. It is wrapped
                in ``sh -c "…"`` so POSIX features (pipes, redirection)
                work the same way across shells.

        Returns:
            The ready-to-spawn argv list.
        """
        argv: list[str] = ["ssh", *self._common_ssh_options(), self._remote_target()]
        argv.append(f"sh -c {shlex.quote(cmd)}")
        return argv

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def ensure_control_master(self) -> None:
        """Open the shared ``ControlMaster`` socket if not already up.

        Idempotent - safe to call before every command. The resulting
        socket persists for ``ControlPersist`` seconds even after this
        process exits so rapid re-runs stay cheap.
        """
        if self._master_started and self._control_socket.exists():
            return
        self._control_socket.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            "ssh",
            *self._common_ssh_options(),
            "-fN",
            "-M",
            self._remote_target(),
        ]
        logger.debug("ssh: opening ControlMaster socket %s", self._control_socket)
        result = self._transport.run_blocking(argv)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            classified = _classify_ssh_failure(self._host, stderr)
            if classified is not None:
                raise classified
            raise RuntimeError(f"failed to open SSH ControlMaster socket: {stderr.strip()}")
        self._master_started = True

    def close(self) -> None:
        """Close the ``ControlMaster`` socket if this backend owns it.

        Idempotent. Any error from the underlying ``ssh -O exit`` call
        is logged and swallowed so callers can use this safely in
        cleanup paths.
        """
        if not self._master_started and not self._control_socket.exists():
            return
        argv = [
            "ssh",
            *self._common_ssh_options(),
            "-O",
            "exit",
            self._remote_target(),
        ]
        try:
            self._transport.run_blocking(argv)
        except OSError as exc:
            logger.debug("ssh -O exit: %s", exc)
        # Best-effort socket removal - OpenSSH usually clears it itself.
        try:
            self._control_socket.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("socket unlink failed: %s", exc)
        self._master_started = False

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def run_ssh(
        self,
        cmd: str,
        *,
        timeout_seconds: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        """Run ``cmd`` on the remote host and collect the result."""
        self.ensure_control_master()
        argv = self._build_ssh_cmd(cmd)
        start = time.monotonic()
        result = await self._transport.run_async(argv, timeout_seconds=timeout_seconds, stdin=stdin)
        duration = time.monotonic() - start
        exit_code = result.returncode
        if exit_code != 0:
            classified = _classify_ssh_failure(self._host, result.stderr.decode("utf-8", errors="replace"))
            if classified is not None:
                raise classified
        return ExecResult(
            exit_code=exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=duration,
        )

    def spawn_agent(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start ``cmd`` on the remote host and return a live handle.

        Mirrors :func:`subprocess.Popen` so existing callers that
        previously spawned adapter CLIs locally can route them through
        an SSH tunnel with minimal changes.

        Args:
            cmd: Argv for the remote process.
            cwd: Optional remote working directory.
            env: Extra environment variables exported before ``cmd``.
            stdin: Forwarded to :class:`subprocess.Popen`.
            stdout: Forwarded to :class:`subprocess.Popen`.
            stderr: Forwarded to :class:`subprocess.Popen`.

        Returns:
            A :class:`subprocess.Popen` wrapping the local ``ssh``
            client process; its ``stdout`` is the remote ``cmd``'s
            stdout stream.
        """
        if not cmd:
            raise ValueError("cmd must be a non-empty argv list")
        self.ensure_control_master()
        env_prefix = ""
        if env:
            env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items()) + " "
        cd_prefix = f"cd {shlex.quote(cwd)} && " if cwd else ""
        quoted = shlex.join(cmd)
        script = f"{cd_prefix}{env_prefix}{quoted}".strip()
        argv = self._build_ssh_cmd(script)
        return self._transport.popen(argv, stdin=stdin, stdout=stdout, stderr=stderr)

    # ------------------------------------------------------------------
    # Worktree lifecycle (remote)
    # ------------------------------------------------------------------

    async def create_remote_worktree(
        self,
        manifest: WorkspaceManifest,
        session_id: str,
        *,
        new_branch: str | None = None,
        reset: bool = False,
    ) -> str:
        """Provision a fresh remote worktree and return its POSIX path.

        When ``manifest.repo`` is set, ``git worktree add`` is executed on the
        remote host. Passing *new_branch* provisions the worktree on a brand-new
        branch cut from ``manifest.repo.branch`` (``git worktree add -b``), so
        many sessions can fan out from one base ref without the branch-per-tree
        collision that plain ``git worktree add <path> <branch>`` would hit --
        the isolation guarantee the detached run service leans on. Without a
        repo, a plain directory is created.

        With *reset*, provisioning is idempotent under resume: any stale
        worktree left at the path by a killed prior attempt is removed and the
        branch is created-or-reset (``git worktree add -B``). This lets a
        supervisor re-run a task that was killed mid-execution on the ssh
        backend without a branch/worktree collision, so a resumed run loses no
        work across the ssh boundary.

        Args:
            manifest: The workspace manifest; ``manifest.repo`` selects the
                git-worktree path over the plain-directory path.
            session_id: The remote worktree directory name; validated so it
                cannot escape ``<path>``.
            new_branch: Optional new branch name to create for this worktree.
            reset: When ``True``, clear any stale worktree/dir first and reset
                the branch instead of failing if it already exists.

        Returns:
            The absolute POSIX path of the provisioned worktree.
        """
        session_id = _validate_session_component(session_id)
        workdir = str(PurePosixPath(self._path) / session_id)
        quoted_workdir = shlex.quote(workdir)
        if manifest.repo is not None:
            repo_src = shlex.quote(manifest.repo.src_path)
            base = shlex.quote(manifest.repo.branch)
            cleanup = ""
            if reset:
                # Retire a stale worktree from a killed attempt (dir + admin
                # record) and prune any dangling record so `-B` can reuse the
                # branch. `prune` covers the case where the dir was already
                # removed but its admin record survived.
                cleanup = (
                    f"if [ -e {quoted_workdir} ]; then "
                    f"git -C {repo_src} worktree remove --force {quoted_workdir} 2>/dev/null "
                    f"|| rm -rf {quoted_workdir}; fi; "
                    f"git -C {repo_src} worktree prune 2>/dev/null || true; "
                )
            if new_branch is not None:
                branch_flag = "-B" if reset else "-b"
                add = f"git worktree add {branch_flag} {shlex.quote(new_branch)} {quoted_workdir} {base}"
            else:
                add = f"git worktree add {quoted_workdir} {base}"
            script = f"mkdir -p {shlex.quote(self._path)} && cd {repo_src} && {cleanup}{add}"
        else:
            pre = f"rm -rf {quoted_workdir} && " if reset else ""
            script = f"{pre}mkdir -p {quoted_workdir}"
        result = await self.run_ssh(script)
        if result.exit_code != 0:
            raise RuntimeError(
                f"failed to create remote worktree: {result.stderr.decode('utf-8', errors='replace').strip()}"
            )
        return workdir

    async def attach_remote_worktree(self, session_id: str) -> str:
        """Return the path to an existing remote worktree.

        Errors if no directory is present at the expected path.
        """
        workdir = str(PurePosixPath(self._path) / session_id)
        check = await self.run_ssh(f"test -d {shlex.quote(workdir)}")
        if check.exit_code != 0:
            raise FileNotFoundError(f"no remote worktree at {workdir}")
        return workdir

    async def destroy_remote_worktree(self, workdir: str) -> None:
        """Best-effort cleanup of a remote worktree directory.

        A git worktree's ``.git`` is a *file* (a gitdir pointer), not a
        directory, so ``git worktree remove`` is what actually retires the
        worktree's admin record in the source repo. Removing the directory
        alone would leave a dangling record that blocks the branch from being
        reused on a later resume, so the remove runs from inside the worktree
        (which resolves back to the source repo) before the directory is wiped.
        """
        script = (
            f"if [ -e {shlex.quote(workdir)}/.git ]; then "
            f"  git -C {shlex.quote(workdir)} worktree remove --force {shlex.quote(workdir)} 2>/dev/null || true; "
            f"fi; "
            f"rm -rf {shlex.quote(workdir)}"
        )
        await self.run_ssh(script)

    # ------------------------------------------------------------------
    # SandboxBackend protocol
    # ------------------------------------------------------------------

    async def create(
        self,
        manifest: WorkspaceManifest,
        options: dict[str, Any] | None = None,
    ) -> SandboxSession:
        """Provision a new remote session against ``manifest``.

        Recognised ``options`` keys:

        - ``session_id``: Pinned session identifier (validated as a safe
          single remote-path segment). Random when absent.
        - ``worktree_branch``: New branch to cut this worktree onto
          (``git worktree add -b``), so sessions sharing a base ref stay
          isolated. Ignored when the manifest has no repo.
        - ``worktree_reset``: When truthy, provision idempotently under resume
          (clear a stale worktree and reset the branch) instead of failing if
          it already exists.
        """
        opts = dict(options or {})
        hint = opts.get("session_id")
        session_id = (
            _validate_session_component(hint) if isinstance(hint, str) and hint else f"sbx-{secrets.token_hex(6)}"
        )
        branch_opt = opts.get("worktree_branch")
        new_branch = branch_opt if isinstance(branch_opt, str) and branch_opt else None
        reset = bool(opts.get("worktree_reset"))
        workdir = await self.create_remote_worktree(manifest, session_id, new_branch=new_branch, reset=reset)

        if manifest.files:
            for entry in manifest.files:
                target = str(PurePosixPath(workdir) / entry.path)
                payload = base64.b64encode(entry.content).decode("ascii")
                parent = str(PurePosixPath(target).parent)
                script = (
                    f"mkdir -p {shlex.quote(parent)} && "
                    f"printf '%s' {shlex.quote(payload)} | base64 -d > {shlex.quote(target)} && "
                    f"chmod {entry.mode:o} {shlex.quote(target)}"
                )
                await self.run_ssh(script)

        session = SSHSandboxSession(
            backend=self,
            session_id=session_id,
            workdir=workdir,
            base_env=manifest.env,
            default_timeout=manifest.timeout_seconds,
        )
        self._sessions[session_id] = session
        return session

    async def resume(self, snapshot_id: str) -> SandboxSession:
        """SSH backend does not support snapshot/resume."""
        raise NotImplementedError("SSHSandboxBackend does not declare the SNAPSHOT capability")

    async def destroy(self, session: SandboxSession) -> None:
        """Shut down ``session`` and drop it from the internal table."""
        await session.shutdown()
        self._sessions.pop(session.session_id, None)


__all__ = [
    "RemoteExec",
    "SSHSandboxBackend",
    "SSHSandboxSession",
    "SSHTransport",
    "SandboxConnectionError",
]
