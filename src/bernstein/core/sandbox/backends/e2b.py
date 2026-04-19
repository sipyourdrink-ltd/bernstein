"""E2B cloud sandbox backend (optional extra).

E2B runs agent workloads inside ephemeral Firecracker microVMs. It is
an optional Bernstein extra: ``pip install bernstein[e2b]`` pulls in
``e2b_code_interpreter`` and registers this backend. When the SDK is
not installed the module still imports cleanly — instantiation is
where the error surfaces.

Snapshots are supported by the E2B SDK; the backend therefore declares
:attr:`~bernstein.core.sandbox.backend.SandboxCapability.SNAPSHOT` so
callers can persist state across orchestrator restarts.

This implementation ships in phase 1 as a functional skeleton: the
actual wiring against a live E2B session is exercised by integration
tests gated on ``E2B_API_KEY``; unit tests rely on mocks.

Scaffolding that is structurally identical to
:mod:`bernstein.core.sandbox.backends.modal` lives in
:mod:`bernstein.core.sandbox.backends._remote_helpers`; this module
keeps only the E2B-specific SDK import and API-attribute probing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from bernstein.core.sandbox.backend import (
    ExecResult,
    SandboxCapability,
    SandboxSession,
)
from bernstein.core.sandbox.backends._remote_helpers import (
    allocate_session_id,
    encode_as_bytes,
    guard_exec_preconditions,
    merge_exec_env,
    resolve_posix_path,
    resolve_sdk_attr,
    run_exec_with_timeout,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.sandbox.manifest import WorkspaceManifest

logger = logging.getLogger(__name__)


class E2BUnavailableError(RuntimeError):
    """Raised when the ``e2b_code_interpreter`` SDK is not installed."""


def _import_e2b() -> Any:
    """Import the E2B SDK lazily. Keeps the module safe to import without deps."""
    try:
        import e2b_code_interpreter  # type: ignore[import-not-found]
    except ImportError as exc:
        raise E2BUnavailableError("Install the 'e2b' extra: `pip install bernstein[e2b]`") from exc
    return e2b_code_interpreter


def _e2b_filesystem(sandbox: Any) -> Any:
    """Return the filesystem handle exposed by the installed E2B SDK version."""
    fs = resolve_sdk_attr(sandbox, "files", "filesystem")
    if fs is None:
        raise RuntimeError("E2B SDK did not expose a filesystem interface")
    return fs


class E2BSandboxSession(SandboxSession):
    """A session backed by an E2B microVM sandbox."""

    backend_name = "e2b"

    def __init__(
        self,
        *,
        session_id: str,
        sandbox: Any,
        workdir: str,
        base_env: Mapping[str, str],
        default_timeout: int,
    ) -> None:
        self.session_id = session_id
        self.workdir = workdir
        self._sandbox = sandbox
        self._base_env = dict(base_env)
        self._default_timeout = default_timeout
        self._closed = False

    async def read(self, path: str) -> bytes:
        resolved = resolve_posix_path(self.workdir, path)

        def _do_read() -> bytes:
            # The E2B SDK exposes either ``files.read`` or ``filesystem.read``
            # depending on the version; both return bytes or str.
            return encode_as_bytes(_e2b_filesystem(self._sandbox).read(resolved))

        return await asyncio.to_thread(_do_read)

    async def write(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        resolved = resolve_posix_path(self.workdir, path)

        def _do_write() -> None:
            fs = _e2b_filesystem(self._sandbox)
            fs.write(resolved, data)
            # The E2B SDK does not expose chmod explicitly in every
            # version; skip best-effort rather than fail if missing.
            chmod = getattr(fs, "chmod", None)
            if chmod is not None:
                try:
                    chmod(resolved, mode)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("E2B chmod %o on %s failed: %s", mode, resolved, exc)

        await asyncio.to_thread(_do_write)

    async def ls(self, path: str) -> list[str]:
        resolved = resolve_posix_path(self.workdir, path)

        def _do_ls() -> list[str]:
            entries = _e2b_filesystem(self._sandbox).list(resolved)
            names = [getattr(e, "name", str(e)) for e in entries]
            return sorted(names)

        return await asyncio.to_thread(_do_ls)

    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        guard_exec_preconditions(self._closed, self.session_id, cmd)
        effective_cwd = cwd if cwd is not None else self.workdir
        effective_timeout = timeout if timeout is not None else self._default_timeout
        merged_env = merge_exec_env(self._base_env, env)

        def _run() -> tuple[int, bytes, bytes]:
            commands = resolve_sdk_attr(self._sandbox, "commands", "process")
            if commands is None:
                raise RuntimeError("E2B SDK did not expose a commands interface")
            result = commands.run(
                cmd if isinstance(cmd, str) else " ".join(_quote(part) for part in cmd),
                cwd=effective_cwd,
                envs=merged_env,
                timeout=effective_timeout,
            )
            exit_code = int(getattr(result, "exit_code", 0) or 0)
            stdout = getattr(result, "stdout", "") or ""
            stderr = getattr(result, "stderr", "") or ""
            return (exit_code, encode_as_bytes(stdout), encode_as_bytes(stderr))

        return await run_exec_with_timeout(_run, cmd=cmd, timeout=effective_timeout)

    async def snapshot(self) -> str:
        def _do_snapshot() -> str:
            pause = getattr(self._sandbox, "pause", None)
            if pause is not None:
                snap_id = pause()
                return str(snap_id) if snap_id is not None else self.session_id
            raise NotImplementedError("Installed E2B SDK does not expose a snapshot entry point")

        return await asyncio.to_thread(_do_snapshot)

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True

        def _do_shutdown() -> None:
            kill = resolve_sdk_attr(self._sandbox, "kill", "close")
            if kill is None:
                logger.debug("E2B SDK did not expose a shutdown entry point")
                return
            try:
                kill()
            except Exception as exc:
                logger.debug("E2B shutdown raised: %s", exc)

        await asyncio.to_thread(_do_shutdown)


def _quote(arg: str) -> str:
    """Shell-quote a single argv element for E2B's string exec API."""
    if not arg:
        return "''"
    safe = all(ch.isalnum() or ch in "@%+=:,./-" for ch in arg)
    if safe:
        return arg
    return "'" + arg.replace("'", "'\\''") + "'"


