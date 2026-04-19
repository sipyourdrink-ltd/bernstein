"""Modal cloud sandbox backend (optional extra).

Modal provides serverless container execution with GPU support, which
makes it the natural choice for ML-heavy tasks. This backend runs each
session inside a Modal Sandbox; the Modal SDK is pulled in via the
``[modal]`` extra.

Snapshots are supported by Modal sandboxes so the backend declares
:attr:`~bernstein.core.sandbox.backend.SandboxCapability.SNAPSHOT`.

Scaffolding that is structurally identical to
:mod:`bernstein.core.sandbox.backends.e2b` lives in
:mod:`bernstein.core.sandbox.backends._remote_helpers`; this module
keeps only the Modal-specific SDK import and API-attribute probing.
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


class ModalUnavailableError(RuntimeError):
    """Raised when the ``modal`` SDK is not installed."""


def _import_modal() -> Any:
    try:
        import modal  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ModalUnavailableError("Install the 'modal' extra: `pip install bernstein[modal]`") from exc
    return modal


class ModalSandboxSession(SandboxSession):
    """Session backed by a Modal sandbox."""

    backend_name = "modal"

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
            reader = resolve_sdk_attr(self._sandbox, "read_file", "open")
            if reader is None:
                raise RuntimeError("Modal SDK did not expose a file-read API")
            return encode_as_bytes(reader(resolved))

        return await asyncio.to_thread(_do_read)

    async def write(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        resolved = resolve_posix_path(self.workdir, path)

        def _do_write() -> None:
            writer = resolve_sdk_attr(self._sandbox, "write_file", "put")
            if writer is None:
                raise RuntimeError("Modal SDK did not expose a file-write API")
            writer(resolved, data)
            chmod = getattr(self._sandbox, "chmod", None)
            if chmod is not None:
                try:
                    chmod(resolved, mode)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("Modal chmod %o on %s failed: %s", mode, resolved, exc)

        await asyncio.to_thread(_do_write)

    async def ls(self, path: str) -> list[str]:
        resolved = resolve_posix_path(self.workdir, path)

        def _do_ls() -> list[str]:
            lister = resolve_sdk_attr(self._sandbox, "list_files", "ls")
            if lister is None:
                raise RuntimeError("Modal SDK did not expose a listing API")
            entries = lister(resolved)
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
            exec_fn = resolve_sdk_attr(self._sandbox, "exec", "run")
            if exec_fn is None:
                raise RuntimeError("Modal SDK did not expose an exec API")
            process = exec_fn(
                *cmd,
                workdir=effective_cwd,
                env=merged_env,
                timeout=effective_timeout,
            )
            # Modal sandboxes return a process handle with ``.wait()``,
            # ``.stdout``, ``.stderr`` streams; tests cover these via
            # mocks. Real integration tests live under the ``modal``
            # gate.
            exit_code = int(process.wait())
            stdout_val = getattr(process.stdout, "read", lambda: b"")()
            stderr_val = getattr(process.stderr, "read", lambda: b"")()
            return (exit_code, encode_as_bytes(stdout_val), encode_as_bytes(stderr_val))

        return await run_exec_with_timeout(_run, cmd=cmd, timeout=effective_timeout)

    async def snapshot(self) -> str:
        def _do_snapshot() -> str:
            snap = getattr(self._sandbox, "snapshot", None)
            if snap is None:
                raise NotImplementedError("Installed Modal SDK does not expose a snapshot entry point")
            return str(snap())

        return await asyncio.to_thread(_do_snapshot)

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True

        def _do_shutdown() -> None:
            terminate = resolve_sdk_attr(self._sandbox, "terminate", "close")
            if terminate is None:
                logger.debug("Modal SDK did not expose a terminate API")
                return
            try:
                terminate()
            except Exception as exc:
                logger.debug("Modal shutdown raised: %s", exc)

        await asyncio.to_thread(_do_shutdown)


class ModalSandboxBackend:
    """Cloud SandboxBackend powered by Modal."""

    name = "modal"
    capabilities: frozenset[SandboxCapability] = frozenset(
        {
            SandboxCapability.FILE_RW,
            SandboxCapability.EXEC,
            SandboxCapability.NETWORK,
            SandboxCapability.SNAPSHOT,
            SandboxCapability.GPU,
        }
    )

    def __init__(self, *, client_factory: Any | None = None) -> None:
        """Create the backend.

        Args:
            client_factory: Optional callable used by tests to build a
                Modal sandbox. Defaults to constructing one via the
                real SDK at ``create`` time.
        """
        self._client_factory = client_factory
        self._sessions: dict[str, ModalSandboxSession] = {}

    async def create(
        self,
        manifest: WorkspaceManifest,
        options: dict[str, Any] | None = None,
    ) -> SandboxSession:
        """Provision a fresh Modal sandbox per *manifest*.

        Recognised ``options``:

        - ``image``: Modal image reference (e.g. ``python:3.13``).
        - ``gpu``: Optional GPU type string (e.g. ``"A10G"``). Requires
          Modal plan that supports GPUs.
        - ``session_id``: Explicit session identifier.
        """
        opts = dict(options or {})
        image_ref = opts.get("image")
        gpu = opts.get("gpu")
        session_id = allocate_session_id("bernstein-modal", opts.get("session_id"))

        def _build() -> Any:
            if self._client_factory is not None:
                return self._client_factory(
                    image=image_ref,
                    gpu=gpu,
                    manifest=manifest,
                )
            modal = _import_modal()
            sandbox_factory = getattr(modal, "Sandbox", None)
            if sandbox_factory is None:
                raise ModalUnavailableError("Modal SDK missing Sandbox class")
            kwargs: dict[str, Any] = {}
            if image_ref:
                kwargs["image"] = image_ref
            if gpu:
                kwargs["gpu"] = gpu
            return sandbox_factory.create(**kwargs)

        sandbox = await asyncio.to_thread(_build)
        session = ModalSandboxSession(
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
        def _do_resume() -> Any:
            if self._client_factory is not None:
                return self._client_factory(resume=snapshot_id)
            modal = _import_modal()
            sandbox_cls = getattr(modal, "Sandbox", None)
            if sandbox_cls is None:
                raise ModalUnavailableError("Modal SDK missing Sandbox class")
            resume_fn = getattr(sandbox_cls, "resume", None)
            if resume_fn is None:
                raise NotImplementedError("Installed Modal SDK does not expose a resume entry point")
            return resume_fn(snapshot_id)

        sandbox = await asyncio.to_thread(_do_resume)
        session = ModalSandboxSession(
            session_id=snapshot_id,
            sandbox=sandbox,
            workdir="/workspace",
            base_env={},
            default_timeout=1800,
        )
        self._sessions[snapshot_id] = session
        return session

    async def destroy(self, session: SandboxSession) -> None:
        await session.shutdown()
        self._sessions.pop(session.session_id, None)


__all__ = [
    "ModalSandboxBackend",
    "ModalSandboxSession",
    "ModalUnavailableError",
]
