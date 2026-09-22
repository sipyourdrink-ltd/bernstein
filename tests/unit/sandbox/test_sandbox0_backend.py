"""SDK boundary tests; live protocol conformance is in integration/sandbox."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bernstein.core.sandbox import WorkspaceManifest
from bernstein.core.sandbox.backends.sandbox0 import (
    Sandbox0SandboxBackend,
    Sandbox0SandboxSession,
    _bundle_repository,
    _owned_call,
)
from bernstein.core.sandbox.manifest import FileEntry, GitRepoEntry, S3Mount


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


@pytest.mark.parametrize("checkout", ["branch", "detached", "head"])
def test_git_bundle_preserves_tip_and_returns_agent_commits(tmp_path, checkout):
    source = tmp_path / "source"
    target = tmp_path / "sandbox"

    def git(cwd, *args, check=True):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", *args],
            cwd=cwd,
            check=check,
            capture_output=True,
        )

    git(tmp_path, "init", "-b", "main", str(source))
    (source / "tracked").write_text("initial")
    git(source, "add", "tracked")
    git(source, "commit", "-m", "initial")
    git(source, "branch", "unrelated")
    if checkout == "detached":
        git(source, "checkout", "--detach")
    (source / "tracked").write_text("selected commit")
    git(source, "commit", "-am", "selected")
    original_tip = git(source, "rev-parse", "HEAD").stdout
    original_refs = git(source, "show-ref").stdout
    original_head = (source / ".git" / "HEAD").read_bytes()
    git(source, "config", "bernstein.host-only", "private-config")
    (source / "tracked").write_text("uncommitted host edit")
    (source / "untracked").write_text("not for upload")

    payload, branch = _bundle_repository(GitRepoEntry(str(source), "main" if checkout == "branch" else "HEAD"))
    bundle = tmp_path / "input.bundle"
    bundle.write_bytes(payload)
    git(tmp_path, "clone", "--branch", branch, str(bundle), str(target))
    assert git(target, "rev-parse", "HEAD").stdout == original_tip
    assert git(target, "rev-list", "--count", "HEAD").stdout.strip() == b"2"
    assert (target / "tracked").read_text() == "selected commit"
    assert not (target / "untracked").exists()
    assert git(target, "config", "--get", "bernstein.host-only", check=False).returncode == 1
    assert git(target, "branch", "--show-current").stdout.decode().strip() == branch
    heads = git(tmp_path, "bundle", "list-heads", str(bundle)).stdout.decode().splitlines()
    assert len(heads) == 1 and heads[0].endswith(f" refs/heads/{branch}")

    # Exercise the production sync-back refspec with a real agent-side commit.
    (target / "agent-result").write_text("done")
    git(target, "add", "agent-result")
    git(target, "commit", "-m", "agent work")
    returned = tmp_path / "result.bundle"
    git(target, "bundle", "create", str(returned), "--all")
    git(source, "fetch", str(returned), "refs/heads/*:refs/remotes/sandbox/test/*")
    assert git(source, "show", f"refs/remotes/sandbox/test/{branch}:agent-result").stdout == b"done"
    assert (source / ".git" / "HEAD").read_bytes() == original_head
    assert git(source, "rev-parse", "HEAD").stdout == original_tip
    assert git(source, "show-ref", "--heads").stdout == original_refs
    assert (source / "tracked").read_text() == "uncommitted host edit"
    assert not (source / "agent-result").exists()


@pytest.mark.parametrize("initialized", [False, True])
def test_head_without_commit_is_rejected(tmp_path, initialized):
    if initialized:
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    with pytest.raises(ValueError, match="HEAD must resolve to an existing commit"):
        _bundle_repository(GitRepoEntry(str(tmp_path), "HEAD"))


@pytest.mark.asyncio
async def test_snapshot_keeps_base_config_but_not_per_exec_credentials(backend, monkeypatch):
    pytest.importorskip("sandbox0")
    session = await backend.create(WorkspaceManifest(env={"APP_MODE": "test"}))
    sb = session._sandbox
    sb.create_context.return_value.id = "context-1"
    sb.get_context.return_value = SimpleNamespace(running=False, exit_code=0)
    sb.read_file.side_effect = [b"", b""]
    await session.exec(["true"], env={"MODEL_API_KEY": "command-only-secret"})
    write_metadata = AsyncMock()
    monkeypatch.setattr(session, "write", write_metadata)
    backend.client.sandboxes.create_rootfs_snapshot.return_value.id = "snapshot-1"
    reference = await session.snapshot()
    metadata = write_metadata.call_args.args[1]
    assert json.loads(metadata) == {"env": {"APP_MODE": "test"}}
    assert b"command-only-secret" not in metadata
    assert "command-only-secret" not in reference
    assert session._manifest.env == {"APP_MODE": "test"}
    sb.delete_file.assert_called_once()
