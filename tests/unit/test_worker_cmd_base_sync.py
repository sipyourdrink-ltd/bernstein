"""Worker base sync and repository-identity refusal (#6538).

A ``bernstein worker`` node runs each claimed task in a worktree of its own
workspace, branched from the workspace HEAD. Before this fix the workspace
stayed at its clone-time commit, so every task branched from stale code, and
nothing stopped a worker whose workspace was a checkout of the *wrong*
repository from claiming - and "completing" - tasks whose work could never
merge. The worker now fetches the run branch and fast-forwards the workspace
before spawning (``_sync_task_base``), and refuses a claim whose
``repo_root_commit`` does not match the workspace's root commit
(``_check_claim_repository``), releasing the task back to the pool.

The git behaviour is exercised against real synthetic repositories - a bare
origin plus "central" and "worker" clones - with no network access.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from bernstein.cli.commands.worker_cmd import WorkerLoop
from bernstein.core.git.worktree import WorktreeManager

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(("git", *args), cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "worker-base-sync@example.invalid")
    _git(path, "config", "user.name", "Worker Base Sync Test")


def _commit_file(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _root_commit(repo: Path) -> str:
    return _git(repo, "rev-list", "--max-parents=0", "HEAD").splitlines()[0]


def _make_loop(workdir: Path) -> WorkerLoop:
    return WorkerLoop(
        server_url="http://central:8052",
        name="test-node",
        auth_token="secret-token",
        adapter="claude",
        roles=["backend"],
        workdir=workdir,
    )


@pytest.fixture
def cluster_repos(tmp_path: Path) -> tuple[Path, Path]:
    """A bare origin plus "central" and "worker" clones, all on ``main``.

    Returns ``(central, worker)``. Tests advance the central and push so
    origin's ``main`` moves past the commit the worker cloned at - the exact
    staleness window #6538 describes.
    """
    seed = tmp_path / "seed"
    _init_repo(seed)
    _commit_file(seed, "app.py", "print('seed')\n")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    central = tmp_path / "central"
    worker = tmp_path / "worker"
    _git(tmp_path, "clone", "-q", str(origin), str(central))
    _git(tmp_path, "clone", "-q", str(origin), str(worker))
    for repo in (central, worker):
        _git(repo, "config", "user.email", "worker-base-sync@example.invalid")
        _git(repo, "config", "user.name", "Worker Base Sync Test")
    return central, worker


class TestSyncTaskBase:
    def test_worktree_created_from_fetched_base_sha(self, cluster_repos: tuple[Path, Path]) -> None:
        """Acceptance: a task worktree starts at the fetched run-branch head."""
        central, worker = cluster_repos
        stale_head = _git(worker, "rev-parse", "HEAD")
        # The central accepts work after the worker cloned: origin moves on.
        accepted = _commit_file(central, "feature.py", "print('accepted upstream')\n")
        _git(central, "push", "-q", "origin", "main")
        assert accepted != stale_head
        loop = _make_loop(worker)

        assert loop._sync_task_base({}) is None

        # The workspace fast-forwarded to the fetched head, and the spawner's
        # worktree (WorktreeManager.create branches from HEAD) starts there.
        assert _git(worker, "rev-parse", "HEAD") == accepted
        worktree = WorktreeManager(worker).create("sess-base")
        assert _git(worktree, "rev-parse", "HEAD") == accepted
        assert (worktree / "feature.py").read_text(encoding="utf-8") == "print('accepted upstream')\n"

    def test_sync_when_already_current_is_a_noop(self, cluster_repos: tuple[Path, Path]) -> None:
        _central, worker = cluster_repos
        head = _git(worker, "rev-parse", "HEAD")

        assert _make_loop(worker)._sync_task_base({}) is None

        assert _git(worker, "rev-parse", "HEAD") == head

    def test_fetch_failure_refuses_the_claim(self, cluster_repos: tuple[Path, Path]) -> None:
        """No silent fallback to the stale local HEAD when the fetch fails."""
        _central, worker = cluster_repos
        _git(worker, "remote", "set-url", "origin", "/nonexistent/origin.git")

        refusal = _make_loop(worker)._sync_task_base({})

        assert refusal is not None
        assert "could not fetch" in refusal

    def test_claimed_base_ref_must_match_the_workspace_branch(self, cluster_repos: tuple[Path, Path]) -> None:
        _central, worker = cluster_repos

        refusal = _make_loop(worker)._sync_task_base({"base_ref": "release"})

        assert refusal is not None
        assert "does not match the workspace branch" in refusal

    def test_claimed_base_sha_must_be_reachable_from_the_fetched_head(
        self, cluster_repos: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """A claim pinned to history this remote does not have cannot start."""
        _central, worker = cluster_repos
        unrelated = tmp_path / "unrelated"
        _init_repo(unrelated)
        foreign_sha = _commit_file(unrelated, "x.py", "x = 1\n")

        refusal = _make_loop(worker)._sync_task_base({"base_sha": foreign_sha})

        assert refusal is not None
        assert "not reachable" in refusal

    def test_claimed_base_sha_on_the_run_branch_is_accepted(self, cluster_repos: tuple[Path, Path]) -> None:
        central, worker = cluster_repos
        accepted = _commit_file(central, "feature.py", "print('v2')\n")
        _git(central, "push", "-q", "origin", "main")
        loop = _make_loop(worker)

        assert loop._sync_task_base({"base_ref": "main", "base_sha": accepted}) is None

        assert _git(worker, "rev-parse", "HEAD") == accepted

    def test_malformed_base_sha_is_refused(self, cluster_repos: tuple[Path, Path]) -> None:
        """Claim-supplied values are validated before they reach a git argv."""
        _central, worker = cluster_repos

        refusal = _make_loop(worker)._sync_task_base({"base_sha": "--upload-pack=touch /tmp/pwn"})

        assert refusal is not None
        assert "malformed base_sha" in refusal


class TestClaimRepositoryIdentity:
    def test_workspace_repo_identity_is_the_root_commit(self, cluster_repos: tuple[Path, Path]) -> None:
        central, worker = cluster_repos

        assert _make_loop(worker)._workspace_repo_identity() == _root_commit(worker)
        # Clones of the same repository agree on the identity.
        assert _make_loop(worker)._workspace_repo_identity() == _make_loop(central)._workspace_repo_identity()

    def test_repository_mismatch_refuses_claim(self, cluster_repos: tuple[Path, Path], tmp_path: Path) -> None:
        """Acceptance: two synthetic repositories cannot exchange work."""
        _central, worker = cluster_repos
        foreign = tmp_path / "foreign"
        _init_repo(foreign)
        _commit_file(foreign, "other.py", "other = True\n")
        loop = _make_loop(worker)
        client = mock.MagicMock()
        task = {"id": "task-x", "title": "t", "role": "backend", "repo_root_commit": _root_commit(foreign)}

        with (
            mock.patch.object(loop, "_claim_task", return_value=task),
            mock.patch.object(loop, "_spawn_agent") as spawn,
        ):
            loop._claim_available_tasks(client, node_id="node-1")

        spawn.assert_not_called()
        releases = [
            call
            for call in client.post.call_args_list
            if call.args and str(call.args[0]).endswith("/tasks/task-x/release")
        ]
        assert releases, f"expected a release POST, got: {client.post.call_args_list}"
        assert "repository mismatch" in releases[0].kwargs["json"]["reason"]
        assert "task-x" not in loop._active_tasks

        # Latched: later cycles do not even claim from this central.
        with mock.patch.object(loop, "_claim_task") as claim:
            loop._claim_available_tasks(client, node_id="node-1")
        claim.assert_not_called()

    def test_matching_identity_claim_spawns(self, cluster_repos: tuple[Path, Path]) -> None:
        _central, worker = cluster_repos
        loop = _make_loop(worker)
        client = mock.MagicMock()
        task = {"id": "task-ok", "title": "t", "role": "backend", "repo_root_commit": _root_commit(worker)}

        with (
            mock.patch.object(loop, "_claim_task", return_value=task),
            mock.patch.object(loop, "_spawn_agent", return_value=4242) as spawn,
        ):
            loop._claim_available_tasks(client, node_id="node-1")

        spawn.assert_called_once()
        releases = [
            call
            for call in client.post.call_args_list
            if call.args and str(call.args[0]).endswith("/tasks/task-ok/release")
        ]
        assert not releases
        assert loop._active_tasks["task-ok"] == 4242

    def test_legacy_claim_without_identity_still_spawns(self, cluster_repos: tuple[Path, Path]) -> None:
        """An older central stamps no identity; the claim is not refused."""
        _central, worker = cluster_repos
        loop = _make_loop(worker)
        client = mock.MagicMock()
        task = {"id": "task-legacy", "title": "t", "role": "backend"}

        with (
            mock.patch.object(loop, "_claim_task", return_value=task),
            mock.patch.object(loop, "_spawn_agent", return_value=4242) as spawn,
        ):
            loop._claim_available_tasks(client, node_id="node-1")

        spawn.assert_called_once()
        assert loop._active_tasks["task-legacy"] == 4242

    def test_workspace_ahead_of_fetched_base_is_refused(self, cluster_repos: tuple[Path, Path]) -> None:
        """Unpushed local commits ahead of origin must be refused even when --ff-only says up to date."""
        _central, worker = cluster_repos
        (worker / "local_only.txt").write_text("unpushed\n", encoding="utf-8")
        _git(worker, "add", "local_only.txt")
        _git(worker, "commit", "-q", "-m", "unpushed local commit")
        loop = _make_loop(worker)

        refusal = loop._sync_task_base({"id": "task-ahead", "title": "t", "role": "backend"})

        assert refusal is not None
        assert "ahead of fetched" in refusal

    def test_shallow_clone_deepens_and_matches_central_identity(
        self, cluster_repos: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """A depth-1 worker clone unshallows before computing repo identity rather than latching a mismatch."""
        central, _worker = cluster_repos
        (central / "extra.txt").write_text("second commit\n", encoding="utf-8")
        _git(central, "add", "extra.txt")
        _git(central, "commit", "-q", "-m", "second commit")
        central_root = _root_commit(central)

        shallow_worker = tmp_path / "shallow-worker"
        subprocess.run(
            ("git", "clone", "-q", "--depth", "1", f"file://{central}", str(shallow_worker)),
            check=True,
        )
        assert _git(shallow_worker, "rev-parse", "--is-shallow-repository") == "true"

        loop = _make_loop(shallow_worker)
        refusal = loop._check_claim_repository(
            {"id": "task-shallow", "title": "t", "role": "backend", "repo_root_commit": central_root}
        )

        assert refusal is None
        assert loop._repo_mismatch_refusal is None
        assert _git(shallow_worker, "rev-parse", "--is-shallow-repository") == "false"
