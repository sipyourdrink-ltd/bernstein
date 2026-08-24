"""Tests for the bounded merge-gate repair task (#4463).

A merge-gate failure (lint/tests) in the reap-and-merge path used to just
fail the task -- the exact gate output that explains *why* was thrown away,
leaving a human to re-read logs. These tests pin four behaviours:

* a quality-gate failure seeds exactly one repair task whose goal embeds the
  tail of the real gate output plus fix instructions, and preserves the
  failing worktree so the repair resumes on the same branch.
* a task that already carries a repair attempt does not get a second one
  (single-attempt semantics) -- the caller's existing reopen/fail path runs
  unchanged.
* a passing quality gate schedules nothing (pass-through).
* the switch -- config field or the ``BERNSTEIN_GATE_REPAIR`` env override --
  disables scheduling even on a first failure.

Plus a check that ``_reap_and_cleanup_session`` skips ``cleanup_worktree``
when a repair was scheduled, mirroring the existing merge-failure-preserves-
worktree behaviour from issue #2792.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bernstein.core.quality.quality_gates import QualityGateCheckResult, QualityGatesResult
from bernstein.core.tasks.models import AgentSession, Task, TaskStatus
from bernstein.core.tasks.task_lifecycle import (
    _build_gate_repair_goal,
    _gate_repair_enabled,
    _maybe_schedule_gate_repair,
    _reap_and_cleanup_session,
)


class _FakeResponse:
    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self._body = body or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeClient:
    """Records POSTs; POST .../tasks returns a fresh incrementing id."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 1

    def post(self, url: str, json: dict[str, Any] | None = None, **_kwargs: Any) -> _FakeResponse:
        body = json or {}
        self.posts.append((url, body))
        if url.endswith("/tasks"):
            new_id = f"repair-{self._next_id}"
            self._next_id += 1
            return _FakeResponse({"id": new_id, **body})
        return _FakeResponse({})


def _make_task(task_id: str = "task123") -> Task:
    return Task(
        id=task_id,
        title="Implement hello subcommand",
        description="Add a hello subcommand to cli.py",
        role="backend",
        status=TaskStatus.DONE,
    )


def _make_orch(client: _FakeClient, *, gate_repair_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        _config=SimpleNamespace(server_url="http://127.0.0.1:8052", gate_repair_enabled=gate_repair_enabled),
        _client=client,
        _preserved_worktrees={},
    )


def _failing_qg_result(task_id: str = "task123") -> QualityGatesResult:
    long_output = "\n".join(f"line {n}: ruff violation" for n in range(1, 61))
    return QualityGatesResult(
        task_id=task_id,
        passed=False,
        gate_results=[
            QualityGateCheckResult(gate="lint", passed=False, blocked=True, detail=long_output, status="fail"),
            QualityGateCheckResult(gate="tests", passed=True, blocked=False, detail="ok", status="pass"),
        ],
    )


def _passing_qg_result(task_id: str = "task123") -> QualityGatesResult:
    return QualityGatesResult(
        task_id=task_id,
        passed=True,
        gate_results=[QualityGateCheckResult(gate="lint", passed=True, blocked=False, detail="", status="pass")],
    )


# ---------------------------------------------------------------------------
# _build_gate_repair_goal
# ---------------------------------------------------------------------------


def test_goal_embeds_gate_output_tail_and_fix_instructions() -> None:
    goal = _build_gate_repair_goal(_failing_qg_result())

    assert "line 60: ruff violation" in goal
    assert "line 10: ruff violation" not in goal  # older than the ~40-line tail
    assert "do not rewrite the feature" in goal.lower()
    assert "smallest possible diffs" in goal.lower() or "small" in goal.lower()


# ---------------------------------------------------------------------------
# _gate_repair_enabled
# ---------------------------------------------------------------------------


def test_switch_defaults_to_config_value() -> None:
    orch_on = SimpleNamespace(_config=SimpleNamespace(gate_repair_enabled=True))
    orch_off = SimpleNamespace(_config=SimpleNamespace(gate_repair_enabled=False))
    assert _gate_repair_enabled(orch_on) is True
    assert _gate_repair_enabled(orch_off) is False


def test_switch_missing_config_field_defaults_true() -> None:
    orch = SimpleNamespace(_config=SimpleNamespace())
    assert _gate_repair_enabled(orch) is True


def test_env_override_wins_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = SimpleNamespace(_config=SimpleNamespace(gate_repair_enabled=True))
    monkeypatch.setenv("BERNSTEIN_GATE_REPAIR", "false")
    assert _gate_repair_enabled(orch) is False

    monkeypatch.setenv("BERNSTEIN_GATE_REPAIR", "0")
    assert _gate_repair_enabled(orch) is False

    orch_off = SimpleNamespace(_config=SimpleNamespace(gate_repair_enabled=False))
    monkeypatch.setenv("BERNSTEIN_GATE_REPAIR", "true")
    assert _gate_repair_enabled(orch_off) is True


# ---------------------------------------------------------------------------
# _maybe_schedule_gate_repair -- schedule-on-fail
# ---------------------------------------------------------------------------


