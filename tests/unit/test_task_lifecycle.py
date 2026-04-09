"""Focused tests for task lifecycle claim, completion, and ticket movement."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from bernstein.core.convergence_guard import ConvergenceGuard
from bernstein.core.graph import TaskGraph
from bernstein.core.models import AgentSession, Complexity, ConvergenceGuardConfig, ModelConfig, Scope, TaskStatus
from bernstein.core.orchestrator import TickResult
from bernstein.core.task_lifecycle import (
    _enqueue_paired_test_task,
    _move_backlog_ticket,
    claim_and_spawn_batches,
    prepare_speculative_warm_pool,
    process_completed_tasks,
    should_auto_decompose,
)
from bernstein.core.warm_pool import WarmPool, WarmPoolConfig


def _never_quarantined(title: str) -> bool:
    """Return False for all task titles in claim-path tests."""
    del title
    return False


def _no_quarantine_entry(title: str) -> None:
    """Return no quarantine metadata for claim-path tests."""
    del title
    return None


def _claim_orch(tmp_path: Path) -> Any:
    """Build a small orchestrator stub for claim_and_spawn_batches tests."""
    client = MagicMock()
    client.post.return_value = SimpleNamespace(status_code=200)
    spawner = MagicMock()
    spawner._adapter = None
    return SimpleNamespace(
        _config=SimpleNamespace(
            server_url="http://server",
            max_agents=2,
            force_parallel=False,
            max_agent_runtime_s=900,
            ab_test=False,
        ),
        _client=client,
        _spawner=spawner,
        _agents={},
        _file_ownership={},
        _spawn_failures={},
        _quarantine=SimpleNamespace(
            is_quarantined=_never_quarantined,
            get_entry=_no_quarantine_entry,
        ),
        _decomposed_task_ids=set(),
        _idle_shutdown_ts=set(),
        _workdir=tmp_path,
        _response_cache=None,
        _batch_api=None,
        _batch_sessions={},
        _fast_path_stats={},
        _preserved_worktrees={},
        _task_to_session={},
        _SPAWN_BACKOFF_BASE_S=5,
        _SPAWN_BACKOFF_MAX_S=60,
        _MAX_SPAWN_FAILURES=3,
        _lock_manager=None,
        is_shutting_down=lambda: False,
    )


def _collector_for(task_id: str, agent_id: str) -> MagicMock:
    """Build a metrics collector stub with deterministic task metrics."""
    collector = MagicMock()
    collector.task_metrics = {
        task_id: SimpleNamespace(
            cost_usd=2.5,
            tokens_prompt=12,
            tokens_completion=8,
            start_time=10.0,
            end_time=15.0,
        )
    }
    collector.agent_metrics = {agent_id: SimpleNamespace(tasks_completed=1)}
    return collector


def _process_orch(tmp_path: Path, session: AgentSession) -> Any:
    """Build a small orchestrator stub for process_completed_tasks tests."""

    def _find_session_for_task(task_id: str) -> AgentSession | None:
        return session if task_id in session.task_ids else None

    return SimpleNamespace(
        _processed_done_tasks=set(),
        _executor=MagicMock(),
        _find_session_for_task=_find_session_for_task,
        _spawner=MagicMock(),
        _record_provider_health=MagicMock(),
        _approval_gate=None,
        _post_bulletin=MagicMock(),
        _notify=MagicMock(),
        _sync_backlog_file=MagicMock(),
        _cost_tracker=MagicMock(),
        _evolution=None,
        _response_cache=MagicMock(),
        _client=MagicMock(),
        _config=SimpleNamespace(
            server_url="http://server",
            cross_model_verify=None,
            pr_labels=[],
            budget_usd=0.0,
        ),
        _workdir=tmp_path,
        _quality_gate_config=None,
        _wal_writer=None,
        _bandit_router=None,
    )


def _session_for(task_id: str, *, exit_code: int | None = 0) -> AgentSession:
    """Create a deterministic agent session for lifecycle tests."""
    return AgentSession(
        id="A-1",
        role="backend",
        task_ids=[task_id],
        status="working",
        exit_code=exit_code,
        provider="codex",
        model_config=ModelConfig("sonnet", "high"),
    )


def test_should_auto_decompose_after_second_retry_even_when_scope_is_medium(make_task: Any) -> None:
    """should_auto_decompose forces decomposition for tasks that failed twice already."""
    task = make_task(
        id="T-retry",
        title="[RETRY 2] Stabilize planner",
        scope=Scope.MEDIUM,
    )

    assert should_auto_decompose(task, set()) is False
    assert should_auto_decompose(task, set(), force_parallel=True) is True


def test_claim_and_spawn_batches_respects_max_agent_cap(tmp_path: Path, make_task: Any) -> None:
    """claim_and_spawn_batches does nothing when the orchestrator is already at capacity."""
    orch = _claim_orch(tmp_path)
    task = make_task(id="T-cap", role="backend")
    result = TickResult()

    claim_and_spawn_batches(
        orch, [[task]], alive_count=orch._config.max_agents, assigned_task_ids=set(), done_ids=set(), result=result
    )

    orch._client.post.assert_not_called()
    orch._spawner.spawn_for_tasks.assert_not_called()
    assert result.spawned == []


def test_claim_and_spawn_batches_skips_locked_files_owned_by_live_agent(tmp_path: Path, make_task: Any) -> None:
    """claim_and_spawn_batches skips a batch when one of its files is owned by a live agent."""
    orch = _claim_orch(tmp_path)
    task = make_task(id="T-lock", owned_files=["src/auth.py"])
    orch._file_ownership["src/auth.py"] = "A-owner"
    orch._agents["A-owner"] = AgentSession(
        id="A-owner",
        role="backend",
        task_ids=["T-other"],
        status="working",
        model_config=ModelConfig("sonnet", "high"),
    )
    result = TickResult()

    claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    orch._client.post.assert_not_called()
    orch._spawner.spawn_for_tasks.assert_not_called()
    assert result.errors == []


def test_claim_and_spawn_batches_aborts_on_claim_transport_error(tmp_path: Path, make_task: Any) -> None:
    """claim_and_spawn_batches records a claim error and never spawns when the server is unreachable."""
    orch = _claim_orch(tmp_path)
    task = make_task(id="T-net")
    orch._client.post.side_effect = httpx.TransportError("server down")
    result = TickResult()

    claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    orch._spawner.spawn_for_tasks.assert_not_called()
    assert result.errors == ["claim:T-net: server down"]


def test_claim_and_spawn_batches_auto_decomposes_large_task_before_claim(tmp_path: Path, make_task: Any) -> None:
    """claim_and_spawn_batches creates a planner task instead of claiming a decomposable large task."""
    orch = _claim_orch(tmp_path)
    orch._config.auto_decompose = True
    task = make_task(id="T-large", scope=Scope.LARGE)
    result = TickResult()

    with (
        patch("bernstein.core.task_lifecycle.should_auto_decompose", return_value=True),
        patch("bernstein.core.task_lifecycle.auto_decompose_task") as mock_decompose,
    ):
        claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    mock_decompose.assert_called_once()
    orch._client.post.assert_not_called()
    orch._spawner.spawn_for_tasks.assert_not_called()


def test_claim_and_spawn_batches_submits_provider_batch_without_spawning(tmp_path: Path, make_task: Any) -> None:
    """Eligible provider-batch work is submitted and skips the local spawn path."""
    orch = _claim_orch(tmp_path)
    task = make_task(id="T-batch", title="Update docs", description="Refresh the API docs.")
    task.batch_eligible = True
    orch._batch_api = MagicMock()
    orch._batch_api.try_submit.return_value = SimpleNamespace(
        handled=True,
        submitted=True,
        session_id="batch-T-batch",
    )
    result = TickResult()

    claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    orch._batch_api.try_submit.assert_called_once()
    orch._spawner.spawn_for_tasks.assert_not_called()
    assert result.spawned == ["batch-T-batch"]


def test_claim_and_spawn_batches_sets_small_timeout_bucket(tmp_path: Path, make_task: Any) -> None:
    """Small-scope work gets the fixed 15-minute timeout bucket."""
    orch = _claim_orch(tmp_path)
    task = make_task(id="T-small", scope=Scope.SMALL)
    task.estimated_minutes = 5
    session = AgentSession(id="A-small", role="backend", task_ids=[task.id], model_config=ModelConfig("sonnet", "high"))
    orch._spawner.spawn_for_tasks.return_value = session
    result = TickResult()

    claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    assert session.timeout_s == 15 * 60


def test_claim_and_spawn_batches_sets_medium_timeout_bucket(tmp_path: Path, make_task: Any) -> None:
    """Medium-scope work gets the fixed 30-minute timeout bucket."""
    orch = _claim_orch(tmp_path)
    task = make_task(id="T-medium", scope=Scope.MEDIUM)
    task.estimated_minutes = 10
    session = AgentSession(
        id="A-medium",
        role="backend",
        task_ids=[task.id],
        model_config=ModelConfig("sonnet", "high"),
    )
    orch._spawner.spawn_for_tasks.return_value = session
    result = TickResult()

    claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    assert session.timeout_s == 30 * 60


def test_claim_and_spawn_batches_sets_large_timeout_bucket(tmp_path: Path, make_task: Any) -> None:
    """Large-scope work gets the fixed 60-minute timeout bucket."""
    orch = _claim_orch(tmp_path)
    task = make_task(id="T-large-timeout", scope=Scope.LARGE)
    task.estimated_minutes = 20
    session = AgentSession(id="A-large", role="backend", task_ids=[task.id], model_config=ModelConfig("sonnet", "high"))
    orch._spawner.spawn_for_tasks.return_value = session
    result = TickResult()

    with patch("bernstein.core.task_lifecycle.should_auto_decompose", return_value=False):
        claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    assert session.timeout_s == 60 * 60


def test_claim_and_spawn_batches_sets_xl_timeout_bucket_for_high_risk_batch(tmp_path: Path, make_task: Any) -> None:
    """Large/high or architect/security/manager work gets the fixed 120-minute timeout bucket."""
    orch = _claim_orch(tmp_path)
    task = make_task(
        id="T-xl",
        role="backend",
        scope=Scope.LARGE,
    )
    task.complexity = Complexity.HIGH
    task.estimated_minutes = 45
    session = AgentSession(id="A-xl", role="backend", task_ids=[task.id], model_config=ModelConfig("sonnet", "high"))
    orch._spawner.spawn_for_tasks.return_value = session
    result = TickResult()

    with patch("bernstein.core.task_lifecycle.should_auto_decompose", return_value=False):
        claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    assert session.timeout_s == 120 * 60


def test_claim_and_spawn_batches_blocked_by_high_error_rate(tmp_path: Path, make_task: Any) -> None:
    """claim_and_spawn_batches skips the entire spawn wave when convergence guard detects high error rate."""
    orch = _claim_orch(tmp_path)
    # Wire a convergence guard with a low error-rate threshold
    cg = ConvergenceGuard(ConvergenceGuardConfig(max_error_rate=0.3))
    # Record failures to push error rate above the threshold
    import time

    now = time.time()
    for _ in range(8):
        cg.record_failure(now=now)
    for _ in range(2):
        cg.record_success(now=now)
    # error rate is 0.8 > 0.3 threshold
    orch._convergence_guard = cg

    task = make_task(id="T-blocked", role="backend")
    result = TickResult()

    claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    # Spawn must not happen — convergence guard blocked it
    orch._client.post.assert_not_called()
    orch._spawner.spawn_for_tasks.assert_not_called()
    assert result.spawned == []


def test_claim_and_spawn_batches_allowed_when_converged(tmp_path: Path, make_task: Any) -> None:
    """claim_and_spawn_batches proceeds normally when convergence guard passes."""
    orch = _claim_orch(tmp_path)
    # Wire a convergence guard with all-success history
    cg = ConvergenceGuard()
    import time

    now = time.time()
    for _ in range(5):
        cg.record_success(now=now)
    orch._convergence_guard = cg
    orch._merge_queue = []

    task = make_task(id="T-ok", role="backend")
    session = AgentSession(id="A-ok", role="backend", task_ids=[task.id], model_config=ModelConfig("sonnet", "high"))
    orch._spawner.spawn_for_tasks.return_value = session
    result = TickResult()

    claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    # Task was claimed and spawned
    orch._client.post.assert_called()
    assert result.spawned == [session.id]


def test_claim_and_spawn_batches_applies_bandit_route_before_spawn(tmp_path: Path, make_task: Any) -> None:
    """Bandit mode writes the selected model/effort onto the task before spawning."""
    orch = _claim_orch(tmp_path)
    orch._bandit_routing_mode = "bandit"
    orch._bandit_router = MagicMock()
    orch._bandit_router.select.return_value = SimpleNamespace(
        model="sonnet",
        effort="high",
        reason="bandit: LinUCB selected 'sonnet'",
    )
    task = make_task(id="T-bandit", role="backend", complexity=Complexity.HIGH)
    session = AgentSession(
        id="A-bandit", role="backend", task_ids=[task.id], model_config=ModelConfig("sonnet", "high")
    )
    orch._spawner.spawn_for_tasks.return_value = session
    result = TickResult()

    claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    orch._bandit_router.select.assert_called_once_with(task)
    orch._spawner.spawn_for_tasks.assert_called_once()
    assert task.model == "sonnet"
    assert task.effort == "high"
    assert result.spawned == [session.id]


def test_claim_and_spawn_batches_records_bandit_shadow_without_overriding(tmp_path: Path, make_task: Any) -> None:
    """Shadow mode records what the bandit would pick while preserving live static routing."""
    orch = _claim_orch(tmp_path)
    orch._bandit_routing_mode = "bandit-shadow"
    orch._bandit_router = MagicMock()
    decision = SimpleNamespace(
        model="haiku",
        effort="low",
        reason="bandit: LinUCB selected 'haiku'",
    )
    orch._bandit_router.select.return_value = decision
    task = make_task(id="T-shadow", role="backend", complexity=Complexity.HIGH)
    session = AgentSession(
        id="A-shadow", role="backend", task_ids=[task.id], model_config=ModelConfig("sonnet", "high")
    )
    orch._spawner.spawn_for_tasks.return_value = session
    result = TickResult()

    claim_and_spawn_batches(orch, [[task]], alive_count=0, assigned_task_ids=set(), done_ids=set(), result=result)

    orch._bandit_router.select.assert_called_once_with(task)
    orch._bandit_router.record_shadow_decision.assert_called_once_with(
        task=task,
        decision=decision,
        executed_model="sonnet",
        executed_effort="high",
    )
    assert task.model is None
    assert task.effort is None
    assert result.spawned == [session.id]


def test_prepare_speculative_warm_pool_prewarms_near_ready_tasks_without_spawning(tmp_path: Path, make_task: Any) -> None:
    """Near-ready blocked tasks should prepare worktree capacity without claiming or spawning."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()

    warm_pool = WarmPool(repo_root, config=WarmPoolConfig(pool_size=1, use_git_worktrees=False))
    orch = _claim_orch(repo_root)
    orch._spawner._warm_pool = warm_pool
    orch._spawner.spawn_for_tasks = MagicMock()

    blocker = make_task(id="T-blocker", role="backend", status=TaskStatus.OPEN)
    dependent = make_task(id="T-dependent", role="backend", status=TaskStatus.OPEN)
    dependent.depends_on = [blocker.id]
    graph = TaskGraph([blocker, dependent])

    prepare_speculative_warm_pool(orch, graph, [blocker, dependent])

    assert warm_pool.available == 1
    orch._client.post.assert_not_called()
    orch._spawner.spawn_for_tasks.assert_not_called()


