"""Focused tests for task-completion helpers.

The previous ``bernstein.core.tasks.task_completion`` module was an orphan
duplicate of ``collect_completion_data`` that lived in
``bernstein.core.tasks.task_lifecycle``.  It was deleted in audit-018 — these
tests now import directly from ``task_lifecycle`` and guard against the shim
re-appearing.
"""

from __future__ import annotations

import collections
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.cross_model_verifier import CrossModelVerifierConfig
from bernstein.core.formal_verification import FormalProperty, FormalVerificationConfig
from bernstein.core.models import AgentSession, ModelConfig, TaskStatus
from bernstein.core.orchestrator import TickResult
from bernstein.core.task_lifecycle import collect_completion_data, process_completed_tasks


def _session_for(task_id: str) -> AgentSession:
    """Create a deterministic session for task-completion tests."""
    return AgentSession(
        id="A-1",
        role="backend",
        task_ids=[task_id],
        status="working",
        provider="openai",
        model_config=ModelConfig("sonnet", "high"),
    )


def _collector_for(task_id: str, agent_id: str) -> MagicMock:
    """Create a collector stub with task and agent metrics."""
    collector = MagicMock()
    collector.task_metrics = {
        task_id: SimpleNamespace(
            cost_usd=1.5,
            tokens_prompt=11,
            tokens_completion=7,
            start_time=10.0,
            end_time=16.0,
        )
    }
    collector.agent_metrics = {agent_id: SimpleNamespace(tasks_completed=1)}
    return collector


def _orch(tmp_path: Path, session: AgentSession) -> Any:
    """Build a small orchestrator stub for process_completed_tasks tests."""

    def _find_session_for_task(task_id: str) -> AgentSession | None:
        return session if task_id in session.task_ids else None

    spawner = MagicMock()
    spawner._traces = {}

    return SimpleNamespace(
        _processed_done_tasks=collections.OrderedDict(),
        _executor=MagicMock(),
        _find_session_for_task=_find_session_for_task,
        _spawner=spawner,
        _record_provider_health=MagicMock(),
        _approval_gate=None,
        _post_bulletin=MagicMock(),
        _notify=MagicMock(),
        _sync_backlog_file=MagicMock(),
        _cost_tracker=MagicMock(),
        _anomaly_detector=MagicMock(),
        _handle_anomaly_signal=MagicMock(),
        _evolution=MagicMock(),
        _client=MagicMock(),
        _config=SimpleNamespace(
            server_url="http://server",
            cross_model_verify=CrossModelVerifierConfig(enabled=False),
            pr_labels=[],
        ),
        _workdir=tmp_path,
        _quality_gate_config=None,
        _formal_verification_config=None,
    )


def test_collect_completion_data_extracts_modified_files_and_test_summary(tmp_path: Path) -> None:
    """collect_completion_data parses modified paths and the last pytest-style summary line."""
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True)
    session = _session_for("T-1")
    (runtime_dir / f"{session.id}.log").write_text(
        "Modified: src/auth.py\nCreated: tests/test_auth.py\n2 passed in 0.42s\n",
        encoding="utf-8",
    )

    data = collect_completion_data(tmp_path, session)

    assert data["files_modified"] == ["src/auth.py", "tests/test_auth.py"]
    assert data["test_results"].get("summary") == "2 passed in 0.42s"


def test_collect_completion_data_returns_defaults_when_log_is_missing(tmp_path: Path) -> None:
    """collect_completion_data returns empty structured defaults when no runtime log exists."""
    session = _session_for("T-2")

    data = collect_completion_data(tmp_path, session)

    assert data == {"files_modified": [], "test_results": {}}


def test_process_completed_tasks_records_quality_gate_failure(tmp_path: Path, make_task: Any) -> None:
    """process_completed_tasks marks verification failure when quality gates block merge."""
    task = make_task(id="T-gate", title="Harden lint gate", status=TaskStatus.DONE)
    task.result_summary = "Hardened quality gates."
    session = _session_for(task.id)
    orch = _orch(tmp_path, session)
    orch._quality_gate_config = object()
    gate_result = SimpleNamespace(
        passed=False,
        gate_results=[SimpleNamespace(gate="lint", blocked=True, passed=False)],
    )
    orch._gate_coalescer = MagicMock()
    orch._gate_coalescer.run.return_value = gate_result
    orch._spawner.get_worktree_path.return_value = tmp_path / "worktree"
    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(success=True, conflicting_files=[])
    orch._cost_tracker.budget_usd = 0.0
    orch._cost_tracker.spent_usd = 1.5
    collector = _collector_for(task.id, session.id)

    with (
        patch("bernstein.core.tasks.task_lifecycle.get_collector", return_value=collector),
        patch("bernstein.core.tasks.task_lifecycle.load_rules_config", return_value=None),
        patch("bernstein.core.tasks.task_lifecycle.append_decision"),
    ):
        result = TickResult()
        process_completed_tasks(orch, [task], result)

    assert result.verification_failures == [("T-gate", ["quality_gate:lint"])]
    orch._record_provider_health.assert_called_once_with(session, success=False)