def test_gate_failure_schedules_one_repair_task_with_output_tail() -> None:
    """First quality-gate failure seeds one repair task; its goal embeds the gate output tail."""
    client = _FakeClient()
    orch = _make_orch(client)
    task = _make_task()
    worktree = Path("/tmp/worktrees/agent-abc")

    new_id = _maybe_schedule_gate_repair(orch, task, _failing_qg_result(), worktree)

    assert new_id == "repair-1"
    assert len(client.posts) == 1
    url, body = client.posts[0]
    assert url.endswith("/tasks")
    assert "line 60: ruff violation" in body["description"]
    assert "line 10: ruff violation" not in body["description"]
    assert "do not rewrite the feature" in body["description"].lower()
    assert body["role"] == "backend"
    assert body["metadata"]["gate_repair_attempted"] is True
    assert body["metadata"]["gate_repair_of"] == "task123"
    assert orch._preserved_worktrees["repair-1"] == worktree


# ---------------------------------------------------------------------------
# _maybe_schedule_gate_repair -- no-loop (single-attempt semantics)
# ---------------------------------------------------------------------------


def test_already_attempted_task_does_not_schedule_a_second_repair() -> None:
    """A task whose own metadata already carries a repair attempt gets no second one."""
    client = _FakeClient()
    orch = _make_orch(client)
    task = _make_task()
    task.metadata["gate_repair_attempted"] = True

    new_id = _maybe_schedule_gate_repair(orch, task, _failing_qg_result(), Path("/tmp/wt"))

    assert new_id is None
    assert client.posts == []
    assert orch._preserved_worktrees == {}


# ---------------------------------------------------------------------------
# _maybe_schedule_gate_repair -- pass-through-on-success
# ---------------------------------------------------------------------------


def test_passing_gate_schedules_nothing() -> None:
    client = _FakeClient()
    orch = _make_orch(client)
    task = _make_task()

    new_id = _maybe_schedule_gate_repair(orch, task, _passing_qg_result(), Path("/tmp/wt"))

    assert new_id is None
    assert client.posts == []


# ---------------------------------------------------------------------------
# _maybe_schedule_gate_repair -- switch-off
# ---------------------------------------------------------------------------


def test_switch_off_via_config_skips_scheduling() -> None:
    client = _FakeClient()
    orch = _make_orch(client, gate_repair_enabled=False)
    task = _make_task()

    new_id = _maybe_schedule_gate_repair(orch, task, _failing_qg_result(), Path("/tmp/wt"))

    assert new_id is None
    assert client.posts == []


def test_switch_off_via_env_overrides_config_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_GATE_REPAIR", "false")
    client = _FakeClient()
    orch = _make_orch(client, gate_repair_enabled=True)
    task = _make_task()

    new_id = _maybe_schedule_gate_repair(orch, task, _failing_qg_result(), Path("/tmp/wt"))

    assert new_id is None
    assert client.posts == []


def test_no_worktree_skips_scheduling() -> None:
    """No worktree to resume -> nothing to repair on the same branch, so no-op."""
    client = _FakeClient()
    orch = _make_orch(client)
    task = _make_task()

    new_id = _maybe_schedule_gate_repair(orch, task, _failing_qg_result(), None)

    assert new_id is None
    assert client.posts == []


# ---------------------------------------------------------------------------
# _reap_and_cleanup_session -- preserves the worktree when a repair is scheduled
# ---------------------------------------------------------------------------


class _FakeSpawner:
    def __init__(self) -> None:
        self.cleanup_called = False

    def reap_completed_agent(
        self,
        session: AgentSession,
        *,
        skip_merge: bool = False,
        defer_cleanup: bool = False,
    ) -> None:
        return None

    def get_worktree_path(self, _session_id: str) -> Path | None:
        return None

    def cleanup_worktree(self, _session_id: str) -> None:
        self.cleanup_called = True


def _make_reap_orch(spawner: _FakeSpawner, tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        _spawner=spawner,
        _workdir=tmp_path,
        _config=SimpleNamespace(ab_test=False),
        _post_bulletin=lambda *_args, **_kwargs: None,
    )


def _make_session() -> AgentSession:
    return AgentSession(id="writer-8f6311cf", role="backend", pid=4242, task_ids=["task123"], status="working")


def test_reap_preserves_worktree_when_gate_repair_scheduled(tmp_path: Path) -> None:
    spawner = _FakeSpawner()
    orch = _make_reap_orch(spawner, tmp_path)

    _reap_and_cleanup_session(
        orch,
        _make_task(),
        _make_session(),
        None,
        False,  # janitor_passed - the quality gate blocked this completion
        True,  # skip_merge - approval gate already skipped merge on the failure
        None,
        0,
        preserve_worktree=True,
    )

    assert spawner.cleanup_called is False


def test_reap_cleans_worktree_when_no_repair_scheduled(tmp_path: Path) -> None:
    """Default behaviour (no repair scheduled) is unchanged: cleanup still runs."""
    spawner = _FakeSpawner()
    orch = _make_reap_orch(spawner, tmp_path)

    _reap_and_cleanup_session(
        orch,
        _make_task(),
        _make_session(),
        None,
        False,
        True,
        None,
        0,
    )

    assert spawner.cleanup_called is True
