import logging
from unittest.mock import MagicMock

import httpx
import pytest
from bernstein.core.models import TaskStatus

# Both imports point to the same function: task_lifecycle re-exports from task_retry
from bernstein.core.task_lifecycle import retry_or_fail_task as retry_lifecycle
from bernstein.core.task_retry import retry_or_fail_task as retry_completion


# Mock Task class just enough to pass the function
class MockScope:
    value = "small"


class MockComplexity:
    value = "low"


class MockTaskType:
    value = "feature"


class MockTask:
    def __init__(self, task_id, description="", title="Test Task"):
        self.id = task_id
        self.title = title
        self.description = description
        self.role = "backend"
        self.priority = 1
        self.scope = MockScope()
        self.complexity = MockComplexity()
        self.estimated_minutes = 10
        self.depends_on = []
        self.owned_files = []
        self.task_type = MockTaskType()
        self.model = "sonnet"
        self.effort = "high"
        self.max_output_tokens = None
        self.max_turns = None
        self.meta_messages = []
        self.completion_signals = []
        self.metadata = {}
        # audit-017: typed retry fields are the source of truth.
        self.retry_count = 0
        self.max_retries = 3
        self.retry_delay_s = 0.0
        self.terminal_reason = None


@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_retry_or_fail_task_transient(retry_func):
    mock_client = MagicMock(spec=httpx.Client)

    # Task has 0 retries so far
    task = MockTask("task-123", description="")
    tasks_snapshot = {"active": [task]}

    retried_ids = set()

    retry_func(
        task_id="task-123",
        reason="API Rate Limit Exceeded",
        client=mock_client,
        server_url="http://test",
        max_task_retries=1,  # Default is 1, but transient should override to 3
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
    )

    # Should have posted a new task
    call_args = mock_client.post.call_args_list
    assert any(call[0][0].endswith("/tasks") for call in call_args)
    assert "task-123" in retried_ids


@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_retry_or_fail_task_permanent(retry_func):
    mock_client = MagicMock(spec=httpx.Client)

    # Task has 0 retries
    task = MockTask("task-456", description="")
    tasks_snapshot = {"active": [task]}

    retried_ids = set()

    retry_func(
        task_id="task-456",
        reason="SyntaxError in main.py",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,  # Default is 3, but permanent should override to 0
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
    )

    # Should NOT have posted a new task (no retry)
    call_args = mock_client.post.call_args_list
    assert not any(call[0][0].endswith("/tasks") for call in call_args)
    # It should call fail_task, which uses client.delete or hit an endpoint.
    # Actually wait, fail_task hits `client.post(f"{base}/tasks/{task_id}/fail")`
    # We just need to ensure `post` wasn't called for the original task resubmission.
    # Actually fail_task does a PUT or POST to fail it. Let's just check that `task_body` wasn't posted to `/tasks`.
    call_args = mock_client.post.call_args_list
    assert not any(call[0][0].endswith("/tasks") for call in call_args)


# ---------------------------------------------------------------------------
# Auto-spawn guard regression tests.
#
# Root cause: retry_or_fail_task is a SECOND spawn site for auto-spawned
# meta-tasks (evolution-loop "Upgrade: ..." proposals, watchdog
# "Watchdog triage: ..." tasks) that completely bypassed AutoSpawnGuard --
# only the CREATION-time call sites (orchestrator_evolve, watchdog) consulted
# the guard. See work/bernstein/proofs/d2/minimax/sdd-snapshot/runtime/
# tasks.jsonl (attempt-3 evidence): a single "Upgrade: Improve task success
# rate" proposal recreated itself twice more via THIS function
# (b66d312a1b4c -> b711648c45a0 -> 4e816f62948b), never touching the guard.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_retry_of_meta_task_is_suppressed_by_ancestry_cap(retry_func, tmp_path):
    """(ii) Ancestry cap: retrying a task whose OWN title already carries a
    known meta-task prefix ("Upgrade:") is a depth-2 auto-spawn (a meta-task
    about itself) and must be refused -- the task is routed straight to
    permanent failure instead of a new task row being POSTed to /tasks.
    This is the "grandchild" auto-spawn the ancestry cap exists to block:
    without it, a structurally-unsolvable meta-task recreates itself up to
    max_retries times with zero forward progress.
    """
    mock_client = MagicMock(spec=httpx.Client)

    task = MockTask("meta-1", title="Upgrade: Improve task success rate")
    tasks_snapshot = {"active": [task]}
    retried_ids: set[str] = set()

    retry_func(
        task_id="meta-1",
        reason="Agent backend-abc died; no completion signals and no files modified",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
        workdir=tmp_path,
    )

    # No new "Upgrade: ..." task row was created via POST /tasks.
    post_calls = mock_client.post.call_args_list
    assert not any(call[0][0].endswith("/tasks") for call in post_calls), (
        f"retry of a meta-task must not recreate it via POST /tasks, got: {post_calls}"
    )
    # It was routed to permanent failure instead.
    assert any(call[0][0].endswith("/meta-1/fail") for call in post_calls), (
        f"expected a /tasks/meta-1/fail call, got: {post_calls}"
    )


