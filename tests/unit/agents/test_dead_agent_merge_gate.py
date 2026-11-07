"""A dying agent's work reaches the run branch only as an accepted attempt (#6539).

The dead-agent, wall-clock and idle-recycle paths used to commit whatever a
dying agent left in its worktree and merge it into the run branch, whatever
the task's state. These tests drive those paths against a real repository
and real worktrees: the merge step is a plain ``git merge`` of the agent
branch, which is what the spawner's merge-back does to the run branch.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from bernstein.core.models import AgentSession, ModelConfig

import bernstein.core.agents.agent_lifecycle as al
from bernstein.core.agents import agent_recycling as ar
from bernstein.core.git.worktree import WorktreeManager
from bernstein.core.replay.review_board import EVENT_TASK_MERGED, EVENT_TASK_SALVAGED


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def _session(sid: str, *, status: str = "dead") -> AgentSession:
    return AgentSession(
        id=sid,
        role="backend",
        task_ids=["T-1"],
        status=status,
        spawn_ts=100.0,
        model_config=ModelConfig("sonnet", "high"),
    )


class _Client:
    """Task server stub: every task reads back in *status*; posts are kept."""

    def __init__(self, status: str | Exception) -> None:
        self.status = status
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **_: Any) -> Any:
        if isinstance(self.status, Exception):
            raise self.status
        task = {
            "id": url.rsplit("/", 1)[-1],
            "title": "t",
            "description": "d",
            "role": "backend",
            "status": self.status,
        }
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: task, status_code=200)

    def post(self, url: str, json: dict[str, Any] | None = None, **_: Any) -> Any:
        self.posts.append((url, json or {}))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {}, status_code=200)


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


class _Spawner:
    """Real worktrees; the merge-back is a plain merge of ``agent/<id>`` into main."""

    def __init__(self, repo: Path, wm: WorktreeManager) -> None:
        self.repo = repo
        self.wm = wm
        self.paths: dict[str, Path] = {}
        self.reap_calls: list[dict[str, bool]] = []
        self.kill = MagicMock()

    def get_worktree_path(self, sid: str) -> Path | None:
        path = self.paths.get(sid)
        return path if path is not None and path.exists() else None

    def reap_completed_agent(
        self, session: AgentSession, *, skip_merge: bool = False, defer_cleanup: bool = False
    ) -> Any:
        self.reap_calls.append({"skip_merge": skip_merge, "defer_cleanup": defer_cleanup})
        result = None
        if not skip_merge:
            _git(self.repo, "merge", "--no-ff", "--no-edit", f"agent/{session.id}")
            result = SimpleNamespace(success=True, merge_commit=_git(self.repo, "rev-parse", "HEAD"))
        if not defer_cleanup:
            self.cleanup_worktree(session.id)
        return result

    def cleanup_worktree(self, sid: str) -> None:
        self.wm.cleanup(sid)


@pytest.fixture
def run(tmp_path: Path) -> SimpleNamespace:
    """A repo on ``main`` and agent A-1's worktree holding uncommitted edits plus backup files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "seed")
    wm = WorktreeManager(repo, salvage_push=False)
    spawner = _Spawner(repo, wm)
    wt = wm.create("A-1")
    spawner.paths["A-1"] = wt
    (wt / "app.py").write_text("x = 2\n", encoding="utf-8")
    (wt / "app.py.bak").write_text("x = 1\n", encoding="utf-8")
    (wt / "notes.orig").write_text("old\n", encoding="utf-8")
    (wt / "draft.py~").write_text("y = 0\n", encoding="utf-8")
    return SimpleNamespace(repo=repo, wm=wm, spawner=spawner, wt=wt)


