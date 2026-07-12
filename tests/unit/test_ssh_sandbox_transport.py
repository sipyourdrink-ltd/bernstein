"""Transport-seam tests for the ssh sandbox backend (issue #2352, AC4).

Promoting the ssh sandbox backend from scaffold to supported means it must be
drivable without a live ssh host: the only piece that truly needs a remote is
the transport hop, so it is injectable. These tests pin the seam
(:class:`RemoteExec`, the injected transport) and prove that with a faithful
in-process transport the *real* backend logic runs -- base64 file transfer and
the remote ``git worktree`` lifecycle -- against genuine local git worktrees,
with per-task branch isolation. The default (no-transport) backend keeps its
exact ssh argv assembly, so the shipped behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.sandbox.manifest import GitRepoEntry, WorkspaceManifest
from bernstein.core.sandbox.ssh_backend import (
    RemoteExec,
    SandboxConnectionError,
    SSHSandboxBackend,
)
from tests.support.ssh_fake import InProcessSSHTransport


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def remote_repo(tmp_path: Path) -> Path:
    """A real local git repo the backend will ``git worktree add`` from."""
    repo = tmp_path / "remote_repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def remote_root(tmp_path: Path) -> Path:
    root = tmp_path / "remote_root"
    root.mkdir()
    return root


def _backend(remote_root: Path, transport: InProcessSSHTransport) -> SSHSandboxBackend:
    return SSHSandboxBackend(host="builder.invalid", path=str(remote_root), transport=transport)


# ---------------------------------------------------------------------------
# RemoteExec value object + default transport wiring
# ---------------------------------------------------------------------------


def test_remote_exec_carries_returncode_and_streams() -> None:
    result = RemoteExec(returncode=7, stdout=b"out", stderr=b"err")
    assert result.returncode == 7
    assert result.stdout == b"out"
    assert result.stderr == b"err"


def test_default_backend_preserves_ssh_argv_assembly() -> None:
    """No injected transport: the ssh argv is assembled exactly as before."""
    backend = SSHSandboxBackend(host="example.com", user="alice", path="/srv/bern")
    argv = backend._build_ssh_cmd("echo hi")
    assert argv[0] == "ssh"
    assert argv[-2] == "alice@example.com"
    assert argv[-1].startswith("sh -c ")


def test_default_backend_still_classifies_connection_errors() -> None:
    backend = SSHSandboxBackend(host="example.com", path="/srv/bern")
    with patch(
        "bernstein.core.sandbox.ssh_backend.subprocess.run",
        return_value=MagicMock(returncode=255, stdout=b"", stderr=b"Connection refused"),
    ):
        with pytest.raises(SandboxConnectionError) as excinfo:
            backend.ensure_control_master()
    assert "connection refused" in excinfo.value.reason


# ---------------------------------------------------------------------------
# Injected faithful transport: real file transfer + exec run locally
# ---------------------------------------------------------------------------


def test_injected_transport_routes_file_and_exec_ops(remote_root: Path) -> None:
    transport = InProcessSSHTransport()
    backend = _backend(remote_root, transport)

    async def scenario() -> None:
        manifest = WorkspaceManifest(env={"GREETING": "hello"})
        session = await backend.create(manifest, options={"session_id": "sbx-a"})
        try:
            # base64 write/read round-trips real binary through the transport.
            await session.write("nested/data.bin", b"\x00\x01payload\xff")
            assert await session.read("nested/data.bin") == b"\x00\x01payload\xff"
            assert "data.bin" in await session.ls("nested")
            # env from the manifest reaches the remote exec.
            result = await session.exec(["sh", "-c", 'printf %s "$GREETING"'])
            assert result.exit_code == 0
            assert result.stdout == b"hello"
        finally:
            await backend.destroy(session)

    asyncio.run(scenario())
    # The multiplex socket was "opened" and at least one remote command ran.
    assert transport.master_opens >= 1
    assert transport.commands, "no remote commands were dispatched through the transport"


def test_injected_transport_provisions_isolated_git_worktrees(remote_repo: Path, remote_root: Path) -> None:
    """Two sessions get distinct git worktrees on distinct branches; no crosstalk."""
    transport = InProcessSSHTransport()
    backend = _backend(remote_root, transport)

    async def scenario() -> tuple[str, str]:
        manifest = WorkspaceManifest(repo=GitRepoEntry(src_path=str(remote_repo), branch="main"))
        s_a = await backend.create(manifest, options={"session_id": "sbx-a", "worktree_branch": "bernstein/run/t-a"})
        s_b = await backend.create(manifest, options={"session_id": "sbx-b", "worktree_branch": "bernstein/run/t-b"})
        # Each worktree is a real checkout (the seed file is present).
        assert "seed.txt" in await s_a.ls(".")
        assert "seed.txt" in await s_b.ls(".")
        # Write only into A; B must not see it (isolation).
        await s_a.write("only_in_a.txt", b"A")
        assert await s_a.exists("only_in_a.txt")
        assert not await s_b.exists("only_in_a.txt")
        wt_a, wt_b = s_a.workdir, s_b.workdir
        return wt_a, wt_b

    wt_a, wt_b = asyncio.run(scenario())
    assert wt_a != wt_b
    # Both worktree directories are registered in the source repo.
    listed = subprocess.run(
        ["git", "-C", str(remote_repo), "worktree", "list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "bernstein/run/t-a" in listed
    assert "bernstein/run/t-b" in listed


def test_worktree_branch_uses_dash_b_off_base(remote_repo: Path, remote_root: Path) -> None:
    transport = InProcessSSHTransport()
    backend = _backend(remote_root, transport)

    async def scenario() -> None:
        manifest = WorkspaceManifest(repo=GitRepoEntry(src_path=str(remote_repo), branch="main"))
        session = await backend.create(manifest, options={"session_id": "sbx-x", "worktree_branch": "bernstein/run/x"})
        await backend.destroy(session)

    asyncio.run(scenario())
    create_cmd = next(c for c in transport.commands if "git worktree add" in c)
    assert "-b bernstein/run/x" in create_cmd


def test_worktree_reset_reprovisions_without_collision(remote_repo: Path, remote_root: Path) -> None:
    """A stale worktree/branch from a killed attempt is reset, not collided with."""
    transport = InProcessSSHTransport()
    backend = _backend(remote_root, transport)

    async def scenario() -> None:
        manifest = WorkspaceManifest(repo=GitRepoEntry(src_path=str(remote_repo), branch="main"))
        opts = {"session_id": "sbx-r", "worktree_branch": "bernstein/run/r", "worktree_reset": True}
        # First provisioning; deliberately do NOT destroy (simulates a mid-run kill).
        await backend.create(manifest, options=opts)
        # Re-provisioning the same session id + branch must succeed via reset.
        again = await backend.create(manifest, options=opts)
        assert "seed.txt" in await again.ls(".")
        await backend.destroy(again)

    asyncio.run(scenario())
    reset_cmd = next(c for c in transport.commands if "git worktree add -B" in c)
    assert "-B bernstein/run/r" in reset_cmd


def test_session_component_rejects_path_escape(remote_root: Path) -> None:
    transport = InProcessSSHTransport()
    backend = _backend(remote_root, transport)

    async def scenario() -> None:
        manifest = WorkspaceManifest()
        await backend.create(manifest, options={"session_id": "../escape"})

    with pytest.raises(ValueError, match="session id"):
        asyncio.run(scenario())
