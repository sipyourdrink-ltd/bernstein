"""Sandbox backend protocol and session ABC (oai-002 phase 1).

Bernstein's agent isolation has historically been git-worktree-only. This
module defines the shape that lets additional isolation backends (Docker,
E2B, Modal, Daytona, Cloudflare, Vercel ...) plug in behind a uniform
interface — every backend provisions a :class:`SandboxSession` against a
:class:`~bernstein.core.sandbox.manifest.WorkspaceManifest` and exposes
``read``/``write``/``exec``/``ls``/``snapshot``/``shutdown`` primitives.

The design mirrors OpenAI Agents SDK v2's ``SandboxClient`` protocol so
plugin authors can port implementations across ecosystems with minimal
shim code.

Phase 1 scope (this ticket, oai-002): protocol + worktree + Docker
first-party, E2B/Modal as optional extras. The spawner gains an
OPTIONAL ``sandbox_session`` parameter; when ``None`` it falls back to
the existing direct-worktree path so all 35 existing adapters keep
working unchanged. Per-adapter refactor to route exec through
:class:`SandboxSession` is tracked as a follow-up (``oai-002b``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.sandbox.manifest import WorkspaceManifest


class SandboxCapability(StrEnum):
    """Feature flags describing what a backend can offer.

    Backends advertise a :class:`frozenset` of capabilities so schedulers
    and the plan loader can reject manifests that demand unsupported
    features (e.g. a plan that requires ``SNAPSHOT`` on a backend that
    does not declare it).

    Values:
        FILE_RW: The backend supports ``session.read`` / ``session.write``.
        EXEC: The backend supports ``session.exec`` with exit code,
            stdout, and stderr capture.
        NETWORK: The sandbox has outbound network access. Worktrees
            always do; isolated cloud sandboxes frequently do not by
            default.
        GPU: The backend can allocate a GPU attached to the session
            (e.g. Modal).
        SNAPSHOT: The backend supports ``session.snapshot`` returning an
            identifier that later ``backend.resume`` can restore.
        PERSISTENT_VOLUMES: The backend can mount persistent volumes that
            outlive the session (used by oai-003 cloud artifact storage).
    """

    FILE_RW = "file_rw"
    EXEC = "exec"
    NETWORK = "network"
    GPU = "gpu"
    SNAPSHOT = "snapshot"
    PERSISTENT_VOLUMES = "persistent_volumes"


@dataclass(frozen=True)
class ExecResult:
    """Result of a :meth:`SandboxSession.exec` invocation.

    Attributes:
        exit_code: Process exit code. ``0`` indicates success.
        stdout: Raw bytes written to stdout.
        stderr: Raw bytes written to stderr.
        duration_seconds: Wall-clock time taken by the command.
    """

    exit_code: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float


class SandboxSession(ABC):
    """An active sandbox provisioned by a :class:`SandboxBackend`.

    Sessions are created via :meth:`SandboxBackend.create` and destroyed
    via :meth:`SandboxBackend.destroy` (which in turn calls
    :meth:`shutdown`). All file and exec I/O is routed through the
    session's abstract methods so the caller doesn't need to know whether
    the sandbox is a local worktree, a Docker container, or a cloud
    microVM.

    Attributes:
        backend_name: Canonical name of the owning backend (e.g.
            ``"worktree"``, ``"docker"``). Set by the backend on
            construction.
        session_id: Opaque identifier chosen by the backend. Stable for
            the session's lifetime.
        workdir: Logical path inside the sandbox where the workspace
            root lives. Adapters that still run as local subprocesses
            use this as ``cwd``; adapters refactored in oai-002b will
            use it only inside ``session.exec`` invocations.
    """

    backend_name: str
    session_id: str
    workdir: str

    @abstractmethod
    async def read(self, path: str) -> bytes:
        """Read a file from the sandbox.

        Args:
            path: File path inside the sandbox. May be absolute or
                relative to :attr:`workdir`; backends treat both forms
                consistently.

        Returns:
            The file's raw bytes.

        Raises:
            FileNotFoundError: If no file exists at ``path``.
            OSError: For other filesystem errors.
        """

    @abstractmethod
    async def write(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        """Write bytes to a file in the sandbox.

        Creates parent directories as needed. Overwrites any existing
        file at ``path``.

        Args:
            path: File path inside the sandbox.
            data: Bytes to write.
            mode: POSIX file mode for the new file. Best-effort on
                Windows.
        """

    @abstractmethod
    async def exec(
        self,
        cmd: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult:
        """Run a command inside the sandbox.

        Args:
            cmd: Argv list. The backend is responsible for not piping
                this through a shell.
            cwd: Working directory. Defaults to :attr:`workdir`.
            env: Extra environment variables. Merged on top of the
                backend's base environment; ``None`` uses the base
                environment unchanged.
            timeout: Kill the command after this many seconds. ``None``
                uses the backend's default.
            stdin: Optional bytes fed to the command's stdin.

        Returns:
            An :class:`ExecResult` with exit code, captured output, and
            duration.

        Raises:
            TimeoutError: When ``timeout`` elapses before the command
                finishes.
        """

    @abstractmethod
    async def ls(self, path: str) -> list[str]:
        """List entries inside a directory.

        Args:
            path: Directory path inside the sandbox.

        Returns:
            Sorted list of entry names (not full paths). Directories
            and files are both listed; no type information is implied
            by the names themselves.
        """

    @abstractmethod
    async def snapshot(self) -> str:
        """Persist the current session state for later ``resume``.

        Backends without :attr:`SandboxCapability.SNAPSHOT` must raise
        :class:`NotImplementedError`. The returned opaque string is
        understood only by the owning backend.

        Returns:
            A snapshot identifier passed back to
            :meth:`SandboxBackend.resume`.

        Raises:
            NotImplementedError: When the backend does not declare
                :attr:`SandboxCapability.SNAPSHOT`.
        """

    @abstractmethod
    async def shutdown(self) -> None:
        """Release all resources held by the session.

        Idempotent — safe to call multiple times.
        """


@runtime_checkable
class SandboxBackend(Protocol):
    """Protocol every sandbox backend implements.

    Backends are discovered via the ``bernstein.sandbox_backends``
    entry-point group (see :mod:`bernstein.core.sandbox.registry`).
    First-party backends (``worktree``, ``docker``) ship in bernstein
    core; cloud backends (``e2b``, ``modal``) ship as optional extras.

    Attributes:
        name: Canonical identifier used in plan.yaml ``sandbox.backend``.
            Must be stable across versions; rename with care.
        capabilities: The set of :class:`SandboxCapability` values this
            backend guarantees. ``FILE_RW`` and ``EXEC`` are effectively
            mandatory for any useful backend; everything else is opt-in.
    """

    name: str
    capabilities: frozenset[SandboxCapability]

    async def create(
        self,
        manifest: WorkspaceManifest,
        options: dict[str, Any] | None = None,
    ) -> SandboxSession:
        """Provision a fresh sandbox per *manifest*.

        Args:
            manifest: Declarative description of the workspace root,
                files to inject, and environment variables. See
                :class:`~bernstein.core.sandbox.manifest.WorkspaceManifest`.
            options: Backend-specific knobs (e.g. container image name,
                memory limit). Backends must tolerate ``None`` and
                unknown keys.

        Returns:
            An active :class:`SandboxSession`.
        """
        ...

    async def resume(self, snapshot_id: str) -> SandboxSession:
        """Restore a previously-captured snapshot.

        Args:
            snapshot_id: The opaque value returned by
                :meth:`SandboxSession.snapshot`.

        Returns:
            A session whose state equals the snapshot's state.

        Raises:
            NotImplementedError: When the backend does not declare
                :attr:`SandboxCapability.SNAPSHOT`.
        """
        ...

    async def destroy(self, session: SandboxSession) -> None:
        """Tear down a session and release any orchestrator-side state.

        Equivalent to calling :meth:`SandboxSession.shutdown` plus any
        backend-level bookkeeping (deleting images, freeing graveyard
        entries, etc.). Idempotent.
        """
        ...


__all__ = [
    "ExecResult",
    "SandboxBackend",
    "SandboxCapability",
    "SandboxSession",
]
