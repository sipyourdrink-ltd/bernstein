"""Worktree-backed :class:`SandboxBackend` — default, zero behaviour change.

This backend wraps the existing :class:`~bernstein.core.git.worktree.WorktreeManager`
without changing any of its internals — the graveyard salvage path,
stale-lock detection, and post-create isolation validation all continue
to run exactly as before. Its purpose in phase 1 is to give callers a
uniform :class:`SandboxBackend` handle even when they ultimately want
the existing local-worktree behaviour.

Every session wraps a single worktree directory on the host filesystem.
``read``/``write``/``exec`` use :mod:`asyncio.to_thread` to avoid
blocking the event loop; sync filesystem ops would otherwise monopolise
the loop when agents churn on large files.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.git.worktree import WorktreeManager, validate_worktree_slug
from bernstein.core.sandbox.backend import (
    ExecResult,
    SandboxCapability,
    SandboxSession,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bernstein.core.sandbox.manifest import WorkspaceManifest

logger = logging.getLogger(__name__)


class WorktreeSandboxSession(SandboxSession):
    """A session whose workspace is a local git worktree.

    Callers instantiate sessions only via
    :meth:`WorktreeSandboxBackend.create`. Manual construction is
    allowed (kept public) but most users should go through the backend
    so the lifecycle is managed.
    """

    backend_name = "worktree"

    def __init__(
        self,
        *,
        session_id: str,
        worktree_path: Path,
        manager: WorktreeManager,
        base_env: Mapping[str, str],
        default_timeout: int,
    ) -> None:
        """Initialise the session.

        Args:
            session_id: Opaque session identifier, also the worktree
                directory name.
            worktree_path: Absolute path to the worktree on the host.
            manager: The :class:`WorktreeManager` that owns this
                session — used on shutdown to run the existing cleanup
                pipeline (salvage, graveyard, branch delete).
            base_env: Environment applied to every :meth:`exec`.
            default_timeout: Default wall-clock timeout for
                :meth:`exec` when the caller doesn't supply one.
        """
        self.session_id = session_id
        self.workdir = str(worktree_path)
        self._worktree_path = worktree_path
        self._manager = manager
        self._base_env = dict(base_env)
        self._default_timeout = default_timeout
        self._closed = False

    # ------------------------------------------------------------------
    # File primitives
    # ------------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """Resolve *path* against the worktree, blocking traversal.

        Args:
            path: Caller-supplied path. Absolute paths must lie under
                the worktree; relative paths are joined onto it.

        Raises:
            ValueError: If the resolved path escapes the worktree root.
        """
        candidate = Path(path)
        resolved = candidate.resolve() if candidate.is_absolute() else (self._worktree_path / candidate).resolve()
        root = self._worktree_path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Path {path!r} escapes worktree root {self._worktree_path}") from exc
        return resolved

    async def read(self, path: str) -> bytes:
        """Read a file from the worktree."""
        target = self._resolve(path)
        return await asyncio.to_thread(target.read_bytes)

    async def write(self, path: str, data: bytes, *, mode: int = 0o644) -> None:
        """Write *data* to *path*, creating parent dirs as needed."""
        target = self._resolve(path)

        def _do_write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            try:
                target.chmod(mode)
            except OSError:
                # Windows / noexec mounts: chmod may fail, data is still
                # written so surface a debug-level log and move on.
                logger.debug("chmod(%o) failed for %s", mode, target)

        await asyncio.to_thread(_do_write)

    async def ls(self, path: str) -> list[str]:
        """List entries in a directory inside the worktree."""
        target = self._resolve(path)

        def _do_ls() -> list[str]:
            if not target.is_dir():
                raise NotADirectoryError(f"Not a directory: {target}")
            return sorted(p.name for p in target.iterdir())

        return await asyncio.to_thread(_do_ls)

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
        """Run *cmd* as a subprocess inside the worktree.

        Uses :mod:`asyncio.subprocess` so the call integrates cleanly
        with the orchestrator's event loop.
        """
        if self._closed:
            raise RuntimeError(f"Session {self.session_id} is closed")
        if not cmd:
            raise ValueError("cmd must be a non-empty argv list")
        effective_cwd = self._resolve(cwd) if cwd is not None else self._worktree_path
        effective_timeout = timeout if timeout is not None else self._default_timeout
        effective_env = dict(os.environ)
        effective_env.update(self._base_env)
        if env:
            effective_env.update(env)

        start = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(effective_cwd),
            env=effective_env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=stdin),
                timeout=effective_timeout,
            )
        except TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                logger.warning("Process did not exit after kill within 5s: %s", cmd)
            duration = time.monotonic() - start
            raise TimeoutError(f"Command {cmd!r} timed out after {effective_timeout}s") from None
        duration = time.monotonic() - start
        return ExecResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------
    # Snapshots (unsupported)
    # ------------------------------------------------------------------

    async def snapshot(self) -> str:
        """Worktree backend does not support snapshots."""
        raise NotImplementedError("WorktreeSandboxBackend does not declare the SNAPSHOT capability")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Clean up the worktree via the underlying manager.

        Idempotent — repeat calls are no-ops. Runs the existing
        :meth:`WorktreeManager.cleanup` so salvage, graveyard capture,
        branch delete, and lock removal all happen in their usual
        order.
        """
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._manager.cleanup, self.session_id)


