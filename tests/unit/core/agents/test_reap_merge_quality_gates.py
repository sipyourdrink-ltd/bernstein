"""Unit and regression tests for reap-and-merge quality gates (#4393).

Verifies that:
1. A task whose agent exits immediately after `task complete` has a `gates/<task>.json`
   verdict written before its branch is merged.
2. A blocking gate failure on that path leaves the agent branch unmerged.
3. A passing gate allows the merge to succeed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bernstein.core.models import AgentSession

from bernstein.core.agents.spawner_merge import _quality_gate_refusal, _run_merge_and_push
from bernstein.core.quality.quality_gates import QualityGateCheckResult, QualityGatesConfig, QualityGatesResult


def _make_session(session_id: str = "agent-123", task_id: str = "T-999") -> AgentSession:
    """Build a minimal AgentSession for testing."""
    session = AgentSession(
        id=session_id,
        role="backend",
        task_ids=[task_id],
    )
    session.task_title = "Fix something"  # type: ignore[attr-defined]
    return session


def test_reap_merge_runs_quality_gates_and_persists_verdict(tmp_path: Path) -> None:
    """Quality gates run and write .sdd/runtime/gates/<task_id>.json before merge lands."""
    workdir = tmp_path / "repo"
    workdir.mkdir()
    session = _make_session(session_id="sess-1", task_id="task-100")
    qg_config = QualityGatesConfig(enabled=True, lint=True)

    def fake_run_quality_gates(task: Any, run_dir: Path, root: Path, config: QualityGatesConfig) -> QualityGatesResult:
        gates_dir = root / ".sdd" / "runtime" / "gates"
        gates_dir.mkdir(parents=True, exist_ok=True)
        report_file = gates_dir / f"{task.id}.json"
        report_file.write_text(
            json.dumps({"task_id": task.id, "passed": True, "gate_results": []}),
            encoding="utf-8",
        )
        return QualityGatesResult(task_id=task.id, passed=True)

    merge_called = []

    def fake_merge_fn(session_id: str, repo_root: Path) -> Any:
        # Assert that the gate verdict file exists at the time the merge function is called!
        report_file = repo_root / ".sdd" / "runtime" / "gates" / "task-100.json"
        assert report_file.exists(), "Gate verdict must be written before merge is executed!"
        merge_called.append(session_id)

        class _Result:
            success = True
            conflicting_files: list[str] = []
            error = None

        return _Result()

    with (
        patch("bernstein.core.quality.quality_gates.run_quality_gates", side_effect=fake_run_quality_gates),
        patch("bernstein.core.git_ops.safe_push") as mock_push,
    ):
        mock_push.return_value.ok = True
        mock_push.return_value.stderr = ""

        result = _run_merge_and_push(
            session,
            workdir,
            fake_merge_fn,
            quality_gate_config=qg_config,
        )

        assert result is not None
        assert result.success is True
        assert merge_called == ["sess-1"]
        # Gate report persisted on disk
        verdict_path = workdir / ".sdd" / "runtime" / "gates" / "task-100.json"
        assert verdict_path.exists()
        data = json.loads(verdict_path.read_text(encoding="utf-8"))
        assert data["task_id"] == "task-100"
        assert data["passed"] is True


def test_blocking_quality_gate_failure_refuses_merge(tmp_path: Path) -> None:
    """A blocking quality gate failure leaves the branch unmerged."""
    workdir = tmp_path / "repo"
    workdir.mkdir()
    session = _make_session(session_id="sess-2", task_id="task-200")
    qg_config = QualityGatesConfig(enabled=True, lint=True)

    def fake_failing_quality_gates(
        task: Any,
        run_dir: Path,
        root: Path,
        config: QualityGatesConfig,
    ) -> QualityGatesResult:
        return QualityGatesResult(
            task_id=task.id,
            passed=False,
            gate_results=[
                QualityGateCheckResult(
                    gate="lint",
                    passed=False,
                    blocked=True,
                    detail="Ruff check failed",
                )
            ],
        )

    merge_called = []

    def fake_merge_fn(session_id: str, repo_root: Path) -> Any:
        merge_called.append(session_id)

    with patch("bernstein.core.quality.quality_gates.run_quality_gates", side_effect=fake_failing_quality_gates):
        result = _run_merge_and_push(
            session,
            workdir,
            fake_merge_fn,
            quality_gate_config=qg_config,
        )

        assert result is not None
        assert result.success is False
        assert "quality gates blocked merge for task task-200: quality_gate:lint" in result.error
        # Merge function was NEVER called because gate blocked it
        assert merge_called == []


def test_disabled_quality_gates_skips_refusal(tmp_path: Path) -> None:
    """When quality gates are disabled, no gate refusal is produced."""
    workdir = tmp_path / "repo"
    workdir.mkdir()
    session = _make_session(session_id="sess-3", task_id="task-300")
    qg_config = QualityGatesConfig(enabled=False)

    refusal = _quality_gate_refusal(
        session,
        workdir,
        "agent/sess-3",
        quality_gate_config=qg_config,
    )
    assert refusal is None


def test_reap_merge_refuses_when_quality_gate_raises_exception(tmp_path: Path) -> None:
    """When run_quality_gates crashes/raises, merge must be refused with quality-gates-errored."""
    workdir = tmp_path / "repo"
    workdir.mkdir()
    session = _make_session(session_id="sess-err", task_id="task-err")
    qg_config = QualityGatesConfig(enabled=True)

    with patch(
        "bernstein.core.quality.quality_gates.run_quality_gates", side_effect=RuntimeError("Subprocess timeout")
    ):
        refusal = _quality_gate_refusal(
            session,
            workdir,
            "agent/sess-err",
            quality_gate_config=qg_config,
        )

    assert refusal is not None
    assert refusal.success is False
    assert "refused: quality gates execution errored for task task-err" in (refusal.error or "")
    assert "Subprocess timeout" in (refusal.error or "")

    # Verify refusal marker in refused_merges.jsonl
    refusals_file = workdir / ".sdd" / "runtime" / "refused_merges.jsonl"
    assert refusals_file.exists()
    refusals_content = refusals_file.read_text(encoding="utf-8")
    assert "quality-gates-errored" in refusals_content
    # The gate's own explanation rides along, so the run summary can say why.
    entry = json.loads(refusals_content.splitlines()[-1])
    assert entry["reason"] == "quality-gates-errored"
    assert "Subprocess timeout" in entry["detail"]