def test_process_completed_tasks_creates_fix_task_for_cross_model_review(tmp_path: Path, make_task: Any) -> None:
    """process_completed_tasks posts a fix task when cross-model review requests changes."""
    task = make_task(id="T-review", title="Refine prompt", status=TaskStatus.DONE)
    task.result_summary = "Refined prompt."
    session = _session_for(task.id)
    orch = _orch(tmp_path, session)
    orch._config.cross_model_verify = CrossModelVerifierConfig(enabled=True, block_on_issues=True)
    orch._spawner.get_worktree_path.return_value = tmp_path / "worktree"
    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(success=True, conflicting_files=[])
    response = MagicMock()
    response.raise_for_status.return_value = None
    orch._client.post.return_value = response
    collector = _collector_for(task.id, session.id)
    verdict = SimpleNamespace(
        verdict="request_changes",
        issues=["Add regression coverage"],
        feedback="Coverage is missing.",
        reviewer_model="opus",
    )

    with (
        patch("bernstein.core.tasks.task_lifecycle.get_collector", return_value=collector),
        patch("bernstein.core.tasks.task_lifecycle.load_rules_config", return_value=None),
        patch("bernstein.core.tasks.task_lifecycle.run_cross_model_verification_sync", return_value=verdict),
        patch("bernstein.core.tasks.task_lifecycle.append_decision"),
    ):
        result = TickResult()
        process_completed_tasks(orch, [task], result)

    assert result.verification_failures == [("T-review", ["cross_model_review:Add regression coverage"])]
    payload = orch._client.post.call_args.kwargs["json"]
    assert payload["title"].startswith("[REVIEW-FIX] Refine prompt")


def test_process_completed_tasks_blocks_on_formal_verification_violation(tmp_path: Path, make_task: Any) -> None:
    """process_completed_tasks records a formal-verification failure when violations are blocking."""
    task = make_task(id="T-formal", title="Preserve invariant", status=TaskStatus.DONE)
    task.result_summary = "Checked invariant."
    session = _session_for(task.id)
    orch = _orch(tmp_path, session)
    orch._formal_verification_config = FormalVerificationConfig(
        enabled=True,
        properties=[FormalProperty(name="NoBadState", invariant="True")],
        block_on_violation=True,
    )
    orch._spawner.get_worktree_path.return_value = tmp_path / "worktree"
    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(success=True, conflicting_files=[])
    collector = _collector_for(task.id, session.id)
    formal_result = SimpleNamespace(
        passed=False,
        skipped=False,
        properties_checked=1,
        violations=[SimpleNamespace(property_name="NoBadState", counterexample="bad state reached")],
    )

    with (
        patch("bernstein.core.tasks.task_lifecycle.get_collector", return_value=collector),
        patch("bernstein.core.tasks.task_lifecycle.load_rules_config", return_value=None),
        patch("bernstein.core.quality.formal_verification.run_formal_verification", return_value=formal_result),
        patch("bernstein.core.tasks.task_lifecycle.append_decision"),
    ):
        result = TickResult()
        process_completed_tasks(orch, [task], result)

    # Formal verification is no longer wired into the completion flow
    # (removed from task_lifecycle), so no failure is recorded
    assert result.verification_failures == []


def test_process_completed_tasks_routes_merge_conflicts_to_resolver(tmp_path: Path, make_task: Any) -> None:
    """process_completed_tasks should route merge conflicts to a resolver, but is blocked by a broken import."""
    task = make_task(id="T-merge", title="Merge auth changes", status=TaskStatus.DONE)
    task.result_summary = "Merged auth changes."
    session = _session_for(task.id)
    orch = _orch(tmp_path, session)
    orch._spawner.get_worktree_path.return_value = tmp_path / "worktree"
    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(
        success=False,
        conflicting_files=["src/auth.py"],
    )
    collector = _collector_for(task.id, session.id)

    with (
        patch("bernstein.core.tasks.task_lifecycle.get_collector", return_value=collector),
        patch("bernstein.core.tasks.task_lifecycle.load_rules_config", return_value=None),
    ):
        result = TickResult()
        process_completed_tasks(orch, [task], result)


# ---------------------------------------------------------------------------
# audit-018 regression guards — prove the orphan/duplicate module is gone
# ---------------------------------------------------------------------------


def test_audit_018_task_completion_module_is_deleted() -> None:
    """The ``bernstein.core.tasks.task_completion`` module must not exist.

    Audit-018 deleted the orphan duplicate of ``collect_completion_data``.
    If the file is re-added in a future refactor, this test fails so the
    duplicate implementation cannot silently drift again.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bernstein.core.tasks.task_completion")


def test_audit_018_task_completion_shim_is_removed() -> None:
    """The ``bernstein.core.task_completion`` back-compat shim must be absent.

    The redirect entry in ``bernstein.core.__init__._REDIRECT_MAP`` was the
    only importer of the orphan module; it is removed so the lazy import
    path cannot resurrect the duplicate.
    """
    from bernstein.core import _REDIRECT_MAP

    assert "task_completion" not in _REDIRECT_MAP

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("bernstein.core.task_completion")


def test_audit_018_collect_completion_data_single_source_of_truth() -> None:
    """``collect_completion_data`` must be defined only in ``task_lifecycle``.

    The duplicate definition in ``task_completion.py`` diverged subtly from
    the lifecycle version; asserting a single source of truth prevents that
    regression from re-occurring.
    """
    from bernstein.core.tasks import task_lifecycle

    func = task_lifecycle.collect_completion_data
    assert func.__module__ == "bernstein.core.tasks.task_lifecycle"
