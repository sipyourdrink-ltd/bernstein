"""SDK boundary tests; live protocol conformance is in integration/sandbox."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bernstein.core.sandbox import WorkspaceManifest
from bernstein.core.sandbox.backends.sandbox0 import Sandbox0SandboxBackend, Sandbox0SandboxSession, _owned_call
from bernstein.core.sandbox.manifest import FileEntry, S3Mount


@pytest.fixture
def backend():
    client = MagicMock()
    client.sandboxes.claim.return_value.id = "sandbox-1"
    missing = RuntimeError("not found")
    missing.status_code = 404
    client.sandboxes.get.side_effect = missing
    return Sandbox0SandboxBackend(client=client)


@pytest.mark.asyncio
async def test_create_hydrates_and_cleans_partial_failure(backend):
    manifest = WorkspaceManifest(files=(FileEntry("nested/x", b"payload"),))
    with pytest.MonkeyPatch.context() as patch:
        write = AsyncMock(side_effect=OSError("write failed"))
        patch.setattr(Sandbox0SandboxSession, "write", write)
        with pytest.raises(OSError, match="write failed"):
            await backend.create(manifest, {"template": "agent", "unknown": True})
    backend.client.sandboxes.claim.assert_called_once_with("agent", snapshot_id=None)
    backend.client.delete_sandbox.assert_called_once_with("sandbox-1")
    assert not backend._sessions


@pytest.mark.asyncio
async def test_rejects_mounts_before_allocating(backend):
    with pytest.raises(NotImplementedError):
        await backend.create(WorkspaceManifest(artifact_mounts=(S3Mount("bucket", "", "/artifacts"),)))
    backend.client.sandboxes.claim.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_retries_then_is_idempotent(backend):
    session = await backend.create(WorkspaceManifest())
    backend.client.delete_sandbox.side_effect = [OSError("unreachable"), None]
    with pytest.raises(OSError):
        await session.shutdown()
    assert not session._closed
    await session.shutdown()
    await session.shutdown()
    assert backend.client.delete_sandbox.call_count == 2
    assert not backend._sessions


@pytest.mark.asyncio
async def test_cancelled_allocation_is_reaped():
    entered, finish = threading.Event(), threading.Event()
    cleanup = MagicMock()

    def allocate():
        entered.set()
        finish.wait(5)
        return "resource"

    task = asyncio.create_task(_owned_call(allocate, cleanup=cleanup))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    cleanup.assert_called_once_with("resource")


@pytest.mark.asyncio
async def test_snapshot_reference_excludes_environment(backend, monkeypatch):
    session = await backend.create(WorkspaceManifest(env={"SECRET": "private"}))
    monkeypatch.setattr(session, "write", AsyncMock())
    backend.client.sandboxes.create_rootfs_snapshot.return_value.id = "snapshot-1"
    reference = await session.snapshot()
    assert "private" not in reference
    assert json.loads(reference)["snapshot_id"] == "snapshot-1"
    await backend.destroy(session)
    backend.client.sandboxes.delete_rootfs_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_resume_restores_metadata_with_new_backend(backend, monkeypatch):
    reference = json.dumps(dict(version=1, root="/project", template="agent", timeout_seconds=42, snapshot_id="snap"))
    monkeypatch.setattr(Sandbox0SandboxSession, "read", AsyncMock(return_value=b'{"env":{"X":"Y"}}'))
    session = await backend.resume(reference)
    assert session.workdir == "/project"
    assert session._manifest.env == {"X": "Y"}
    assert session._manifest.timeout_seconds == 42
    backend.client.sandboxes.claim.assert_called_once_with("agent", snapshot_id="snap")


@pytest.mark.asyncio
async def test_exec_preserves_bytes_and_literal_argv(backend):
    pytest.importorskip("sandbox0")
    session = await backend.create(WorkspaceManifest(env={"BASE": "one", "X": "old"}))
    sb = session._sandbox
    sb.create_context.return_value.id = "context-1"
    sb.get_context.return_value = SimpleNamespace(running=False, exit_code=7)
    sb.read_file.side_effect = [bytes(range(256)), b"stderr"]
    result = await session.exec(["echo", "$(touch /bad)", "a b"], cwd="sub", env={"X": "new"}, stdin=b"\x00\xff")
    assert result.exit_code == 7
    assert result.stdout == bytes(range(256))
    requests = {call.args[0].rsplit("/", 1)[-1]: call.args[1] for call in sb.write_file.call_args_list}
    request = json.loads(requests["request.json"])
    assert request == {
        "cmd": ["echo", "$(touch /bad)", "a b"],
        "cwd": "/workspace/sub",
        "env": {"BASE": "one", "X": "new"},
    }
    assert requests["stdin"] == b"\x00\xff"
    sb.delete_context.assert_called_once_with("context-1")
    assert sb.delete_file.called


@pytest.mark.asyncio
async def test_exec_timeout_terminates_remote_context(backend):
    pytest.importorskip("sandbox0")
    session = await backend.create(WorkspaceManifest())
    sb = session._sandbox
    sb.create_context.return_value.id = "context-1"
    sb.get_context.return_value = SimpleNamespace(running=True)
    with pytest.raises(TimeoutError):
        await session.exec(["sleep", "10"], timeout=0.01)
    sb.delete_context.assert_called_once_with("context-1")


@pytest.mark.asyncio
async def test_exec_cancellation_terminates_remote_context(backend):
    pytest.importorskip("sandbox0")
    session = await backend.create(WorkspaceManifest())
    sb = session._sandbox
    started = threading.Event()
    sb.create_context.return_value.id = "context-1"

    def poll(_):
        started.set()
        return SimpleNamespace(running=True)

    sb.get_context.side_effect = poll
    task = asyncio.create_task(session.exec(["sleep", "10"]))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    sb.delete_context.assert_called_once_with("context-1")


@pytest.mark.parametrize("credential", [None, "SANDBOX0_API_KEY", "SANDBOX0_TOKEN"])
def test_selector_requires_either_credential(backend, credential):
    from bernstein.core.sandbox.selector import SandboxEnvironment, SandboxPolicy, SandboxSelectionError, select_sandbox

    environment = SandboxEnvironment(available_credentials=frozenset([credential] if credential else []))
    policy = SandboxPolicy(override="sandbox0")
    if credential:
        assert select_sandbox([backend], policy=policy, environment=environment) is backend
    else:
        with pytest.raises(SandboxSelectionError, match="SANDBOX0_API_KEY"):
            select_sandbox([backend], policy=policy, environment=environment)


@pytest.mark.asyncio
async def test_shutdown_waits_after_accepted_response(backend, monkeypatch):
    session = await backend.create(WorkspaceManifest())
    accepted = RuntimeError("accepted")
    accepted.status_code = 202
    missing = RuntimeError("not found")
    missing.status_code = 404
    backend.client.delete_sandbox.side_effect = accepted
    backend.client.sandboxes.get.side_effect = [SimpleNamespace(status="terminating"), missing]
    monkeypatch.setattr("bernstein.core.sandbox.backends.sandbox0.time.sleep", lambda _: None)
    await session.shutdown()
    assert backend.client.sandboxes.get.call_count == 2
    assert session._closed
    assert not backend._sessions


@pytest.mark.asyncio
async def test_cancelled_snapshot_is_reaped(backend, monkeypatch):
    session = await backend.create(WorkspaceManifest())
    monkeypatch.setattr(session, "write", AsyncMock())
    entered, finish = threading.Event(), threading.Event()

    def snapshot(_):
        entered.set()
        finish.wait(5)
        return SimpleNamespace(id="cancelled-snapshot")

    backend.client.sandboxes.create_rootfs_snapshot.side_effect = snapshot
    task = asyncio.create_task(session.snapshot())
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    backend.client.sandboxes.delete_rootfs_snapshot.assert_called_once_with("cancelled-snapshot")
