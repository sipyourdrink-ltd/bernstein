"""Protocol conformance helpers for :class:`SandboxBackend` implementations.

A single reusable test base class lives here so backend-specific test
modules only have to supply a fixture that yields a ``(backend,
manifest)`` pair. The class intentionally uses plain ``async def``
test methods with ``@pytest.mark.asyncio`` so it slots into the
existing pytest-asyncio setup.

Subclass it like::

    class TestWorktreeConformance(SandboxBackendConformance):
        @pytest.fixture
        async def backend(self, tmp_path):
            ...
            yield backend

        @pytest.fixture
        def manifest(self, tmp_path):
            return WorkspaceManifest(...)

Test runners that cannot accept fixtures at the method level (e.g.
``doctest``) are not supported — the conformance contract is pytest-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.sandbox.backend import (
    SandboxBackend,
    SandboxCapability,
)

if TYPE_CHECKING:
    from bernstein.core.sandbox.manifest import WorkspaceManifest


class SandboxBackendConformance:
    """Reusable conformance suite for any :class:`SandboxBackend`.

    Subclasses supply two pytest fixtures:

    - ``backend``: an async fixture yielding a fresh
      :class:`SandboxBackend` plus any per-test setup/teardown.
    - ``manifest``: a :class:`WorkspaceManifest` appropriate to the
      backend (e.g. a worktree needs ``manifest.repo`` set).

    Backends declaring :attr:`SandboxCapability.SNAPSHOT` get the
    snapshot-resume checks; other backends have those tests skipped
    automatically.
    """

    @pytest.mark.asyncio
    async def test_read_write_roundtrip_utf8(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        """Write then read a UTF-8 file and check byte equality."""
        session = await backend.create(manifest)
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
        """Roundtrip arbitrary binary bytes."""
        session = await backend.create(manifest)
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
        """Roundtrip 1 MB of data."""
        session = await backend.create(manifest)
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
        """Exec captures exit code and stdout/stderr."""
        session = await backend.create(manifest)
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
        """Env overrides propagate into the exec environment."""
        session = await backend.create(manifest)
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
        """A command exceeding the timeout raises :class:`TimeoutError`."""
        session = await backend.create(manifest)
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
        """``ls`` reports files written in the same directory."""
        session = await backend.create(manifest)
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
        """Calling ``shutdown`` twice does not raise."""
        session = await backend.create(manifest)
        await session.shutdown()
        await session.shutdown()  # must not raise

    @pytest.mark.asyncio
    async def test_snapshot_resume_roundtrip(
        self,
        backend: SandboxBackend,
        manifest: WorkspaceManifest,
    ) -> None:
        """Snapshot-capable backends round-trip state across resume."""
        if SandboxCapability.SNAPSHOT not in backend.capabilities:
            pytest.skip("Backend does not declare SNAPSHOT capability")
        session = await backend.create(manifest)
        try:
            await session.write("state.txt", b"captured")
            snap_id = await session.snapshot()
        finally:
            await backend.destroy(session)
        resumed = await backend.resume(snap_id)
        try:
            got = await resumed.read("state.txt")
            assert got == b"captured"
        finally:
            await backend.destroy(resumed)

    @pytest.mark.asyncio
    async def test_capabilities_declared(
        self,
        backend: SandboxBackend,
    ) -> None:
        """Every backend must advertise FILE_RW and EXEC at minimum."""
        assert SandboxCapability.FILE_RW in backend.capabilities
        assert SandboxCapability.EXEC in backend.capabilities

    @pytest.mark.asyncio
    async def test_name_is_nonempty(self, backend: SandboxBackend) -> None:
        """Backends must expose a non-empty canonical name."""
        assert isinstance(backend.name, str) and backend.name


__all__ = ["SandboxBackendConformance"]
