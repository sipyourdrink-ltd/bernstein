"""Retry-path wiring for checkpointed retries (#2359).

``retry_or_fail_task`` and ``maybe_retry_task`` must stamp the deterministic
checkpoint-retry decision onto the retried task's metadata: warm when a
verified checkpoint matches the live workspace, cold (recorded as such) when
there is no checkpoint, no capability, or a workspace-hash mismatch. The stamp
is best-effort: it must never break the retry itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from bernstein.core.tasks.checkpoint_retry import (
    record_task_checkpoint,
    workspace_hash,
)
from bernstein.core.tasks.task_lifecycle import maybe_retry_task, retry_or_fail_task


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
