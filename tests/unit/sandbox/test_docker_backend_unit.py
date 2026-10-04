"""Unit tests for DockerSandboxBackend helpers with a mocked docker client.

Live-daemon coverage lives in ``tests/integration/sandbox/``; these tests
exercise the daemon-availability probe, the run-teardown session sweep
(issue #2162), and the container spec (mounts, capabilities, user)
without requiring Docker.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from bernstein.core.sandbox import WorkspaceManifest
from bernstein.core.sandbox.backends.docker import DockerSandboxBackend, DockerUnavailableError
from bernstein.core.sandbox.manifest import GitRepoEntry

if TYPE_CHECKING:
    from pathlib import Path


def _make_client() -> MagicMock:
    """Build a docker client mock whose containers run and exec cleanly."""
    client = MagicMock()
    container = MagicMock()
    exec_result = MagicMock()
    exec_result.exit_code = 0
    exec_result.output = b""
    container.exec_run.return_value = exec_result
    client.containers.run.return_value = container
    return client


def test_ensure_available_pings_the_daemon() -> None:
    """A responsive daemon passes the availability probe."""
    client = _make_client()
    backend = DockerSandboxBackend(client=client)
    backend.ensure_available()
    client.ping.assert_called_once()


def test_ensure_available_raises_when_ping_fails() -> None:
    """An unreachable daemon surfaces as DockerUnavailableError."""
    client = _make_client()
    client.ping.side_effect = RuntimeError("daemon down")
    backend = DockerSandboxBackend(client=client)
    with pytest.raises(DockerUnavailableError):
        backend.ensure_available()


def test_destroy_all_removes_every_tracked_session() -> None:
    """destroy_all sweeps sessions left behind at run teardown."""
    client = _make_client()
    backend = DockerSandboxBackend(client=client)
    manifest = WorkspaceManifest(root="/workspace")

    asyncio.run(backend.create(manifest))
    asyncio.run(backend.create(manifest))
    assert len(backend._sessions) == 2  # pyright: ignore[reportPrivateUsage]

    asyncio.run(backend.destroy_all())

    assert backend._sessions == {}  # pyright: ignore[reportPrivateUsage]
    container = client.containers.run.return_value
    assert container.stop.call_count == 2
    assert container.remove.call_count == 2


# ---------------------------------------------------------------------------
# Container spec: what the sandboxed agent can see and do
# ---------------------------------------------------------------------------


def _run_kwargs(client: MagicMock) -> dict[str, Any]:
    """Return the keyword arguments of the single ``containers.run`` call."""
    client.containers.run.assert_called_once()
    return dict(client.containers.run.call_args.kwargs)


def _make_workdir(tmp_path: Path) -> Path:
    """Build a workdir with a ``.git`` directory and private ``.sdd`` state."""
    workdir = tmp_path / "project"
    (workdir / ".git" / "objects").mkdir(parents=True)
    (workdir / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (workdir / ".sdd" / "auth").mkdir(parents=True)
    (workdir / ".sdd" / "auth" / "identity.key").write_text("secret")
    (workdir / ".sdd" / "runtime").mkdir(parents=True)
    (workdir / "README.md").write_text("tracked\n")
    return workdir


def _repo_manifest(workdir: Path, branch: str = "main") -> WorkspaceManifest:
    return WorkspaceManifest(root="/workspace", repo=GitRepoEntry(src_path=str(workdir), branch=branch))


def test_repo_session_mounts_only_the_git_dir_read_only(tmp_path: Path) -> None:
    """The host workdir root (and its .sdd state) is never bind-mounted."""
    workdir = _make_workdir(tmp_path)
    client = _make_client()
    backend = DockerSandboxBackend(client=client)

    asyncio.run(backend.create(_repo_manifest(workdir)))

    volumes = _run_kwargs(client)["volumes"]
    git_dir = os.path.realpath(workdir / ".git")
    assert volumes == {git_dir: {"bind": "/host-repo/.git", "mode": "ro"}}
    real_workdir = os.path.realpath(workdir)
    for source in volumes:
        assert source != real_workdir
        assert not source.startswith(os.path.join(real_workdir, ".sdd"))


def test_repo_session_still_clones_from_host_repo(tmp_path: Path) -> None:
    """The in-container clone keeps reading ``/host-repo`` and checks out the branch.

    The mount keeps its host owner, so the clone runs with a container-local
    global config that lists the mount as safe: without that, git refuses the
    source when the container user differs from the operator (seen with a
    root image on a Linux host), and ``git -c`` never reaches the local
    ``upload-pack``.
    """
    workdir = _make_workdir(tmp_path)
    client = _make_client()
    backend = DockerSandboxBackend(client=client)

    asyncio.run(backend.create(_repo_manifest(workdir, branch="feature")))

    container = client.containers.run.return_value
    calls = container.exec_run.call_args_list
    argvs = [c.args[0] for c in calls]
    config_file = "/tmp/bernstein-clone.gitconfig"
    for safe_dir in ("/host-repo", "/host-repo/.git"):
        assert ["git", "config", "-f", config_file, "--add", "safe.directory", safe_dir] in argvs
    clone = next(c for c in calls if c.args[0][:2] == ["git", "clone"])
    assert clone.args[0] == ["git", "clone", "/host-repo", "/workspace"]
    assert clone.kwargs["environment"] == {"GIT_CONFIG_GLOBAL": config_file}
    assert ["git", "checkout", "feature"] in argvs


def test_gitfile_without_commondir_mounts_the_referenced_git_dir(tmp_path: Path) -> None:
    """A ``.git`` file (e.g. a submodule) mounts the git dir it points at."""
    real_git = tmp_path / "modules" / "sub"
    (real_git / "objects").mkdir(parents=True)
    (real_git / "HEAD").write_text("ref: refs/heads/main\n")
    workdir = tmp_path / "project"
    workdir.mkdir()
    (workdir / ".git").write_text("gitdir: ../modules/sub\n")
    client = _make_client()
    backend = DockerSandboxBackend(client=client)

    asyncio.run(backend.create(_repo_manifest(workdir)))

    assert _run_kwargs(client)["volumes"] == {
        os.path.realpath(real_git): {"bind": "/host-repo/.git", "mode": "ro"},
    }


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_linked_worktree_mounts_the_common_git_dir(tmp_path: Path) -> None:
    """A linked worktree resolves to the main repository's ``.git`` directory."""
    main = tmp_path / "main"
    main.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q", "-b", "main", str(main)], check=True)
    (main / "README.md").write_text("x\n")
    subprocess.run([*git, "-C", str(main), "add", "README.md"], check=True)
    subprocess.run([*git, "-C", str(main), "commit", "-q", "-m", "init"], check=True)
    linked = tmp_path / "linked"
    subprocess.run([*git, "-C", str(main), "worktree", "add", "-q", "-b", "wt", str(linked)], check=True)
    (linked / ".sdd" / "auth").mkdir(parents=True)
    assert (linked / ".git").is_file()
    client = _make_client()
    backend = DockerSandboxBackend(client=client)

    asyncio.run(backend.create(_repo_manifest(linked, branch="wt")))

    assert _run_kwargs(client)["volumes"] == {
        os.path.realpath(main / ".git"): {"bind": "/host-repo/.git", "mode": "ro"},
    }


