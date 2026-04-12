"""Integration-style tests for orchestration-intelligence v2."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.base import SpawnError
from bernstein.core.completion_budget import CompletionBudget
from bernstein.core.effectiveness import EffectivenessScore, EffectivenessScorer
from bernstein.core.heartbeat import compute_stall_profile
from bernstein.core.janitor import create_fix_tasks
from bernstein.core.models import Scope
from bernstein.core.spawn_prompt import render_prompt
from bernstein.core.task_lifecycle import claim_and_spawn_batches, maybe_retry_task


def _write_log(tmp_path: Path, session_id: str, content: str) -> None:
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / f"{session_id}.log").write_text(content, encoding="utf-8")


def test_agent_failure_generates_context_aware_retry(tmp_path: Path, make_task: Any) -> None:
    task = make_task(id="T-1", title="Compile parser", description="Fix the parser.")
    _write_log(tmp_path, "A-1", "SyntaxError: invalid syntax\nModified: src/parser.py\n")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"id": "T-2"}
    client = MagicMock()
    client.post.return_value = response

    created = maybe_retry_task(
        task,
        retried_task_ids=set(),
        max_task_retries=2,
        client=client,
        server_url="http://server",
        quarantine=MagicMock(),
        workdir=tmp_path,
        session_id="A-1",
    )

    assert created is True
    assert "Previous attempt failed" in client.post.call_args.kwargs["json"]["description"]


def test_stall_detection_adapts_to_task_complexity(make_task: Any) -> None:
    small = compute_stall_profile(make_task(), None, None)
    large = compute_stall_profile(make_task(scope=Scope.LARGE), None, None)

    assert large.kill_threshold > small.kill_threshold


@pytest.mark.asyncio
async def test_completion_budget_prevents_infinite_fix_spiral(tmp_path: Path, make_task: Any) -> None:
    task = make_task(title="Lineage task")
    budget = CompletionBudget(tmp_path)
    for _ in range(2):
        budget.record_attempt(task, is_fix=True)

    ids = await create_fix_tasks(task, ["path_exists: missing.py"], "http://localhost:8052", workdir=tmp_path)

    assert ids == []


def test_recommendations_reach_agent_prompt(tmp_path: Path, make_task: Any) -> None:
    recs = tmp_path / ".sdd" / "recommendations.yaml"
    recs.parent.mkdir(parents=True, exist_ok=True)
    recs.write_text(
        "recommendations:\n"
        "  - id: use-uv\n"
        "    category: tool_usage\n"
        "    severity: critical\n"
        "    text: Always use `uv run`\n",
        encoding="utf-8",
    )
    task = make_task(id="T-1", title="Implement feature", description="Do work.")
    prompt = render_prompt([task], tmp_path / "templates", tmp_path, session_id="A-1")

    assert "Always use `uv run`" in prompt


def test_effectiveness_score_recorded_on_completion(tmp_path: Path) -> None:
    scorer = EffectivenessScorer(tmp_path)
    scorer.record(
        EffectivenessScore(
            session_id="A-1",
            task_id="T-1",
            role="backend",
            model="opus",
            effort="max",
            time_score=90,
            quality_score=90,
            efficiency_score=80,
            retry_score=100,
            completion_score=100,
            total=90,
            grade="A",
            wall_time_s=120.0,
            estimated_time_s=300.0,
            tokens_used=500,
            retry_count=0,
            fix_count=0,
            gate_pass_rate=1.0,
        )
    )

    assert (tmp_path / ".sdd" / "metrics" / "effectiveness.jsonl").exists()


def test_spawn_failure_analysis_prevents_permanent_retry(tmp_path: Path, make_task: Any) -> None:
    task = make_task(id="T-1", title="Spawn task")
    batch = [task]
    post_response = MagicMock()
    post_response.status_code = 200
    post_response.raise_for_status.return_value = None
    client = MagicMock()
    client.post.return_value = post_response
    orch = SimpleNamespace(
        _config=SimpleNamespace(
            server_url="http://server",
            max_tasks_per_agent=1,
            max_agents=2,
            force_parallel=False,
            ab_test=False,
            max_agent_runtime_s=600,
        ),
        _workdir=tmp_path,
        _agents={},
        _file_ownership={},
        _spawn_failures={},
        _spawn_failure_history={},
        _MAX_SPAWN_FAILURES=3,
        _SPAWN_BACKOFF_BASE_S=30.0,
        _SPAWN_BACKOFF_MAX_S=300.0,
        _idle_shutdown_ts={},
        _quarantine=MagicMock(),
        _decomposed_task_ids=set(),
        _preserved_worktrees={},
        _client=client,
        _spawner=MagicMock(),
        _task_to_session={},
        _lock_manager=MagicMock(),
        _rate_limit_tracker=None,
        _wal_writer=None,
        _response_cache=None,
        _fast_path_stats=MagicMock(),
        _bulletin=None,
    )
    orch._quarantine.is_quarantined.return_value = False
    orch._spawner.spawn_for_tasks.side_effect = SpawnError("adapter not found")

    with patch("bernstein.core.tasks.task_claim.fail_task") as fail_task_mock:
        claim_and_spawn_batches(orch, [batch], 0, set(), set(), SimpleNamespace(spawned=[], errors=[]))

    assert fail_task_mock.call_count == 1