@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_retry_of_non_meta_task_is_unaffected_by_guard(retry_func, tmp_path):
    """Control: an ordinary (non meta-task) retry is unaffected by the guard
    even when a workdir is supplied -- the guard only intercepts titles
    matching a known auto-spawn prefix."""
    mock_client = MagicMock(spec=httpx.Client)

    task = MockTask("normal-1", title="Implement hello subcommand in cli.py")
    tasks_snapshot = {"active": [task]}
    retried_ids: set[str] = set()

    retry_func(
        task_id="normal-1",
        reason="Agent backend-abc died; no completion signals and no files modified",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
        workdir=tmp_path,
    )

    post_calls = mock_client.post.call_args_list
    assert any(call[0][0].endswith("/tasks") for call in post_calls)


def test_retry_of_meta_task_logs_info_refusal_line(caplog):
    """(iii) The refusal must be logged at INFO with the reason and ancestry
    depth -- logging IS the debugging interface, per team convention."""
    mock_client = MagicMock(spec=httpx.Client)
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        task = MockTask("meta-2", title="Upgrade: Improve task success rate")
        tasks_snapshot = {"active": [task]}
        retried_ids: set[str] = set()

        with caplog.at_level(logging.INFO):
            retry_lifecycle(
                task_id="meta-2",
                reason="Agent backend-xyz died; no completion signals and no files modified",
                client=mock_client,
                server_url="http://test",
                max_task_retries=3,
                retried_task_ids=retried_ids,
                tasks_snapshot=tasks_snapshot,
                workdir=Path(tmp),
            )

    messages = [r.getMessage() for r in caplog.records]
    refusal_lines = [m for m in messages if "Refusing to re-spawn meta-task" in m]
    assert refusal_lines, f"expected a refusal log line, got: {messages}"
    assert any("reason=depth" in m and "ancestry_depth=2" in m for m in refusal_lines)
    # The guard's own uniform decision line must also be present.
    assert any("auto_spawn_decision" in m for m in messages)


@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_retry_of_meta_task_without_workdir_falls_back_to_legacy_behavior(retry_func):
    """Callers that omit workdir (ad-hoc scripts, legacy tests) keep the
    historical unguarded retry behaviour -- the guard requires a workdir to
    persist its cap counter."""
    mock_client = MagicMock(spec=httpx.Client)

    task = MockTask("meta-3", title="Upgrade: Improve task success rate")
    tasks_snapshot = {"active": [task]}
    retried_ids: set[str] = set()

    retry_func(
        task_id="meta-3",
        reason="Agent backend-abc died; no completion signals and no files modified",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
        # workdir intentionally omitted
    )

    post_calls = mock_client.post.call_args_list
    assert any(call[0][0].endswith("/tasks") for call in post_calls)


# ---------------------------------------------------------------------------
# Bug 2 (2026-07-02, fix/claim-conflict-churn): hard retry-count cap for
# REGULAR (non-meta) task lineages.
#
# Root cause: with task.max_retries=3 and a "died" (transient) failure
# reason resolving dynamic_limit to 3, retry_or_fail_task allowed retry_count
# to climb 0 -> 1 -> 2 -> 3 before permanent failure -- 4 total attempts.
# Evidence (work/bernstein/proofs/d2/claim-loop-evidence/d2-minimax-final-snap.tar,
# tasks.jsonl) showed "Add test for hello subcommand" and "Commit changes on
# feature branch" each respawning 3x inside one 12-minute run with zero
# forward progress (every agent died for the same underlying reason). The
# fix caps the effective retry ceiling at 2 (3 total attempts) for every
# task lineage, independent of task.max_retries / the reason-derived
# dynamic_limit.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_regular_task_retry_capped_at_two_then_permanent_fail(retry_func):
    """A non-meta task with retry_count already at 2 (its 3rd attempt just
    failed) must be permanently failed, not retried a 4th time -- even
    though task.max_retries=3 and the failure reason is transient (which
    would otherwise raise dynamic_limit to 3, allowing one more retry)."""
    mock_client = MagicMock(spec=httpx.Client)

    task = MockTask("regular-1", title="Commit changes on feature branch")
    task.retry_count = 2  # already retried twice (0 -> 1 -> 2)
    task.max_retries = 3  # would normally allow one more retry
    tasks_snapshot = {"active": [task]}
    retried_ids: set[str] = set()

    retry_func(
        task_id="regular-1",
        reason="Agent devops-abc died; janitor failed: ['test_passes: git log --oneline -1 | grep -q hello']",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
    )

    post_calls = mock_client.post.call_args_list
    # No new task row created -- the lineage must not respawn a 4th time.
    assert not any(call[0][0].endswith("/tasks") for call in post_calls), (
        f"expected no retry POST /tasks once the hard cap is reached, got: {post_calls}"
    )
    # Routed to permanent failure instead.
    assert any(call[0][0].endswith("/regular-1/fail") for call in post_calls), (
        f"expected a /tasks/regular-1/fail call, got: {post_calls}"
    )