def _orch(run: SimpleNamespace, client: _Client, recorder: _Recorder) -> SimpleNamespace:
    return SimpleNamespace(
        _config=SimpleNamespace(server_url="http://server", evolve_mode=False),
        _client=client,
        _recorder=recorder,
        _spawner=run.spawner,
        _preserved_worktrees={},
        _agent_failure_timestamps={},
        _crash_counts={},
        _rate_limit_tracker=None,
        _signal_mgr=SimpleNamespace(clear_signals=lambda sid: None, write_shutdown=lambda *a, **k: None),
        _idle_shutdown_ts={},
        _workdir=run.repo,
    )


def _quiet_death_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "transition_agent",
        "_capture_agent_crash",
        "_propagate_abort_to_children",
        "_release_file_ownership",
        "_release_task_to_session",
        "_preserve_runner_logs",
        "_write_retry_checkpoint",
        "_maybe_preserve_worktree",
        "_handle_orphaned_task_guarded",
    ):
        monkeypatch.setattr(al, name, lambda *a, **k: None)


def _handle_dead(monkeypatch: pytest.MonkeyPatch, orch: SimpleNamespace, sid: str = "A-1") -> None:
    _quiet_death_side_effects(monkeypatch)
    al._handle_dead_agent(orch, _session(sid), {"open": [], "claimed": [], "in_progress": [], "done": []})


def _tree(repo: Path, rev: str) -> set[str]:
    return set(_git(repo, "ls-tree", "-r", "--name-only", rev).splitlines())


def test_failed_attempt_leaves_the_run_branch_byte_identical(
    monkeypatch: pytest.MonkeyPatch, run: SimpleNamespace
) -> None:
    """A failed attempt merges nothing; its edits are kept on a salvage ref named on the task."""
    before = _git(run.repo, "rev-parse", "main")
    client, recorder = _Client("failed"), _Recorder()

    _handle_dead(monkeypatch, _orch(run, client, recorder))

    assert _git(run.repo, "rev-parse", "main") == before
    assert run.spawner.reap_calls == [{"skip_merge": True, "defer_cleanup": True}]
    ref = "refs/heads/salvage/A-1"
    sha = _git(run.repo, "rev-parse", "--verify", ref)
    assert _git(run.repo, "show", f"{sha}:app.py") == "x = 2"
    assert {"app.py.bak", "notes.orig", "draft.py~"}.isdisjoint(_tree(run.repo, sha))
    assert [e for e, _ in recorder.events] == [EVENT_TASK_SALVAGED]
    _, fields = recorder.events[0]
    assert (fields["task_id"], fields["agent_id"], fields["ref"], fields["commit"]) == ("T-1", "A-1", ref, sha)
    progress = [body for url, body in client.posts if url == "http://server/tasks/T-1/progress"]
    assert len(progress) == 1
    assert sha in progress[0]["message"]


def test_requeued_attempt_is_not_merged_either(monkeypatch: pytest.MonkeyPatch, run: SimpleNamespace) -> None:
    """A task sent back to the queue for a retry is not an accepted attempt."""
    before = _git(run.repo, "rev-parse", "main")

    _handle_dead(monkeypatch, _orch(run, _Client("open"), _Recorder()))

    assert _git(run.repo, "rev-parse", "main") == before
    assert _git(run.repo, "rev-parse", "--verify", "refs/heads/salvage/A-1")


def test_unreachable_task_server_preserves_without_merging(
    monkeypatch: pytest.MonkeyPatch, run: SimpleNamespace
) -> None:
    """When the task state cannot be read, the work is kept and nothing is merged."""
    before = _git(run.repo, "rev-parse", "main")

    _handle_dead(monkeypatch, _orch(run, _Client(httpx.ConnectError("down")), _Recorder()))

    assert _git(run.repo, "rev-parse", "main") == before
    assert _git(run.repo, "rev-parse", "--verify", "refs/heads/salvage/A-1")