def test_missing_git_dir_fails_before_any_container_starts(tmp_path: Path) -> None:
    """A repo path without ``.git`` is refused instead of mounting the tree."""
    workdir = tmp_path / "not-a-repo"
    (workdir / ".sdd").mkdir(parents=True)
    client = _make_client()
    backend = DockerSandboxBackend(client=client)

    with pytest.raises(RuntimeError, match=r"\.git"):
        asyncio.run(backend.create(_repo_manifest(workdir)))
    client.containers.run.assert_not_called()


def test_unreadable_gitfile_fails_before_any_container_starts(tmp_path: Path) -> None:
    """A ``.git`` file that does not point at a git dir is refused."""
    workdir = tmp_path / "project"
    workdir.mkdir()
    (workdir / ".git").write_text("not a gitfile\n")
    client = _make_client()
    backend = DockerSandboxBackend(client=client)

    with pytest.raises(RuntimeError, match="gitdir"):
        asyncio.run(backend.create(_repo_manifest(workdir)))
    client.containers.run.assert_not_called()


def test_gitfile_pointing_back_at_the_workdir_is_refused(tmp_path: Path) -> None:
    """A ``gitdir:`` that resolves to the workdir (or above it) would expose the tree."""
    workdir = tmp_path / "project"
    (workdir / ".sdd" / "auth").mkdir(parents=True)
    (workdir / "HEAD").write_text("ref: refs/heads/main\n")
    (workdir / ".git").write_text("gitdir: .\n")
    client = _make_client()
    backend = DockerSandboxBackend(client=client)

    with pytest.raises(RuntimeError, match="gitdir"):
        asyncio.run(backend.create(_repo_manifest(workdir)))
    client.containers.run.assert_not_called()


def test_gitfile_pointing_at_missing_dir_fails(tmp_path: Path) -> None:
    """A ``gitdir:`` target that does not exist is refused."""
    workdir = tmp_path / "project"
    workdir.mkdir()
    (workdir / ".git").write_text("gitdir: ../nowhere\n")
    client = _make_client()
    backend = DockerSandboxBackend(client=client)

    with pytest.raises(RuntimeError, match="gitdir"):
        asyncio.run(backend.create(_repo_manifest(workdir)))
    client.containers.run.assert_not_called()
