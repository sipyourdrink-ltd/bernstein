"""Worker startup preflight: the workspace must be a usable git repo (#3018).

A cluster worker executes every claimed task by adding a git worktree under
its workspace (``AgentSpawner`` -> ``WorktreeManager``). When the workspace is
not a git checkout, the ``git worktree add`` fails with ``fatal: not a git
repository`` -- but only *after* the task has already been claimed, stranding it
in ``claimed`` with no live agent. The preflight surfaces the unusable
workspace *before* the worker registers or accepts any claim, so it refuses to
start with a clear setup error instead of claiming work it cannot run.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from bernstein.cli.commands.worker_cmd import WorkerLoop

if TYPE_CHECKING:
    from pathlib import Path


def _make_loop(workdir: Path) -> WorkerLoop:
    return WorkerLoop(
        server_url="http://central:8052",
        name="test-node",
        auth_token="secret-token",
        adapter="claude",
        workdir=workdir,
    )


def _git(workdir: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo_with_commit(workdir: Path) -> None:
    _git(workdir, "init")
    _git(workdir, "config", "user.email", "worker@example.com")
    _git(workdir, "config", "user.name", "Worker")
    (workdir / "README.md").write_text("workspace\n", encoding="utf-8")
    _git(workdir, "add", "README.md")
    _git(workdir, "commit", "-m", "init")


class TestWorkspaceSetupError:
    def test_flags_non_git_directory(self, tmp_path: Path) -> None:
        """A plain (non-git) workspace yields a clear, actionable error."""
        loop = _make_loop(tmp_path)

        err = loop._workspace_setup_error()

        assert err is not None
        low = err.lower()
        assert "git" in low
        assert "workspace" in low
        # The message must name the concrete path so the operator knows where
        # to mount/checkout the repo.
        assert str(tmp_path) in err

    def test_flags_missing_directory(self, tmp_path: Path) -> None:
        """A workspace path that does not exist is refused with a clear error."""
        missing = tmp_path / "workspace"
        loop = _make_loop(missing)

        err = loop._workspace_setup_error()

        assert err is not None
        assert str(missing) in err

    def test_flags_git_repo_without_commits(self, tmp_path: Path) -> None:
        """A git repo with no commits cannot back a ``git worktree add``.

        ``git worktree add <path> -b <branch>`` branches from HEAD; with no
        commit there is no HEAD to branch from, so an empty repo is still not a
        *usable* workspace and must be refused before any claim.
        """
        _git(tmp_path, "init")
        loop = _make_loop(tmp_path)

        err = loop._workspace_setup_error()

        assert err is not None
        assert "commit" in err.lower()

    def test_none_for_usable_git_repo(self, tmp_path: Path) -> None:
        """A git checkout with at least one commit passes preflight."""
        _init_repo_with_commit(tmp_path)
        loop = _make_loop(tmp_path)

        assert loop._workspace_setup_error() is None


class TestRunRefusesOnBadWorkspace:
    def test_run_refuses_to_register_on_non_git_workspace(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """run() aborts (non-zero exit) and never registers on a bad workspace."""
        loop = _make_loop(tmp_path)

        with mock.patch.object(loop, "_register_with_retry") as register:
            with pytest.raises(SystemExit):
                loop.run()

        # The worker must NOT have registered / accepted claims.
        register.assert_not_called()
        out = capsys.readouterr().out
        assert "Worker cannot start" in out

    def test_run_proceeds_past_preflight_for_usable_workspace(self, tmp_path: Path) -> None:
        """A usable workspace lets run() reach registration (then exit cleanly)."""
        _init_repo_with_commit(tmp_path)
        loop = _make_loop(tmp_path)

        # Registration returns None -> run() exits immediately after preflight
        # without entering the poll loop. The point is that preflight did not
        # abort: _register_with_retry WAS reached.
        with mock.patch.object(loop, "_register_with_retry", return_value=None) as register:
            loop.run()

        register.assert_called_once()