def test_process_completed_tasks_moves_ticket_and_caches_verified_result(tmp_path: Path, make_task: Any) -> None:
    """process_completed_tasks closes the backlog ticket and writes a verified cache entry after a clean reap."""
    worktree = tmp_path / "agent-worktree"
    worktree.mkdir()
    open_dir = tmp_path / ".sdd" / "backlog" / "open"
    open_dir.mkdir(parents=True)
    source_ticket = open_dir / "bug-101.md"
    source_ticket.write_text("# BUG-101\n", encoding="utf-8")

    task = make_task(
        id="T-done",
        title="Close parser regression",
        description="Done.\n<!-- source: bug-101.md -->",
        status=TaskStatus.DONE,
    )
    task.result_summary = "Parser regression closed."
    session = _session_for(task.id, exit_code=0)
    orch = _process_orch(tmp_path, session)
    orch._spawner.get_worktree_path.return_value = worktree
    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(success=True, conflicting_files=[])
    collector = _collector_for(task.id, session.id)

    with (
        patch("bernstein.core.task_lifecycle.get_collector", return_value=collector),
        patch("bernstein.core.task_lifecycle._get_git_diff_line_count_in_worktree", return_value=12),
        patch("bernstein.core.task_lifecycle.append_decision"),
    ):
        result = TickResult()
        process_completed_tasks(orch, [task], result)

    assert (tmp_path / ".sdd" / "backlog" / "closed" / "bug-101.md").exists()
    assert not source_ticket.exists()
    orch._sync_backlog_file.assert_called_once_with(task)
    orch._response_cache.store.assert_called_once_with(
        orch._response_cache.task_key.return_value,
        "Parser regression closed.",
        verified=True,
        git_diff_lines=12,
        source_task_id="T-done",
    )
    assert result.verified == ["T-done"]


