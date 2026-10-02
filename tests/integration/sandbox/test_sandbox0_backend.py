"""Live Sandbox0 conformance. Opt in with CI_SANDBOX0_TEST=1 and credentials.

Use an isolated region/team and a template containing Python 3 and Git.
All sessions and snapshots created by this suite are explicitly reaped.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest
import pytest_asyncio

from bernstein.core.sandbox import WorkspaceManifest
from bernstein.core.sandbox.backends.sandbox0 import Sandbox0SandboxBackend
from bernstein.core.sandbox.conformance import SandboxBackendConformance
from bernstein.core.sandbox.manifest import FileEntry, GitRepoEntry

pytestmark = pytest.mark.skipif(
    os.environ.get("CI_SANDBOX0_TEST") != "1",
    reason="Set CI_SANDBOX0_TEST=1 to create real Sandbox0 resources",
)


class TestSandbox0Conformance(SandboxBackendConformance):
    @pytest_asyncio.fixture
    async def backend(self):
        backend = Sandbox0SandboxBackend()
        snapshots: list[str] = []
        original = backend.client.sandboxes.create_rootfs_snapshot

        def capture(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            snapshots.append(snapshot.id)
            return snapshot

        backend.client.sandboxes.create_rootfs_snapshot = capture
        try:
            yield backend
        finally:
            await backend.destroy_all()
            for snapshot_id in snapshots:
                await asyncio.to_thread(backend.client.sandboxes.delete_rootfs_snapshot, snapshot_id)
            backend.client.close()

    @pytest.fixture
    def manifest(self):
        return WorkspaceManifest(root="/workspace", env={"BASE": "preserved"}, timeout_seconds=60)

    @pytest.mark.asyncio
    async def test_binary_stdio_literal_argv_and_cwd(self, backend, manifest):
        session = await backend.create(manifest)
        await session.write("nested/executable", b"data", mode=0o700)
        data = bytes(range(256)) * 8192
        result = await session.exec(
            [
                "python3",
                "-c",
                "import os,sys; assert os.getcwd().endswith('/nested'); assert os.environ['BASE']=='preserved'; assert sys.argv[1]=='$(touch /tmp/injected)'; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.buffer.write(bytes([0,255]))",
                "$(touch /tmp/injected)",
            ],
            cwd="nested",
            stdin=data,
        )
        assert result.exit_code == 0
        assert result.stdout == data
        assert result.stderr == b"\x00\xff"
        result = await session.exec(["stat", "-c", "%a", "nested/executable"])
        assert result.stdout.strip() == b"700"
        with pytest.raises(FileNotFoundError):
            await session.read("missing-file")

    @pytest.mark.asyncio
    async def test_timeout_stops_side_effects(self, backend, manifest):
        session = await backend.create(manifest)
        with pytest.raises(TimeoutError):
            await session.exec(["sh", "-c", "sleep 3; touch /workspace/escaped-timeout"], timeout=1)
        await asyncio.sleep(3)
        assert "escaped-timeout" not in await session.ls(".")

    @pytest.mark.asyncio
    async def test_cancel_stops_side_effects(self, backend, manifest):
        session = await backend.create(manifest)
        task = asyncio.create_task(
            session.exec(["sh", "-c", "touch /workspace/started; sleep 3; touch /workspace/escaped-cancel"])
        )
        for _ in range(100):
            if "started" in await session.ls("."):
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("Command never started")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(3)
        assert "escaped-cancel" not in await session.ls(".")

    @pytest.mark.asyncio
    async def test_independent_sessions_and_snapshot_restores(self, backend, manifest):
        first = await backend.create(manifest)
        await first.write("state", b"base")
        reference = await first.snapshot()
        await backend.destroy(first)
        left, right = await asyncio.gather(backend.resume(reference), backend.resume(reference))
        assert left.session_id != right.session_id
        await left.write("state", b"changed")
        assert await right.read("state") == b"base"
        result = await right.exec(["printenv", "BASE"])
        assert result.stdout.strip() == b"preserved"
        assert not any(name.startswith("bernstein-exec-") for name in await right.ls("/tmp"))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("detached", [False, True])
    async def test_repository_and_injected_file(self, backend, tmp_path, detached):
        subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
        (tmp_path / "tracked.txt").write_text("committed")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "fixture"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        if detached:
            subprocess.run(["git", "checkout", "--detach"], cwd=tmp_path, check=True, capture_output=True)
        tip = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_path).strip()
        session = await backend.create(
            WorkspaceManifest(
                repo=GitRepoEntry(str(tmp_path), "HEAD" if detached else "main"),
                files=(FileEntry("injected.txt", b"injected"),),
            )
        )
        assert await session.read("tracked.txt") == b"committed"
        assert await session.read("injected.txt") == b"injected"
        result = await session.exec(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        assert result.stdout.strip() == (b"bernstein-detached" if detached else b"main")
        result = await session.exec(["git", "rev-parse", "HEAD"])
        assert result.stdout.strip() == tip