def test_accepted_attempt_still_merges_without_backup_files(
    monkeypatch: pytest.MonkeyPatch, run: SimpleNamespace
) -> None:
    """A task already ``done`` (accepted, not merged yet) keeps the merge, minus editor and backup files."""
    recorder = _Recorder()

    _handle_dead(monkeypatch, _orch(run, _Client("done"), recorder))

    assert run.spawner.reap_calls[0]["skip_merge"] is False
    assert _git(run.repo, "show", "main:app.py") == "x = 2"
    assert {"app.py.bak", "notes.orig", "draft.py~"}.isdisjoint(_tree(run.repo, "main"))
    assert [e for e, _ in recorder.events] == [EVENT_TASK_MERGED]


def test_closed_task_leftovers_are_not_merged(monkeypatch: pytest.MonkeyPatch, run: SimpleNamespace) -> None:
    """After the gate merged and closed the task, files the agent left behind stay off the run branch."""
    before = _git(run.repo, "rev-parse", "main")

    _handle_dead(monkeypatch, _orch(run, _Client("closed"), _Recorder()))

    assert _git(run.repo, "rev-parse", "main") == before


def test_retry_after_a_salvaged_attempt_lands_once(monkeypatch: pytest.MonkeyPatch, run: SimpleNamespace) -> None:
    """The failed attempt adds nothing to main, so the retry's commit is the only change there."""
    _handle_dead(monkeypatch, _orch(run, _Client("failed"), _Recorder()))

    retry_wt = run.wm.create("A-2")
    run.spawner.paths["A-2"] = retry_wt
    (retry_wt / "app.py").write_text("x = 2\n", encoding="utf-8")
    _git(retry_wt, "add", "app.py")
    _git(retry_wt, "commit", "-m", "retry: set x")
    # An accepted attempt reaches main through the merge-back of its own completion.
    run.spawner.reap_completed_agent(_session("A-2", status="working"), skip_merge=False)

    assert _git(run.repo, "log", "--format=%s", "main", "--", "app.py").splitlines() == ["retry: set x", "seed"]
    assert "[WIP]" not in _git(run.repo, "log", "--format=%s", "main")


def test_idle_recycle_kill_of_an_unaccepted_attempt_merges_nothing(run: SimpleNamespace) -> None:
    """The idle-recycle force-kill path follows the same rule as the crash path."""
    before = _git(run.repo, "rev-parse", "main")
    orch = _orch(run, _Client("failed"), _Recorder())
    session = _session("A-1", status="working")
    now = time.time()
    orch._idle_shutdown_ts[session.id] = now - ar._IDLE_GRACE_S - 1

    with (
        patch.object(ar, "_propagate_abort_to_children"),
        patch("bernstein.core.metrics.get_collector", return_value=MagicMock()),
        patch.object(al, "_preserve_runner_logs", return_value=None),
    ):
        ar._recycle_or_kill(orch, session, now, "task_resolved")

    run.spawner.kill.assert_called_once()
    assert _git(run.repo, "rev-parse", "main") == before
    assert _git(run.repo, "rev-parse", "--verify", "refs/heads/salvage/A-1")


def test_completed_path_still_commits_and_merges(tmp_path: Path) -> None:
    """The completion-marker reap is unchanged: it saves and merges whatever the agent left."""
    completed = tmp_path / ".sdd" / "runtime" / "completed"
    completed.mkdir(parents=True)
    (completed / "A-1").write_text("", encoding="utf-8")
    session = _session("A-1", status="working")
    orch = SimpleNamespace(
        _agents={"A-1": session},
        _workdir=tmp_path,
        _spawner=MagicMock(),
        _signal_mgr=MagicMock(),
        _idle_shutdown_ts={},
        _config=SimpleNamespace(evolve_mode=False),
        _recorder=None,
    )

    with (
        patch.object(ar, "_save_partial_work") as save,
        patch.object(ar, "_propagate_abort_to_children"),
        patch("bernstein.core.metrics.get_collector", return_value=MagicMock()),
    ):
        ar._reap_completed_agent(orch, session, completed / "A-1")

    save.assert_called_once()
    assert save.call_args.kwargs["reason"] == "completed"