@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_regular_task_retry_allowed_below_hard_cap(retry_func):
    """Control: a task with retry_count=1 (2nd attempt just failed) is still
    under the hard cap of 2 and gets one more retry."""
    mock_client = MagicMock(spec=httpx.Client)

    task = MockTask("regular-2", title="Add test for hello subcommand")
    task.retry_count = 1
    task.max_retries = 3
    tasks_snapshot = {"active": [task]}
    retried_ids: set[str] = set()

    retry_func(
        task_id="regular-2",
        reason="Agent qa-abc died; janitor failed: ['test_passes: pytest test_cli.py -k hello']",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
    )

    post_calls = mock_client.post.call_args_list
    assert any(call[0][0].endswith("/tasks") for call in post_calls), (
        f"expected one more retry POST /tasks under the hard cap, got: {post_calls}"
    )


def test_retry_cap_decision_is_logged_with_original_task_id(caplog):
    """LOG every retry decision: original task id, attempt number, reason, verdict."""
    mock_client = MagicMock(spec=httpx.Client)

    task = MockTask("regular-3", title="Commit changes on feature branch")
    task.retry_count = 2
    task.max_retries = 3
    task.metadata = {"original_task_id": "orig-root-task"}
    tasks_snapshot = {"active": [task]}
    retried_ids: set[str] = set()

    with caplog.at_level(logging.INFO):
        retry_lifecycle(
            task_id="regular-3",
            reason="Agent devops-abc died",
            client=mock_client,
            server_url="http://test",
            max_task_retries=3,
            retried_task_ids=retried_ids,
            tasks_snapshot=tasks_snapshot,
        )

    messages = [r.getMessage() for r in caplog.records]
    assert any("retry_or_fail_task decision inputs" in m and "orig-root-task" in m for m in messages), (
        f"expected a decision-inputs log line with the original_task_id, got: {messages}"
    )
    assert any("verdict=permanent_fail" in m and "orig-root-task" in m for m in messages), (
        f"expected a permanent_fail verdict log line, got: {messages}"
    )


# ---------------------------------------------------------------------------
# Planning-retry race guard (#4309).
#
# A failed planning ("manager") task's retry can be created after a sibling
# planning task -- decomposing the exact same goal -- has already reached
# DONE: two independent retry-creation call sites (the tick-loop
# maybe_retry_task sweep and this reap-path function) can each react to the
# same failure, and a run can be worked by more than one orchestrator
# process against the same shared task board, so in-process dedup
# (retried_task_ids) never sees the other side. Left unguarded, the
# redundant retry gets claimed by a second manager agent and re-decomposes
# the same goal, doubling every downstream task.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_planning_retry_cancelled_when_sibling_already_done(retry_func, caplog):
    """A' (the retry of failed planning task A) must be created and then
    immediately cancelled -- not left open/claimable -- once a sibling
    planning task B sharing A's decomposition lineage has already finished
    the decomposition."""
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.return_value.json.return_value = {"id": "planning-A-retry-1"}

    task_a = MockTask("planning-A", title="Decompose goal")
    task_a.role = "manager"
    # task_a is itself the lineage root (no metadata.original_task_id yet),
    # so its lineage id is its own task id.

    sibling_b = MockTask("planning-B", title="Decompose goal")
    sibling_b.role = "manager"
    sibling_b.status = TaskStatus.DONE
    sibling_b.metadata = {"original_task_id": "planning-A"}

    tasks_snapshot = {"active": [task_a], "done": [sibling_b]}
    retried_ids: set[str] = set()

    with caplog.at_level(logging.INFO):
        retry_func(
            task_id="planning-A",
            reason="Agent manager-abc died mid-decomposition",
            client=mock_client,
            server_url="http://test",
            max_task_retries=3,
            retried_task_ids=retried_ids,
            tasks_snapshot=tasks_snapshot,
        )

    post_calls = mock_client.post.call_args_list
    # The retry row is created -- visible on the board, not silently dropped.
    assert any(call[0][0].endswith("/tasks") for call in post_calls), (
        f"expected the retry to still be created via POST /tasks, got: {post_calls}"
    )
    # ... and then immediately cancelled, with a reason, instead of left open.
    cancel_calls = [call for call in post_calls if call[0][0].endswith("/planning-A-retry-1/cancel")]
    assert cancel_calls, f"expected a cancel call for the duplicate retry, got: {post_calls}"
    assert "planning-B" in cancel_calls[0].kwargs["json"]["reason"]
    # The original failed task is still marked failed/superseded as usual.
    assert any(call[0][0].endswith("/planning-A/fail") for call in post_calls), (
        f"expected the original task to still be failed, got: {post_calls}"
    )

    messages = [r.getMessage() for r in caplog.records]
    assert any("verdict=cancel_planning_retry" in m and "planning-B" in m for m in messages), (
        f"expected a cancel_planning_retry verdict log line, got: {messages}"
    )


