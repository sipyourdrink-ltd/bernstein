"""A remote completion with nothing posted fails closed (issue #6611).

A worker outside the host hands its output back as a posted artifact. When it
posted nothing verifiable, the alive-exit pass used to grade the run checkout
instead, so stale bytes an earlier run left at the declared output path (or a
file that already satisfied a ``path_exists`` signal) passed the task without
any remote work. A completion claimed by a registered cluster node now fails
with ``artifact_missing_remote``; every other no-session completion keeps the
previous behaviour.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from bernstein.core.models import CompletionSignal, Task, TaskType

from bernstein.core.git import remote_diff_ingest
from bernstein.core.tasks import task_lifecycle
from bernstein.core.tasks.artifacts import ArtifactCriterion, ArtifactKind, ArtifactSpec, artifact_content_hash
from bernstein.core.tasks.task_lifecycle import _enqueue_alive_exit_janitor_pass, _verify_against_merge_preview

_NODE = "node-7f3a"
_SERVER = "http://127.0.0.1:8052"
_STALE = "stale report an earlier run left in the run checkout\n"


class _Client:
    """Answers ``GET /cluster/nodes`` and records every call."""

    def __init__(self, node_ids: list[str] | Exception) -> None:
        self.node_ids = node_ids
        self.gets: list[str] = []

    def get(self, url: str, **_: Any) -> Any:
        self.gets.append(url)
        if isinstance(self.node_ids, Exception):
            raise self.node_ids
        body = [{"id": node_id, "status": "offline"} for node_id in self.node_ids]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)


def _orch(run: Path, client: _Client) -> Any:
    submitted: list[tuple[Any, tuple[Any, ...]]] = []
    return SimpleNamespace(
        _executor=SimpleNamespace(submit=lambda fn, *a, **k: submitted.append((fn, a)) or SimpleNamespace()),
        _submitted=submitted,
        _processed_done_tasks={},
        _config=SimpleNamespace(server_url=_SERVER),
        _client=client,
        _task_to_session={},
        _workdir=run,
        _find_session_for_task=lambda _tid: None,
    )


def _artifact_task(run: Path, claimed_by: str | None) -> Task:
    """A report task whose criteria the stale run-checkout file satisfies exactly."""
    (run / "out").mkdir(parents=True, exist_ok=True)
    (run / "out" / "report.md").write_text(_STALE, encoding="utf-8")
    task = Task(
        id="R-1",
        title="t",
        description="d",
        role="qa",
        task_type=TaskType.STANDARD,
        status=task_lifecycle.TaskStatus.DONE,
    )
    task.artifact_spec = ArtifactSpec(
        kind=ArtifactKind.REPORT,
        output_path="out/report.md",
        criteria=(ArtifactCriterion(type="hash_stable", value=artifact_content_hash(ArtifactKind.REPORT, _STALE)),),
    )
    task.claimed_by_session = claimed_by
    return task


def _diff_task(run: Path, claimed_by: str | None) -> Task:
    """A code_diff task whose only signal the run checkout already satisfies."""
    (run / "already_there.py").write_text("x = 1\n", encoding="utf-8")
    task = Task(
        id="D-1",
        title="t",
        description="d",
        role="backend",
        task_type=TaskType.STANDARD,
        status=task_lifecycle.TaskStatus.DONE,
    )
    task.completion_signals = [CompletionSignal(type="path_exists", value="already_there.py")]
    task.claimed_by_session = claimed_by
    return task


@pytest.fixture(autouse=True)
def _nothing_posted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_diff_ingest, "ingest_remote_artifact", lambda *a, **k: None)
    monkeypatch.setattr(remote_diff_ingest, "ingest_remote_diff", lambda *a, **k: None)
    monkeypatch.setattr("bernstein.core.security.audit.load_or_create_audit_key", lambda: b"k" * 32)


@pytest.mark.parametrize("make_task", [_artifact_task, _diff_task], ids=["artifact-mode", "code-diff"])
def test_completion_by_a_cluster_node_with_nothing_posted_fails_closed(tmp_path: Path, make_task: Any) -> None:
    client = _Client([_NODE, "node-other"])
    orch = _orch(tmp_path, client)
    task = make_task(tmp_path, _NODE)

    future = _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick")

    assert future is not None
    passed, failures = future.result()
    assert passed is False
    assert len(failures) == 1
    assert failures[0].startswith("artifact_missing_remote:")
    assert _NODE in failures[0]
    # The run checkout was never graded in its place.
    assert orch._submitted == []
    assert client.gets == [f"{_SERVER}/cluster/nodes"]


@pytest.mark.parametrize(
    ("claimed_by", "node_ids"),
    [
        ("orchestrator-session", [_NODE]),
        (_NODE, RuntimeError("cluster API down")),
        (None, [_NODE]),
    ],
    ids=["not-a-registered-node", "cluster-api-unreachable", "no-claim-owner"],
)
def test_other_no_session_completions_keep_grading_the_run_checkout(
    tmp_path: Path, claimed_by: str | None, node_ids: list[str] | Exception
) -> None:
    client = _Client(node_ids)
    orch = _orch(tmp_path, client)
    task = _artifact_task(tmp_path, claimed_by)

    _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick")

    ((fn, args),) = orch._submitted
    assert fn is not _verify_against_merge_preview
    assert args[1] == tmp_path
    if claimed_by is None:
        assert client.gets == []


def test_a_posted_artifact_is_graded_on_its_branch_without_asking_the_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(remote_diff_ingest, "ingest_remote_artifact", lambda *a, **k: "agent/remote-R-1-v1")
    client = _Client([_NODE])
    orch = _orch(tmp_path, client)
    task = _artifact_task(tmp_path, _NODE)

    _enqueue_alive_exit_janitor_pass(orch, task, reason="alive_exit_tick")

    ((fn, args),) = orch._submitted
    assert fn is _verify_against_merge_preview
    assert args[3] == "agent/remote-R-1-v1"
    assert client.gets == []
