"""Retry-path wiring for checkpointed retries (#2359).

``retry_or_fail_task`` and ``maybe_retry_task`` must stamp the deterministic
checkpoint-retry decision onto the retried task's metadata: warm when a
verified checkpoint matches the live workspace, cold (recorded as such) when
there is no checkpoint, no capability, or a workspace-hash mismatch. The stamp
is best-effort: it must never break the retry itself.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from bernstein.core.tasks.checkpoint_retry import (
    record_task_checkpoint,
    workspace_hash,
)
from bernstein.core.tasks.task_lifecycle import (
    _write_retry_checkpoint,
    maybe_retry_task,
    retry_or_fail_task,
)


class _Scope:
    value = "small"


class _Complexity:
    value = "low"


class _TaskType:
    value = "feature"


class _Task:
    def __init__(self, task_id: str) -> None:
        self.id = task_id
        self.title = "Test Task"
        self.description = "desc"
        self.role = "backend"
        self.priority = 1
        self.scope = _Scope()
        self.complexity = _Complexity()
        self.estimated_minutes = 10
        self.depends_on: list[str] = []
        self.owned_files: list[str] = []
        self.task_type = _TaskType()
        self.model = "sonnet"
        self.effort = "high"
        self.max_output_tokens = None
        self.max_turns = None
        self.meta_messages: list[str] = []
        self.completion_signals: list[Any] = []
        self.metadata: dict[str, Any] = {}
        self.retry_count = 0
        self.max_retries = 3
        self.retry_delay_s = 0.0
        self.terminal_reason = None
        self.deadline = None
        self.agent_restart_between_retries = False


def _posted_metadata(mock_client: MagicMock) -> dict[str, Any]:
    for call in mock_client.post.call_args_list:
        if call[0][0].endswith("/tasks"):
            return call[1]["json"]["metadata"]
    raise AssertionError("no retry task was posted")


def _make_worktree(root: Path) -> Path:
    tree = root / "wt"
    tree.mkdir()
    (tree / "main.py").write_text("print('x')\n", encoding="utf-8")
    return tree


def test_retry_or_fail_stamps_warm_decision(tmp_path: Path) -> None:
    tree = _make_worktree(tmp_path)
    record_task_checkpoint(
        sdd_dir=tmp_path / ".sdd",
        task_id="task-1",
        adapter="claude",
        session_id="sess-1",
        workspace_hash=workspace_hash(tree),
        worktree_path=str(tree),
    )
    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-1")
    retry_or_fail_task(
        task_id="task-1",
        reason="rate limit",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "warm"
    assert metadata["retry_checkpoint_session_id"] == "sess-1"
    assert metadata["retry_decision_hash"]


def test_retry_or_fail_stamps_cold_without_checkpoint(tmp_path: Path) -> None:
    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-2")
    retry_or_fail_task(
        task_id="task-2",
        reason="rate limit",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "cold"
    assert metadata["retry_downgrade_reason"] == "no_checkpoint"


def test_retry_or_fail_downgrades_on_workspace_mismatch(tmp_path: Path) -> None:
    tree = _make_worktree(tmp_path)
    record_task_checkpoint(
        sdd_dir=tmp_path / ".sdd",
        task_id="task-3",
        adapter="claude",
        session_id="sess-3",
        workspace_hash=workspace_hash(tree),
        worktree_path=str(tree),
    )
    (tree / "main.py").write_text("print('mutated')\n", encoding="utf-8")
    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-3")
    retry_or_fail_task(
        task_id="task-3",
        reason="rate limit",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "cold"
    assert metadata["retry_downgrade_reason"] == "workspace_hash_mismatch"


def test_fresh_context_retry_forces_cold(tmp_path: Path) -> None:
    tree = _make_worktree(tmp_path)
    record_task_checkpoint(
        sdd_dir=tmp_path / ".sdd",
        task_id="task-4",
        adapter="claude",
        session_id="sess-4",
        workspace_hash=workspace_hash(tree),
        worktree_path=str(tree),
    )
    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-4")
    task.agent_restart_between_retries = True
    retry_or_fail_task(
        task_id="task-4",
        reason="rate limit",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "cold"
    assert metadata["retry_downgrade_reason"] == "fresh_context_restart"


def test_stamp_failure_never_breaks_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import bernstein.core.tasks.checkpoint_retry as checkpoint_retry_module

    def _boom(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("stamp exploded")

    monkeypatch.setattr(checkpoint_retry_module, "stamp_checkpoint_retry_metadata", _boom)
    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-5")
    retry_or_fail_task(
        task_id="task-5",
        reason="rate limit",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "cold"


def test_no_workdir_keeps_legacy_behavior(tmp_path: Path) -> None:
    # Callers without a workdir (legacy tests, ad-hoc scripts) get the
    # historical retry body with a plain cold stamp and no decision record.
    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-6")
    retry_or_fail_task(
        task_id="task-6",
        reason="rate limit",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "cold"


def test_maybe_retry_task_stamps_decision(tmp_path: Path) -> None:
    tree = _make_worktree(tmp_path)
    record_task_checkpoint(
        sdd_dir=tmp_path / ".sdd",
        task_id="task-7",
        adapter="claude",
        session_id="sess-7",
        workspace_hash=workspace_hash(tree),
        worktree_path=str(tree),
    )
    mock_client = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.json.return_value = {"id": "task-7-retry"}
    mock_client.post.return_value = resp
    task = _Task("task-7")
    created = maybe_retry_task(
        task,
        retried_task_ids=set(),
        max_task_retries=3,
        client=mock_client,
        server_url="http://test",
        quarantine=MagicMock(),
        workdir=tmp_path,
    )
    assert created is True
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "warm"
    assert metadata["retry_checkpoint_session_id"] == "sess-7"


def _make_orch(tmp_path: Path, worktree: Path | None) -> Any:
    spawner = SimpleNamespace(
        get_worktree_path=lambda _session_id: worktree,
        default_adapter_name="claude",
    )
    return SimpleNamespace(_workdir=tmp_path, _spawner=spawner)


def _make_session(task_ids: list[str], *, session_id: str = "agent-1") -> Any:
    return SimpleNamespace(
        id=session_id,
        task_ids=task_ids,
        provider="claude",
        model_config=SimpleNamespace(model="claude-sonnet-5"),
    )


def test_write_retry_checkpoint_before_retry_stamps_warm(tmp_path: Path) -> None:
    """#5844: the ordinary crash/timeout retry path now has a real writer.

    ``_write_retry_checkpoint`` is what ``_handle_dead_agent``,
    ``_reap_wall_clock_timeout`` and ``_reap_heartbeat_timeout`` call before
    their retry decision fires, reproduced here without those orchestrator
    internals by calling it with the same surface they pass (an orch exposing
    ``_workdir``/``_spawner.get_worktree_path``/``_spawner.default_adapter_name``
    and a session exposing ``id``/``task_ids``/``provider``/``model_config``).
    """
    tree = _make_worktree(tmp_path)
    session = _make_session(["task-8"], session_id="sess-8")
    orch = _make_orch(tmp_path, tree)

    _write_retry_checkpoint(orch, session, detector="crash")

    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-8")
    retry_or_fail_task(
        task_id="task-8",
        reason="agent crashed",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "warm"
    assert metadata["retry_checkpoint_session_id"] == "sess-8"
    assert "retry_downgrade_reason" not in metadata


def test_write_retry_checkpoint_empty_session_id_downgrades(tmp_path: Path) -> None:
    """A checkpoint recorded with no native session id must still downgrade.

    Named explicitly by the maintainer alongside the fix: a writer that
    records an empty ``session_id`` produces a checkpoint that downgrades
    with ``no_session_id`` rather than silently resuming nothing.
    """
    tree = _make_worktree(tmp_path)
    session = _make_session(["task-9"], session_id="")
    orch = _make_orch(tmp_path, tree)

    _write_retry_checkpoint(orch, session, detector="crash")

    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-9")
    retry_or_fail_task(
        task_id="task-9",
        reason="agent crashed",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "cold"
    assert metadata["retry_downgrade_reason"] == "no_session_id"


def test_write_retry_checkpoint_empty_adapter_downgrades(tmp_path: Path) -> None:
    """A third trap of the same class as the session-id and hash traps above.

    ``adapter_name_for_provider`` and ``default_adapter_name`` can both miss
    (an unrecognised provider/model with no configured default), so the
    checkpoint records ``adapter=""``. ``decide_retry`` treats a falsy
    adapter as ``CheckpointRetryCapability.NONE``, so this must downgrade
    with a named reason (``adapter_capability_none``) rather than attempt a
    resume under an empty adapter name.
    """
    tree = _make_worktree(tmp_path)
    session = _make_session(["task-15"], session_id="sess-15")
    session.provider = "totally-unrecognised-provider"
    session.model_config = SimpleNamespace(model="totally-unrecognised-model")
    orch = _make_orch(tmp_path, tree)
    orch._spawner.default_adapter_name = None  # both resolution routes miss

    _write_retry_checkpoint(orch, session, detector="crash")

    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-15")
    retry_or_fail_task(
        task_id="task-15",
        reason="agent crashed",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "cold"
    assert metadata["retry_downgrade_reason"] == "adapter_capability_none"


def test_workspace_hash_unaffected_by_wip_commit(tmp_path: Path) -> None:
    """Review question: does ``_save_partial_work``'s WIP git commit change
    the ``workspace_hash`` a checkpoint already recorded?

    ``workspace_hash`` walks the filesystem tree and explicitly excludes
    ``.git``, and ``_save_partial_work`` only ever stages and commits
    content that is already sitting in the worktree (``git add -A`` plus a
    ``[WIP]`` commit), it does not edit any file. So a real WIP commit
    over the same tree must not move the hash.
    """
    import subprocess

    tree = _make_worktree(tmp_path)
    (tree / "output.txt").write_text("agent wrote this before dying\n", encoding="utf-8")

    before = workspace_hash(tree)
    subprocess.run(["git", "init", "-q"], cwd=str(tree), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tree), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tree), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(tree), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "[WIP] agent-1 partial work"],
        cwd=str(tree),
        check=True,
        capture_output=True,
    )
    after = workspace_hash(tree)

    assert before != ""
    assert before == after


def test_write_retry_checkpoint_workspace_mutated_after_write_downgrades(tmp_path: Path) -> None:
    """The other trap named alongside the fix: a workspace hashed too late.

    Once ``_write_retry_checkpoint`` has recorded the workspace hash, any
    further change to the worktree (e.g. a cleanup step running after the
    checkpoint instead of before it) must downgrade the retry to cold with
    ``workspace_hash_mismatch`` rather than resuming against a workspace the
    checkpoint no longer describes.
    """
    tree = _make_worktree(tmp_path)
    session = _make_session(["task-10"], session_id="sess-10")
    orch = _make_orch(tmp_path, tree)

    _write_retry_checkpoint(orch, session, detector="crash")
    (tree / "main.py").write_text("print('mutated')\n", encoding="utf-8")

    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-10")
    retry_or_fail_task(
        task_id="task-10",
        reason="agent crashed",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "cold"
    assert metadata["retry_downgrade_reason"] == "workspace_hash_mismatch"


def test_write_retry_checkpoint_no_worktree_never_raises(tmp_path: Path) -> None:
    """Fail-open: no resolvable worktree must not block the retry that follows."""
    session = _make_session(["task-11"], session_id="sess-11")
    orch = _make_orch(tmp_path, worktree=None)

    _write_retry_checkpoint(orch, session, detector="crash")  # must not raise

    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-11")
    retry_or_fail_task(
        task_id="task-11",
        reason="agent crashed",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "cold"
    assert metadata["retry_downgrade_reason"] == "no_checkpoint"


def test_write_retry_checkpoint_empty_task_ids_is_noop(tmp_path: Path) -> None:
    """A session with no task ids has nothing to checkpoint; must not raise."""
    tree = _make_worktree(tmp_path)
    session = _make_session([], session_id="sess-12")
    calls: list[str] = []
    orch = _make_orch(tmp_path, tree)
    orch._spawner.get_worktree_path = lambda session_id: calls.append(session_id) or tree

    _write_retry_checkpoint(orch, session, detector="crash")  # must not raise

    assert calls == [], "expected no worktree lookup for a session with no task ids"


def test_write_retry_checkpoint_swallows_exception_and_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A write failure (e.g. a corrupt journal) must never block the retry that follows."""
    import logging

    session = _make_session(["task-13"], session_id="sess-13")
    orch = _make_orch(tmp_path, worktree=tmp_path / "does-not-exist")
    orch._spawner.get_worktree_path = MagicMock(side_effect=RuntimeError("worktree lookup exploded"))

    with caplog.at_level(logging.WARNING):
        _write_retry_checkpoint(orch, session, detector="crash")  # must not raise

    assert any("Could not write retry checkpoint" in r.getMessage() for r in caplog.records)
    assert any(session.id in r.getMessage() for r in caplog.records)

    mock_client = MagicMock(spec=httpx.Client)
    task = _Task("task-13")
    retry_or_fail_task(
        task_id="task-13",
        reason="agent crashed",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=set(),
        tasks_snapshot={"active": [task]},
        workdir=tmp_path,
    )
    metadata = _posted_metadata(mock_client)
    assert metadata["retry_mode"] == "cold"
    assert metadata["retry_downgrade_reason"] == "no_checkpoint"
