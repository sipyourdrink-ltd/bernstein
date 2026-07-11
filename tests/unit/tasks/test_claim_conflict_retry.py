"""Regression tests for Bug 1 (2026-07-02, fix/claim-conflict-churn).

Root cause: the claim call site sent one CAS claim attempt and, on 409, gave
up for the tick -- but since ``batches`` is recomputed fresh every tick from
the same stale server state, the identical ``expected_version`` got
resubmitted every tick forever. Evidence: 144 consecutive identical
``POST /tasks/109ba3616f03/claim?expected_version=1`` -> 409 responses from
one session against one task in
``work/bernstein/proofs/d2/claim-loop-evidence/d2-minimax-final-snap.tar``.

These tests exercise ``_claim_task_with_conflict_retry`` directly against a
mocked httpx client so the three required behaviours are pinned down without
needing a live server:
  1. 409 -> re-GET shows a fresh version, still OPEN -> retries succeed.
  2. 409 -> re-GET shows the task claimed by a foreign session -> stop,
     don't retry.
  3. 409 forever (stale lock / unmet precondition, version never resolves)
     -> capped at ``_CLAIM_CONFLICT_MAX_ATTEMPTS`` attempts, then gives up
     with a reason instead of looping unboundedly.
Plus the cross-tick backoff bookkeeping (episode counter + backoff window).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import httpx

from bernstein.core.tasks.models import Task, TaskStatus
from bernstein.core.tasks.task_lifecycle import (
    _CLAIM_CONFLICT_MAX_ATTEMPTS,
    _claim_conflict_backoff_active,
    _claim_task_with_conflict_retry,
    _clear_claim_conflict_state,
    _record_claim_conflict_episode,
)


def _make_task(task_id: str = "task-1", version: int = 1, status: TaskStatus = TaskStatus.OPEN) -> Task:
    return Task(
        id=task_id,
        title="Commit changes on feature branch",
        description="desc",
        role="devops",
        version=version,
        status=status,
    )


def _resp(status_code: int, json_body: dict | None = None, text: str = "") -> httpx.Response:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.json.return_value = json_body or {}
    r.text = text
    r.raise_for_status = MagicMock()
    return r


class _FakeOrch:
    """Minimal orchestrator stand-in exposing only ``_client``."""

    def __init__(self, client: MagicMock) -> None:
        self._client = client


# ---------------------------------------------------------------------------
# 1. 409 -> re-fetch shows a fresh version, still OPEN -> retry succeeds.
# ---------------------------------------------------------------------------


def test_409_then_refetch_fresh_version_retries_and_succeeds():
    client = MagicMock(spec=httpx.Client)
    orch = _FakeOrch(client)
    task = _make_task(version=1)

    stale_claim_409 = _resp(409, text="Version conflict: task task-1 is at version 2, expected 1")
    refetch_open_v2 = _resp(
        200, {"id": "task-1", "title": "t", "description": "d", "role": "devops", "version": 2, "status": "open"}
    )
    fresh_claim_200 = _resp(
        200, {"id": "task-1", "title": "t", "description": "d", "role": "devops", "version": 3, "status": "claimed"}
    )

    client.post.side_effect = [stale_claim_409, fresh_claim_200]
    client.get.side_effect = [refetch_open_v2]

    resp, reason = _claim_task_with_conflict_retry(orch, task, "http://test", "session-a")

    assert reason is None
    assert resp is not None
    assert resp.status_code == 200
    # Second POST must have used the freshly observed version (2), not the stale 1.
    second_call_params = client.post.call_args_list[1].kwargs["params"]
    assert second_call_params["expected_version"] == 2
    # The caller's Task object is kept in sync with the refreshed version.
    assert task.version == 2


# ---------------------------------------------------------------------------
# 2. 409 -> re-fetch shows a foreign session now holds the claim -> stop.
# ---------------------------------------------------------------------------


def test_409_then_foreign_claimant_stops_without_retry():
    client = MagicMock(spec=httpx.Client)
    orch = _FakeOrch(client)
    task = _make_task(version=1)

    conflict_409 = _resp(409)
    refetch_claimed_by_other = _resp(
        200,
        {
            "id": "task-1",
            "title": "t",
            "description": "d",
            "role": "devops",
            "version": 2,
            "status": "claimed",
            "claimed_by_session": "some-other-session",
        },
    )
    client.post.side_effect = [conflict_409]
    client.get.side_effect = [refetch_claimed_by_other]

    _result, reason = _claim_task_with_conflict_retry(orch, task, "http://test", "session-a")

    assert reason is not None
    assert "another session" in reason
    assert client.post.call_count == 1  # never retried


# ---------------------------------------------------------------------------
# 3. Persistent 409 (e.g. stale lock) never resolves -> capped, doesn't loop forever.
# ---------------------------------------------------------------------------


def test_persistent_conflict_is_capped_not_infinite():
    client = MagicMock(spec=httpx.Client)
    orch = _FakeOrch(client)
    task = _make_task(version=1)

    always_409 = _resp(409)
    still_open_same_version = _resp(
        200,
        {"id": "task-1", "title": "t", "description": "d", "role": "devops", "version": 1, "status": "open"},
    )

    client.post.side_effect = [always_409] * _CLAIM_CONFLICT_MAX_ATTEMPTS
    client.get.side_effect = [still_open_same_version] * (_CLAIM_CONFLICT_MAX_ATTEMPTS - 1)

    _result, reason = _claim_task_with_conflict_retry(orch, task, "http://test", "session-a")

    assert reason is not None
    assert "gave up" in reason
    assert client.post.call_count == _CLAIM_CONFLICT_MAX_ATTEMPTS  # bounded, not 144


# ---------------------------------------------------------------------------
# 4. Terminal status on re-fetch (task already done elsewhere) -> stop.
# ---------------------------------------------------------------------------


def test_409_then_terminal_status_stops():
    client = MagicMock(spec=httpx.Client)
    orch = _FakeOrch(client)
    task = _make_task(version=1)

    conflict_409 = _resp(409)
    refetch_done = _resp(
        200,
        {"id": "task-1", "title": "t", "description": "d", "role": "devops", "version": 5, "status": "done"},
    )
    client.post.side_effect = [conflict_409]
    client.get.side_effect = [refetch_done]

    _result, reason = _claim_task_with_conflict_retry(orch, task, "http://test", "session-a")

    assert reason is not None
    assert "terminal status" in reason
    assert client.post.call_count == 1


# ---------------------------------------------------------------------------
# Cross-tick backoff bookkeeping.
# ---------------------------------------------------------------------------


def test_claim_conflict_episode_sets_backoff_and_clears_on_success():
    orch = _FakeOrch(MagicMock(spec=httpx.Client))
    assert not _claim_conflict_backoff_active(orch, "task-1")

    _record_claim_conflict_episode(orch, "task-1")
    assert _claim_conflict_backoff_active(orch, "task-1")
    count, backoff_until = orch._claim_conflict_state["task-1"]
    assert count == 1
    assert backoff_until > time.time()

    # A second episode escalates the backoff window further into the future.
    first_backoff_until = backoff_until
    _record_claim_conflict_episode(orch, "task-1")
    count2, backoff_until2 = orch._claim_conflict_state["task-1"]
    assert count2 == 2
    assert backoff_until2 >= first_backoff_until

    _clear_claim_conflict_state(orch, "task-1")
    assert "task-1" not in orch._claim_conflict_state
    assert not _claim_conflict_backoff_active(orch, "task-1")