def test_process_completed_tasks_records_quality_gate_failure_without_closing_ticket(
    tmp_path: Path,
    make_task: Any,
) -> None:
    """process_completed_tasks leaves the ticket open and skips cache writes when quality gates block merge."""
    worktree = tmp_path / "agent-worktree"
    worktree.mkdir()
    open_dir = tmp_path / ".sdd" / "backlog" / "open"
    open_dir.mkdir(parents=True)
    source_ticket = open_dir / "bug-102.md"
    source_ticket.write_text("# BUG-102\n", encoding="utf-8")

    task = make_task(
        id="T-gate",
        title="Harden linter path",
        description="Done.\n<!-- source: bug-102.md -->",
        status=TaskStatus.DONE,
    )
    task.result_summary = "Applied linter hardening."
    session = _session_for(task.id, exit_code=1)
    orch = _process_orch(tmp_path, session)
    orch._quality_gate_config = object()
    orch._spawner.get_worktree_path.return_value = worktree
    orch._spawner.reap_completed_agent.return_value = SimpleNamespace(success=True, conflicting_files=[])
    collector = _collector_for(task.id, session.id)
    gate_result = SimpleNamespace(
        passed=False,
        gate_results=[SimpleNamespace(gate="lint", blocked=True, passed=False)],
    )
    orch._gate_coalescer = MagicMock()
    orch._gate_coalescer.run.return_value = gate_result

    with (
        patch("bernstein.core.task_lifecycle.get_collector", return_value=collector),
        patch("bernstein.core.task_lifecycle.append_decision"),
    ):
        result = TickResult()
        process_completed_tasks(orch, [task], result)

    assert source_ticket.exists()
    orch._response_cache.store.assert_not_called()
    assert result.verification_failures == [("T-gate", ["quality_gate:lint"])]
    orch._record_provider_health.assert_called_once_with(session, success=False)