class WorktreeSandboxBackend:
    """:class:`SandboxBackend` that provisions local git worktrees.

    Produces one :class:`WorktreeSandboxSession` per call to
    :meth:`create`. Backed by a cache of
    :class:`WorktreeManager` instances keyed by repository root so
    multiple manifests pointing at the same repo share the existing
    graveyard, lock, and salvage state.

    Snapshots are not supported because git worktrees don't have a
    natural restore primitive that survives the host process.
    """

    name = "worktree"
    capabilities: frozenset[SandboxCapability] = frozenset(
        {
            SandboxCapability.FILE_RW,
            SandboxCapability.EXEC,
            SandboxCapability.NETWORK,
        }
    )

    def __init__(self) -> None:
        self._managers: dict[Path, WorktreeManager] = {}
        self._sessions: dict[str, WorktreeSandboxSession] = {}

    def _manager_for(self, repo_root: Path) -> WorktreeManager:
        resolved = repo_root.resolve()
        cached = self._managers.get(resolved)
        if cached is not None:
            return cached
        manager = WorktreeManager(resolved)
        self._managers[resolved] = manager
        return manager

    @staticmethod
    def _allocate_session_id(hint: str | None = None) -> str:
        """Pick a fresh session ID, validating it for worktree use."""
        if hint:
            return validate_worktree_slug(hint)
        # 12 hex chars keeps dir names readable while avoiding collisions.
        return validate_worktree_slug(f"sbx-{secrets.token_hex(6)}")

    async def create(
        self,
        manifest: WorkspaceManifest,
        options: dict[str, Any] | None = None,
    ) -> SandboxSession:
        """Provision a new worktree session.

        Recognised ``options`` keys:

        - ``session_id``: Explicit session ID. Must pass
          :func:`validate_worktree_slug`. Default: random.
        - ``repo_root``: Host path to the repo that owns the worktree.
          Falls back to ``manifest.repo.src_path`` and finally to the
          current directory.

        The manifest's ``root`` is treated as informational — worktrees
        always live at ``<repo_root>/.sdd/worktrees/<session_id>``
        for compatibility with existing tooling. This is intentional:
        phase 1 promises zero behaviour change for worktree users.
        """
        opts = dict(options or {})
        session_id = self._allocate_session_id(opts.get("session_id"))
        repo_root_opt = opts.get("repo_root")
        if repo_root_opt is not None:
            repo_root = Path(repo_root_opt)
        elif manifest.repo is not None:
            repo_root = Path(manifest.repo.src_path)
        else:
            repo_root = Path.cwd()
        manager = await asyncio.to_thread(self._manager_for, repo_root)
        worktree_path = await asyncio.to_thread(manager.create, session_id)

        # Byte-injected files land at manifest.root for cloud backends,
        # but for the worktree they belong inside the worktree root so
        # relative paths behave identically.
        if manifest.files:

            def _inject_files() -> None:
                for entry in manifest.files:
                    target = (worktree_path / entry.path).resolve()
                    root = worktree_path.resolve()
                    try:
                        target.relative_to(root)
                    except ValueError:
                        logger.warning("Skipping file %r: path escapes worktree root", entry.path)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(entry.content)
                    try:
                        target.chmod(entry.mode)
                    except OSError:
                        logger.debug("chmod(%o) failed for %s", entry.mode, target)

            await asyncio.to_thread(_inject_files)

        session = WorktreeSandboxSession(
            session_id=session_id,
            worktree_path=worktree_path,
            manager=manager,
            base_env=manifest.env,
            default_timeout=manifest.timeout_seconds,
        )
        self._sessions[session_id] = session
        return session

    async def resume(self, snapshot_id: str) -> SandboxSession:
        """Worktree backend does not support snapshot/resume."""
        raise NotImplementedError("WorktreeSandboxBackend does not declare the SNAPSHOT capability")

    async def destroy(self, session: SandboxSession) -> None:
        """Shut down *session* and drop it from the internal table."""
        await session.shutdown()
        self._sessions.pop(session.session_id, None)


__all__ = [
    "WorktreeSandboxBackend",
    "WorktreeSandboxSession",
]