class E2BSandboxBackend:
    """Cloud SandboxBackend powered by E2B."""

    name = "e2b"
    capabilities: frozenset[SandboxCapability] = frozenset(
        {
            SandboxCapability.FILE_RW,
            SandboxCapability.EXEC,
            SandboxCapability.NETWORK,
            SandboxCapability.SNAPSHOT,
        }
    )

    def __init__(self, *, client_factory: Any | None = None) -> None:
        """Create the backend.

        Args:
            client_factory: Optional callable used by tests to build an
                E2B Sandbox object. Defaults to constructing one via
                the real SDK at ``create`` time so importing this
                module doesn't require the SDK.
        """
        self._client_factory = client_factory
        self._sessions: dict[str, E2BSandboxSession] = {}

    async def create(
        self,
        manifest: WorkspaceManifest,
        options: dict[str, Any] | None = None,
    ) -> SandboxSession:
        """Provision a fresh E2B sandbox per *manifest*.

        Recognised ``options``:

        - ``template``: E2B template name. Default ``code-interpreter-v1``.
        - ``api_key``: Override E2B_API_KEY. Default reads from env.
        - ``session_id``: Explicit session identifier.
        """
        opts = dict(options or {})
        template = opts.get("template", "code-interpreter-v1")
        api_key = opts.get("api_key")
        session_id = allocate_session_id("bernstein-e2b", opts.get("session_id"))

        def _build() -> Any:
            if self._client_factory is not None:
                return self._client_factory(
                    template=template,
                    api_key=api_key,
                    manifest=manifest,
                )
            module = _import_e2b()
            sandbox_cls = resolve_sdk_attr(module, "Sandbox", "AsyncSandbox")
            if sandbox_cls is None:
                raise E2BUnavailableError("E2B SDK missing Sandbox class")
            kwargs: dict[str, Any] = {"template": template}
            if api_key:
                kwargs["api_key"] = api_key
            return sandbox_cls(**kwargs)

        sandbox = await asyncio.to_thread(_build)
        session = E2BSandboxSession(
            session_id=session_id,
            sandbox=sandbox,
            workdir=manifest.root,
            base_env=manifest.env,
            default_timeout=manifest.timeout_seconds,
        )
        for entry in manifest.files:
            await session.write(entry.path, entry.content, mode=entry.mode)
        self._sessions[session_id] = session
        return session

    async def resume(self, snapshot_id: str) -> SandboxSession:
        if self._client_factory is not None:
            sandbox = self._client_factory(resume=snapshot_id)
        else:
            module = _import_e2b()
            sandbox_cls = getattr(module, "Sandbox", None)
            if sandbox_cls is None:
                raise E2BUnavailableError("E2B SDK missing Sandbox class")
            resume_method = getattr(sandbox_cls, "resume", None)
            if resume_method is None:
                raise NotImplementedError("Installed E2B SDK does not expose a resume entry point")
            sandbox = resume_method(snapshot_id)
        session = E2BSandboxSession(
            session_id=snapshot_id,
            sandbox=sandbox,
            workdir="/home/user",
            base_env={},
            default_timeout=1800,
        )
        self._sessions[snapshot_id] = session
        return session

    async def destroy(self, session: SandboxSession) -> None:
        await session.shutdown()
        self._sessions.pop(session.session_id, None)


__all__ = [
    "E2BSandboxBackend",
    "E2BSandboxSession",
    "E2BUnavailableError",
]
