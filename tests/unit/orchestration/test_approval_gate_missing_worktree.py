from types import SimpleNamespace

import pytest

from bernstein.core.tasks.task_lifecycle import _create_approval_pr


def test_create_approval_pr_no_worktree(monkeypatch):
    # Mock orchestrator with spawner that returns None for worktree_path
    dummy_orch = SimpleNamespace()
    dummy_orch._spawner = SimpleNamespace()
    dummy_orch._spawner.get_worktree_path = lambda session_id: None
    # Mock approval_gate.create_pr to raise if called
    dummy_orch._approval_gate = SimpleNamespace()
    dummy_orch._approval_gate.create_pr = lambda *args, **kwargs: pytest.fail(
        "create_pr should not be called when worktree missing"
    )
    # Mock config for pr_labels
    dummy_orch._config = SimpleNamespace(pr_labels=[])
    # Minimal task and session objects
    task = SimpleNamespace(id="T-123", owned_files=[], title="dummy", description="dummy")
    session = SimpleNamespace(id="S-456", role="backend", model_config=SimpleNamespace(model="sonnet"))
    # Call function and verify it returns None without error
    result = _create_approval_pr(dummy_orch, task, session, completion_data=None)
    assert result is None