@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_planning_retry_unaffected_when_no_sibling_done(retry_func):
    """Control: an ordinary planning retry with no completed sibling stays
    open exactly like before the guard existed."""
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.return_value.json.return_value = {"id": "planning-C-retry-1"}

    task_c = MockTask("planning-C", title="Decompose goal")
    task_c.role = "manager"
    tasks_snapshot = {"active": [task_c]}
    retried_ids: set[str] = set()

    retry_func(
        task_id="planning-C",
        reason="Agent manager-abc died mid-decomposition",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
    )

    post_calls = mock_client.post.call_args_list
    assert any(call[0][0].endswith("/tasks") for call in post_calls)
    assert not any(call[0][0].endswith("/cancel") for call in post_calls), (
        f"no sibling is done -- the retry must not be cancelled, got: {post_calls}"
    )


@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_planning_retry_unaffected_by_unrelated_completed_manager_task(retry_func):
    """Control: a DONE manager-role task for a *different* goal (different
    decomposition lineage) must not cancel this retry -- e.g. two unrelated
    evolve-cycle manager tasks running concurrently are not siblings."""
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.return_value.json.return_value = {"id": "planning-D-retry-1"}

    task_d = MockTask("planning-D", title="Decompose goal")
    task_d.role = "manager"

    unrelated_done = MockTask("planning-E", title="Evolve cycle 9: new_features")
    unrelated_done.role = "manager"
    unrelated_done.status = TaskStatus.DONE
    unrelated_done.metadata = {"original_task_id": "some-other-goal-root"}

    tasks_snapshot = {"active": [task_d], "done": [unrelated_done]}
    retried_ids: set[str] = set()

    retry_func(
        task_id="planning-D",
        reason="Agent manager-abc died mid-decomposition",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
    )

    post_calls = mock_client.post.call_args_list
    assert any(call[0][0].endswith("/tasks") for call in post_calls)
    assert not any(call[0][0].endswith("/cancel") for call in post_calls), (
        f"unrelated goal's completed manager task must not cancel this retry, got: {post_calls}"
    )


@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_worker_retry_unaffected_by_completed_planning_sibling(retry_func):
    """Control: the guard is scoped to the planning role only -- an ordinary
    worker task's retry is unaffected even when a same-lineage DONE
    manager-role task exists (e.g. the manager that spawned this worker task
    already finished, which is the normal, expected shape)."""
    mock_client = MagicMock(spec=httpx.Client)
    mock_client.post.return_value.json.return_value = {"id": "worker-1-retry-1"}

    worker_task = MockTask("worker-1", title="Implement hello subcommand in cli.py")
    # role defaults to "backend" on MockTask

    done_manager = MockTask("planning-F", title="Decompose goal")
    done_manager.role = "manager"
    done_manager.status = TaskStatus.DONE
    done_manager.metadata = {"original_task_id": "worker-1"}  # contrived id collision

    tasks_snapshot = {"active": [worker_task], "done": [done_manager]}
    retried_ids: set[str] = set()

    retry_func(
        task_id="worker-1",
        reason="Agent backend-abc died; no completion signals and no files modified",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3,
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
    )

    post_calls = mock_client.post.call_args_list
    assert any(call[0][0].endswith("/tasks") for call in post_calls)
    assert not any(call[0][0].endswith("/cancel") for call in post_calls), (
        f"worker retries must be unaffected by the planning-role guard, got: {post_calls}"
    )
