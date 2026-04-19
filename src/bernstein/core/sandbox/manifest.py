"""WorkspaceManifest — declarative description of a sandbox workspace.

Phase 1 only covers the minimum surface every backend needs to materialise
a workable checkout: the workspace root path, an optional git clone
source, a tuple of bytes-injected files, and environment variables.
Cloud-specific mount entries (S3 artifacts, persistent volumes,
secrets-manager bindings) are intentionally deferred to ``oai-003`` so
this ticket doesn't grow unbounded.

A manifest is frozen once passed to :meth:`SandboxBackend.create`: the
dataclasses are immutable and any nested tuple is read-only. Backends
treat it as a pure value object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class GitRepoEntry:
    """A local git repository to clone into the sandbox root.

    Attributes:
        src_path: Local filesystem path on the orchestrator where the
            repo lives. Backends either push the branch to the sandbox
            (Docker volume mount, E2B upload) or clone the remote when
            the sandbox has network access.
        branch: Branch to check out inside the sandbox.
        sparse_paths: Optional tuple of paths to include via
            ``git sparse-checkout``. Empty tuple means full checkout.
    """

    src_path: str
    branch: str
    sparse_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class FileEntry:
    """A byte-injected file to place inside the sandbox root.

    Useful for seeding small config files (``.env``, ``bernstein.yaml``,
    ``.claude/settings.json``) without having them tracked in git.

    Attributes:
        path: Path inside the sandbox, relative to
            :attr:`WorkspaceManifest.root`.
        content: Raw bytes to write at :attr:`path`.
        mode: POSIX file mode. Best-effort on Windows.
    """

    path: str
    content: bytes
    mode: int = 0o644


@dataclass(frozen=True)
class WorkspaceManifest:
    """Declarative description of a sandbox workspace.

    Backends consume a manifest in :meth:`SandboxBackend.create`.

    Attributes:
        root: Absolute path inside the sandbox where the workspace
            should live. ``/workspace`` is the convention for Docker
            and cloud backends; the worktree backend maps this to the
            host-side worktree directory.
        repo: Optional :class:`GitRepoEntry` to seed the sandbox with
            a git checkout. ``None`` leaves :attr:`root` empty (useful
            for untrusted-code sandboxes that should never see the
            parent repo).
        files: Additional byte-injected files.
        env: Environment variables set for every
            :meth:`SandboxSession.exec` invocation. Callers can still
            override per call via ``env=``.
        timeout_seconds: Default wall-clock timeout for
            :meth:`SandboxSession.exec` when the caller doesn't pass
            one explicitly. Not a hard session lifetime cap — individual
            backends may still honour longer sessions.
    """

    root: str = "/workspace"
    repo: GitRepoEntry | None = None
    files: tuple[FileEntry, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict[str, str])
    timeout_seconds: int = 1800


__all__ = [
    "FileEntry",
    "GitRepoEntry",
    "WorkspaceManifest",
]
