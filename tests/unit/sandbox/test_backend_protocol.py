"""Protocol-level tests for SandboxBackend/SandboxSession (oai-002).

The conformance suite itself lives in :mod:`bernstein.core.sandbox.conformance`;
it is parametrised so each backend simply subclasses and supplies a fixture.

This file exercises:

1. The protocol contract (runtime_checkable Protocol matches our concrete
   classes).
2. :class:`SandboxBackendConformance` against the worktree backend. Docker
   /E2B/Modal conformance lives under ``tests/integration/sandbox/`` and
   is gated by environment/daemon availability.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from bernstein.core.sandbox import (
    ExecResult,
    SandboxBackend,
    SandboxCapability,
    SandboxSession,
    WorkspaceManifest,
)
from bernstein.core.sandbox.backends.worktree import WorktreeSandboxBackend
from bernstein.core.sandbox.conformance import SandboxBackendConformance

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _init_git_repo(path: Path) -> None:
    """Initialise a throwaway git repo for worktree-backend tests."""
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


def test_protocol_runtime_checkable() -> None:
    """WorktreeSandboxBackend satisfies the runtime-checkable protocol."""
    backend = WorktreeSandboxBackend()
    assert isinstance(backend, SandboxBackend)


def test_capabilities_worktree_shape() -> None:
    """Worktree backend must expose FILE_RW + EXEC + NETWORK capabilities."""
    backend = WorktreeSandboxBackend()
    assert SandboxCapability.FILE_RW in backend.capabilities
    assert SandboxCapability.EXEC in backend.capabilities
    assert SandboxCapability.NETWORK in backend.capabilities
    assert SandboxCapability.SNAPSHOT not in backend.capabilities


def test_exec_result_dataclass_is_frozen() -> None:
    """ExecResult is a frozen dataclass (value object semantics)."""
    from dataclasses import FrozenInstanceError

    result = ExecResult(exit_code=0, stdout=b"hi", stderr=b"", duration_seconds=0.1)
    with pytest.raises(FrozenInstanceError):
        result.exit_code = 1  # type: ignore[misc]


def test_sandbox_session_is_abstract() -> None:
    """SandboxSession cannot be instantiated directly (abstract base)."""
    with pytest.raises(TypeError):
        SandboxSession()  # type: ignore[abstract]


class TestWorktreeConformance(SandboxBackendConformance):
    """Run the full conformance suite against the worktree backend."""

    @pytest_asyncio.fixture
    async def backend(
        self,
        tmp_path: Path,
    ) -> AsyncIterator[SandboxBackend]:
        _init_git_repo(tmp_path)
        backend = WorktreeSandboxBackend()
        yield backend

    @pytest.fixture
    def manifest(self, tmp_path: Path) -> WorkspaceManifest:
        return WorkspaceManifest(
            root=str(tmp_path),
            env={"LC_ALL": "C"},
            timeout_seconds=60,
        )

    @pytest.mark.asyncio
    async def test_read_write_roundtrip_utf8(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            payload = "hello world — ✓".encode()
            await session.write("hello.txt", payload)
            got = await session.read("hello.txt")
            assert got == payload
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_read_write_roundtrip_binary(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            payload = bytes(range(256)) * 4
            await session.write("blob.bin", payload)
            got = await session.read("blob.bin")
            assert got == payload
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_read_write_large_file(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            payload = b"x" * (1024 * 1024)
            await session.write("large.bin", payload)
            got = await session.read("large.bin")
            assert got == payload
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_exec_returns_exit_code(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            good = await session.exec(["sh", "-c", "echo hi; echo boom 1>&2; exit 0"])
            assert good.exit_code == 0
            assert b"hi" in good.stdout
            assert b"boom" in good.stderr
            bad = await session.exec(["sh", "-c", "exit 7"])
            assert bad.exit_code == 7
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_exec_respects_env(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            result = await session.exec(
                ["sh", "-c", "echo $BERNSTEIN_CONFORMANCE"],
                env={"BERNSTEIN_CONFORMANCE": "ok"},
            )
            assert result.exit_code == 0
            assert b"ok" in result.stdout
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_exec_respects_timeout(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            with pytest.raises(TimeoutError):
                await session.exec(["sleep", "5"], timeout=1)
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_ls_lists_entries(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        try:
            await session.write("dir/a.txt", b"a")
            await session.write("dir/b.txt", b"b")
            names = await session.ls("dir")
            assert "a.txt" in names
            assert "b.txt" in names
        finally:
            await backend.destroy(session)

    @pytest.mark.asyncio
    async def test_shutdown_is_idempotent(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        session = await backend.create(manifest, options={"repo_root": manifest.root})
        await session.shutdown()
        await session.shutdown()  # must not raise

    @pytest.mark.asyncio
    async def test_snapshot_resume_roundtrip(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        # Worktree backend declares no SNAPSHOT capability, so the
        # conformance contract requires a skip here.
        if SandboxCapability.SNAPSHOT not in backend.capabilities:
            pytest.skip("Backend does not declare SNAPSHOT capability")
        # Unreachable for worktree; kept for future backends that subclass
        # this test class without overriding behaviour.
        raise AssertionError("Worktree backend must not claim SNAPSHOT")