def test_move_backlog_ticket_requires_exact_normalized_title_match(tmp_path: Path, make_task: Any) -> None:
    """_move_backlog_ticket does not close a nearby-but-different ticket title via substring matching."""
    open_dir = tmp_path / ".sdd" / "backlog" / "open"
    open_dir.mkdir(parents=True)
    nearby_ticket = open_dir / "auth-ticket.md"
    nearby_ticket.write_text("# Add authentication flow\n", encoding="utf-8")

    task = make_task(title="Add auth")

    _move_backlog_ticket(tmp_path, task)

    assert nearby_ticket.exists()
    assert not (tmp_path / ".sdd" / "backlog" / "closed" / "auth-ticket.md").exists()


def test_enqueue_paired_test_task_is_idempotent(make_task: Any) -> None:
    """Dedicated test-agent slot should create at most one paired QA task per implementation task."""
    list_resp_1 = MagicMock()
    list_resp_1.raise_for_status.return_value = None
    list_resp_1.json.return_value = []

    list_resp_2 = MagicMock()
    list_resp_2.raise_for_status.return_value = None
    list_resp_2.json.return_value = [{"title": "[TEST:T-impl] Add tests for Implement API endpoint", "description": ""}]

    post_resp = MagicMock()
    post_resp.raise_for_status.return_value = None

    orch = SimpleNamespace(
        _config=SimpleNamespace(
            server_url="http://server",
            test_agent=SimpleNamespace(always_spawn=True, model="sonnet", trigger="on_task_complete"),
        ),
        _client=MagicMock(),
    )
    orch._client.get.side_effect = [list_resp_1, list_resp_2]
    orch._client.post.return_value = post_resp

    completed_task = make_task(
        id="T-impl",
        title="Implement API endpoint",
        description="Ship endpoint behavior.",
        role="backend",
    )

    _enqueue_paired_test_task(orch, completed_task)
    _enqueue_paired_test_task(orch, completed_task)

    assert orch._client.post.call_count == 1
    payload = orch._client.post.call_args.kwargs["json"]
    assert payload["role"] == "qa"
    assert payload["depends_on"] == ["T-impl"]
    assert payload["model"] == "sonnet"
