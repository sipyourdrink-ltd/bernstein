"""Unit tests for :class:`WorktreeSandboxBackend` (oai-002).

These tests cover worktree-specific behaviour that the generic
conformance suite cannot: path-escape guards, legacy
:class:`WorktreeManager` reuse, manifest file injection into the
worktree root, and interactions with the underlying git repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.sandbox import (
    FileEntry,
    WorkspaceManifest,
)
from bernstein.core.sandbox.backends.worktree import (
    WorktreeSandboxBackend,
    WorktreeSandboxSession,
)


def _init_git_repo(path: Path) -> None:
    """Initialise a throwaway git repo under *path*."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=bernstein",
            "-c",
            "user.email=bernstein@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=path,
        check=True,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    _init_git_repo(tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_create_returns_worktree_session(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    session = await backend.create(manifest, options={"repo_root": str(git_repo)})
    try:
        assert isinstance(session, WorktreeSandboxSession)
        assert session.backend_name == "worktree"
        # Worktree lives at .sdd/worktrees/<session_id>.
        expected = git_repo / ".sdd" / "worktrees" / session.session_id
        assert Path(session.workdir) == expected
        assert expected.exists()
    finally:
        await backend.destroy(session)


@pytest.mark.asyncio
async def test_destroy_removes_worktree(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    session = await backend.create(manifest, options={"repo_root": str(git_repo)})
    worktree_path = Path(session.workdir)
    assert worktree_path.exists()
    await backend.destroy(session)
    assert not worktree_path.exists()


@pytest.mark.asyncio
async def test_create_injects_manifest_files(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(
        root=str(git_repo),
        files=(
            FileEntry(path="cfg/a.txt", content=b"a"),
            FileEntry(path="cfg/b.txt", content=b"b"),
        ),
        timeout_seconds=30,
    )
    session = await backend.create(manifest, options={"repo_root": str(git_repo)})
    try:
        assert (Path(session.workdir) / "cfg" / "a.txt").read_bytes() == b"a"
        assert (Path(session.workdir) / "cfg" / "b.txt").read_bytes() == b"b"
    finally:
        await backend.destroy(session)


@pytest.mark.asyncio
async def test_path_escape_guard(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    session = await backend.create(manifest, options={"repo_root": str(git_repo)})
    try:
        with pytest.raises(ValueError):
            await session.write("../escape.txt", b"x")
        with pytest.raises(ValueError):
            await session.read("../../../etc/hostname")
    finally:
        await backend.destroy(session)


@pytest.mark.asyncio
async def test_snapshot_raises_not_implemented(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    session = await backend.create(manifest, options={"repo_root": str(git_repo)})
    try:
        with pytest.raises(NotImplementedError):
            await session.snapshot()
        with pytest.raises(NotImplementedError):
            await backend.resume("any-id")
    finally:
        await backend.destroy(session)


@pytest.mark.asyncio
async def test_ls_non_directory_raises(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    session = await backend.create(manifest, options={"repo_root": str(git_repo)})
    try:
        await session.write("file.txt", b"hi")
        with pytest.raises(NotADirectoryError):
            await session.ls("file.txt")
    finally:
        await backend.destroy(session)


@pytest.mark.asyncio
async def test_manager_is_cached_per_repo(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    s1 = await backend.create(manifest, options={"repo_root": str(git_repo)})
    s2 = await backend.create(manifest, options={"repo_root": str(git_repo)})
    try:
        # Internal assertion: manager reuse across sessions sharing a
        # repo — a subsequent audit of the backend's worktree_managers
        # must find exactly one entry keyed to the repo root.
        managers = backend._managers  # type: ignore[attr-defined]
        assert len(managers) == 1
        key = next(iter(managers))
        assert key == git_repo.resolve()
    finally:
        await backend.destroy(s1)
        await backend.destroy(s2)


@pytest.mark.asyncio
async def test_exec_requires_nonempty_cmd(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    session = await backend.create(manifest, options={"repo_root": str(git_repo)})
    try:
        with pytest.raises(ValueError):
            await session.exec([])
    finally:
        await backend.destroy(session)


@pytest.mark.asyncio
async def test_exec_after_shutdown_raises(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    session = await backend.create(manifest, options={"repo_root": str(git_repo)})
    await session.shutdown()
    with pytest.raises(RuntimeError):
        await session.exec(["echo", "hi"])


@pytest.mark.asyncio
async def test_manifest_injects_file_escape_is_skipped(git_repo: Path) -> None:
    """Manifest files attempting path escape are logged and skipped."""
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(
        root=str(git_repo),
        files=(
            FileEntry(path="../naughty.txt", content=b"x"),
            FileEntry(path="good.txt", content=b"y"),
        ),
        timeout_seconds=30,
    )
    session = await backend.create(manifest, options={"repo_root": str(git_repo)})
    try:
        # The escape attempt is skipped but the good file lands.
        assert (Path(session.workdir) / "good.txt").read_bytes() == b"y"
        assert not (git_repo.parent / "naughty.txt").exists()
    finally:
        await backend.destroy(session)
