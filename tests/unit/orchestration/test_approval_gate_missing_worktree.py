"""An approval PR that cannot be created is surfaced, never swallowed.

``_create_approval_pr`` runs after the gate has already decided to hold the
merge: the PR it creates is the surface the operator approves on. When the
worktree is gone or ``create_pr`` returns nothing, the hold stands but the
approval can never arrive through the intended channel - the task waits
indefinitely, indistinguishable from one waiting on a reviewer. The previous
behaviour logged a warning and returned; these tests pin the replacement:
every failure path emits a ``task.approval_pr_failed`` notification naming the
task, and the success path emits nothing.
"""

from types import SimpleNamespace

from bernstein.core.tasks.task_lifecycle import _create_approval_pr


def _orch(worktree, create_pr):
    notifications = []
    orch = SimpleNamespace()
    orch._spawner = SimpleNamespace(get_worktree_path=lambda session_id: worktree)
    orch._approval_gate = SimpleNamespace(create_pr=create_pr)
    orch._config = SimpleNamespace(pr_labels=[])
    orch._notify = lambda **kw: notifications.append(kw)
    return orch, notifications


def _task():
    return SimpleNamespace(id="T-123", owned_files=[], title="dummy", description="dummy")


def _session():
    return SimpleNamespace(id="S-456", role="backend", model_config=SimpleNamespace(model="sonnet"))


def test_missing_worktree_notifies_instead_of_swallowing(tmp_path):
    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("create_pr must not be called when the worktree is missing")

    orch, notifications = _orch(worktree=None, create_pr=_must_not_be_called)

    _create_approval_pr(orch, _task(), _session(), completion_data=None)

    assert len(notifications) == 1
    note = notifications[0]
    assert note["event"] == "task.approval_pr_failed"
    assert note["task_id"] == "T-123"
    assert "no worktree" in note["body"]


def test_create_pr_returning_nothing_notifies(tmp_path, monkeypatch):
    orch, notifications = _orch(worktree=tmp_path, create_pr=lambda *a, **kw: "")
    monkeypatch.setattr(
        "bernstein.core.tasks.task_lifecycle.get_collector",
        lambda _p: SimpleNamespace(task_metrics=SimpleNamespace(get=lambda _id: None)),
    )
    orch._workdir = tmp_path

    _create_approval_pr(orch, _task(), _session(), completion_data=None)

    assert len(notifications) == 1
    assert notifications[0]["event"] == "task.approval_pr_failed"
    assert "no URL" in notifications[0]["body"]


def test_created_pr_notifies_nothing(tmp_path, monkeypatch):
    orch, notifications = _orch(worktree=tmp_path, create_pr=lambda *a, **kw: "https://example.test/pr/1")
    monkeypatch.setattr(
        "bernstein.core.tasks.task_lifecycle.get_collector",
        lambda _p: SimpleNamespace(task_metrics=SimpleNamespace(get=lambda _id: None)),
    )
    orch._workdir = tmp_path

    _create_approval_pr(orch, _task(), _session(), completion_data=None)

    assert notifications == []
