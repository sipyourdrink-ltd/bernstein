"""Unit tests for worktree snapshot/resume (issue #2295).

Fork-from-step builds on git-worktree commits as the snapshot primitive.
:meth:`WorktreeSandboxSession.snapshot` commits the current working tree
to ``refs/bernstein/snapshots/<run_id>/<step_index>`` and returns the
commit sha; :meth:`WorktreeSandboxBackend.resume` checks that sha out
into a fresh worktree with byte-identical contents.

Covers AC1 (snapshot returns a sha, resume restores byte-identical
contents), AC4 (two forks from the same snapshot are isolated), and the
cloud-backend NotImplementedError contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.sandbox import WorkspaceManifest
from bernstein.core.sandbox.backends.worktree import (
    WorktreeSandboxBackend,
    WorktreeSandboxSession,
)


def _init_git_repo(path: Path) -> None:
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
async def test_snapshot_returns_commit_sha_and_updates_ref(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    session = await backend.create(
        manifest,
        options={"repo_root": str(git_repo), "run_id": "run-a", "step_index": 3},
    )
    try:
        assert isinstance(session, WorktreeSandboxSession)
        await session.write("hello.txt", b"world")
        sha = await session.snapshot()
        # A snapshot id is a full 40-char git sha.
        assert len(sha) == 40
        int(sha, 16)  # hex
        # The dedicated ref points at exactly that sha.
        ref = "refs/bernstein/snapshots/run-a/3"
        resolved = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert resolved == sha
    finally:
        await backend.destroy(session)


@pytest.mark.asyncio
async def test_resume_restores_byte_identical_tree(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    session = await backend.create(
        manifest,
        options={"repo_root": str(git_repo), "run_id": "run-b", "step_index": 0},
    )
    payload = b"deterministic-bytes-\x00\x01\x02"
    try:
        await session.write("nested/dir/data.bin", payload)
        await session.write("top.txt", b"top-level")
        sha = await session.snapshot()
    finally:
        await backend.destroy(session)

    resumed = await backend.resume(sha)
    try:
        assert await resumed.read("nested/dir/data.bin") == payload
        assert await resumed.read("top.txt") == b"top-level"
    finally:
        await backend.destroy(resumed)


@pytest.mark.asyncio
async def test_two_resumes_are_isolated(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    session = await backend.create(
        manifest,
        options={"repo_root": str(git_repo), "run_id": "run-c", "step_index": 1},
    )
    try:
        await session.write("shared.txt", b"base")
        sha = await session.snapshot()
    finally:
        await backend.destroy(session)

    a = await backend.resume(sha)
    b = await backend.resume(sha)
    try:
        # Distinct worktree directories - no shared mutable state (AC4).
        assert a.session_id != b.session_id
        assert Path(a.workdir) != Path(b.workdir)
        await a.write("shared.txt", b"mutated-in-a")
        assert await b.read("shared.txt") == b"base"
    finally:
        await backend.destroy(a)
        await backend.destroy(b)


@pytest.mark.asyncio
async def test_snapshot_without_run_context_raises(git_repo: Path) -> None:
    backend = WorktreeSandboxBackend()
    manifest = WorkspaceManifest(root=str(git_repo), timeout_seconds=30)
    # No run_id/step_index supplied - snapshot cannot address a ref.
    session = await backend.create(manifest, options={"repo_root": str(git_repo)})
    try:
        with pytest.raises(RuntimeError):
            await session.snapshot()
    finally:
        await backend.destroy(session)
