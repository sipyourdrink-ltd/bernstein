"""Integration: worker terminal reports against the real central.

The unit tests for this path (#2808) script canned responses, which means they
would keep passing if the worker parsed a body shape the server never sends.
Two decisions in ``WorkerLoop`` depend on the real contract:

* an HTTP 2xx only settles a report if the response names the same task id, so
  a 2xx whose body is not the task must not be mistaken for an ack;
* a 409 is resolved by reading the task record, so the status vocabulary the
  server actually uses decides whether a report is retired or retried.

Both are asserted here against ``create_app`` rather than a mock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from bernstein.cli.commands.worker_cmd import WorkerLoop
from bernstein.core.orchestration.terminal_report_outbox import OUTCOME_DONE, OUTCOME_FAILED

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi.testclient import TestClient

SERVER_URL = "http://testserver"

_TASK = {
    "title": "Report a terminal outcome",
    "description": "The worker reaps the agent and must transition the task itself.",
    "role": "backend",
    "priority": 1,
    "scope": "small",
    "complexity": "low",
    "estimated_minutes": 5,
}


class _CentralDown:
    """A central that cannot be reached at all (the outage half of the test)."""

    def post(self, *args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("connection refused")

    def get(self, *args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("connection refused")


def _make_loop(tmp_path: Path, *, name: str = "node-a") -> WorkerLoop:
    loop = WorkerLoop(
        server_url=SERVER_URL,
        name=name,
        auth_token="secret-token",
        adapter="claude",
        workdir=tmp_path,
    )
    # Collapse the backoff floor: these tests drive several delivery attempts
    # inside one second, which production would space out over minutes.
    loop._outbox._base_delay_s = 0.0
    loop._outbox._max_delay_s = 0.0
    return loop


def _new_task(client: TestClient, *, claim: bool = True) -> str:
    resp = client.post("/tasks", json=_TASK)
    assert resp.status_code == 201, resp.text
    task_id = resp.json()["id"]
    if claim:
        claimed = client.post(f"/tasks/{task_id}/claim")
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["status"] == "claimed"
    return task_id


def _status(client: TestClient, task_id: str) -> str:
    resp = client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    return str(resp.json()["status"])


def test_acknowledged_completion_settles_against_the_real_server(tmp_path: Path, test_client: TestClient) -> None:
    """A real 200 body really does name the task, so the report retires."""
    task_id = _new_task(test_client)
    loop = _make_loop(tmp_path)
    loop._outbox.append(task_id, OUTCOME_DONE, "agent exited with code 0")

    loop._report_finished(test_client)

    assert _status(test_client, task_id) == "done"
    assert loop._outbox.pending() == []


def test_a_dead_central_leaves_the_report_on_disk_for_the_next_node(tmp_path: Path, test_client: TestClient) -> None:
    """Outage, then restart: the outcome is owed by whoever comes back."""
    task_id = _new_task(test_client)
    first = _make_loop(tmp_path)
    first._outbox.append(task_id, OUTCOME_DONE, "agent exited with code 0")

    first._report_finished(_CentralDown())  # type: ignore[arg-type]

    assert [r.task_id for r in first._outbox.pending()] == [task_id]
    assert _status(test_client, task_id) == "claimed"

    # The node restarts over the same workdir and drains what it still owes.
    second = _make_loop(tmp_path)
    second._report_finished(test_client)

    assert _status(test_client, task_id) == "done"
    assert second._outbox.pending() == []


def test_duplicate_completion_reconciles_against_the_real_server(tmp_path: Path, test_client: TestClient) -> None:
    """The ack was lost but the transition landed: the task record says so."""
    task_id = _new_task(test_client)
    landed = test_client.post(f"/tasks/{task_id}/complete", json={"result_summary": "shipped"})
    assert landed.status_code == 200, landed.text

    loop = _make_loop(tmp_path)
    loop._outbox.append(task_id, OUTCOME_DONE, "agent exited with code 0")

    loop._report_finished(test_client)

    # One post, then the 409 was answered from the task record - not retried
    # forever, and not turned into a second transition.
    assert loop._outbox.pending() == []
    assert _status(test_client, task_id) == "done"


def test_conflicting_outcome_never_overwrites_the_real_server(
    tmp_path: Path, test_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The central already failed this task; our "done" must not paper over it."""
    task_id = _new_task(test_client)
    failed = test_client.post(f"/tasks/{task_id}/fail", json={"reason": "janitor rejected the merge"})
    assert failed.status_code == 200, failed.text

    loop = _make_loop(tmp_path)
    fails_posted: list[str] = []
    real_fail = loop._fail_task

    def _spy(client: httpx.Client, task: str, reason: str, claim_token: str | None = None) -> httpx.Response | None:
        fails_posted.append(task)
        return real_fail(client, task, reason, claim_token)

    monkeypatch.setattr(loop, "_fail_task", _spy)
    loop._outbox.append(task_id, OUTCOME_DONE, "agent exited with code 0")

    loop._report_finished(test_client)

    assert fails_posted == []
    assert _status(test_client, task_id) == "failed"
    assert loop._outbox.pending() == []


def test_failure_report_lands_on_a_task_the_agent_never_completed(tmp_path: Path, test_client: TestClient) -> None:
    """The other direction: a failed agent must not leave the task claimed."""
    task_id = _new_task(test_client)
    loop = _make_loop(tmp_path)
    loop._outbox.append(task_id, OUTCOME_FAILED, "agent exited with code 3")

    loop._report_finished(test_client)

    assert _status(test_client, task_id) == "failed"
    assert loop._outbox.pending() == []


def test_second_node_never_reposts_the_outcome_it_does_not_own(tmp_path: Path, test_client: TestClient) -> None:
    """Node tagging holds against the real server: one outcome, one transition."""
    task_id = _new_task(test_client)
    owner = _make_loop(tmp_path, name="node-a")
    owner._outbox.append(task_id, OUTCOME_DONE, "agent exited with code 0")
    peer = _make_loop(tmp_path, name="node-b")

    peer._report_finished(test_client)
    assert _status(test_client, task_id) == "claimed"

    owner._report_finished(test_client)
    assert _status(test_client, task_id) == "done"


# ---------------------------------------------------------------------------
# Claim fencing on the worker side (adversarial review F1/F2).
#
# The server now refuses a terminal report whose ``claim_token`` no longer
# identifies the live claim. Two things have to hold here for that to be a fix
# rather than a new outage: the token must travel with the report (including
# across a restart, since the whole point of the outbox is that reports are
# replayed later), and the refusal must *settle* the report. A 409 that falls
# through to the generic reconcile path reads the newer claim's ``claimed``
# status, is not treated as terminal, and is retried forever -- a report that
# can never succeed occupying the queue indefinitely.
# ---------------------------------------------------------------------------


def _claim(client: TestClient, task_id: str, *, node: str = "node-a") -> str:
    resp = client.post(f"/tasks/{task_id}/claim", params={"claimed_by_session": node})
    assert resp.status_code == 200, resp.text
    token = resp.json().get("claim_token")
    assert isinstance(token, str) and token, f"claim grant carried no claim_token: {resp.json()}"
    return token


def test_the_token_a_report_carries_is_the_one_the_server_mints(tmp_path: Path, test_client: TestClient) -> None:
    """Capture is real: the worker holds the same fence the central issued."""
    task_id = _new_task(test_client, claim=False)
    token = _claim(test_client, task_id)
    assert test_client.get(f"/tasks/{task_id}").json()["claim_token"] == token

    loop = _make_loop(tmp_path)
    loop._outbox.append(task_id, OUTCOME_DONE, "agent exited with code 0", claim_token=token)

    assert [r.claim_token for r in loop._outbox.pending()] == [token]


def test_a_report_replays_the_same_fence_after_a_restart(tmp_path: Path, test_client: TestClient) -> None:
    """The replayed report must name the claim it was granted, not a new one.

    Durability is the entire reason a stale report is possible, so the fence has
    to be as durable as the report it guards. A token that lived only in memory
    would come back ``None`` after a restart, which the server reads as an
    unfenced legacy caller and accepts -- silently reopening the hole.
    """
    task_id = _new_task(test_client, claim=False)
    token = _claim(test_client, task_id)

    first = _make_loop(tmp_path)
    first._outbox.append(task_id, OUTCOME_DONE, "agent exited with code 0", claim_token=token)

    second = _make_loop(tmp_path)
    owed = second._outbox.pending()
    assert len(owed) == 1
    assert owed[0].claim_token == token, "a restart must not downgrade a fenced report to an unfenced one"


def test_a_superseded_report_is_retired_not_retried_forever(tmp_path: Path, test_client: TestClient) -> None:
    """The server named our claim dead; that settles the report."""
    task_id = _new_task(test_client, claim=False)
    stale = _claim(test_client, task_id, node="node-a")
    assert test_client.post(f"/tasks/{task_id}/release", json={"reason": "node-a left"}).status_code == 200
    fresh = _claim(test_client, task_id, node="node-b")

    loop = _make_loop(tmp_path, name="node-a")
    loop._outbox.append(task_id, OUTCOME_DONE, "agent exited with code 0", claim_token=stale)

    loop._report_finished(test_client)

    assert loop._outbox.pending() == [], "a report that can never succeed must not hold the queue"
    # And the refusal left the newer claim exactly as it found it.
    body = test_client.get(f"/tasks/{task_id}").json()
    assert body["status"] == "claimed"
    assert body["claim_token"] == fresh

    # A later cycle has nothing left to post.
    loop._report_finished(test_client)
    assert test_client.get(f"/tasks/{task_id}").json()["status"] == "claimed"


def test_a_bogus_fence_is_not_mistaken_for_an_ack(tmp_path: Path, test_client: TestClient) -> None:
    """A rejected report must not be recorded as delivered.

    This is the assertion that the token really rides on the request: were it
    dropped from the body the server would answer 200 and the task would be
    done, so a still-claimed task is the proof it was sent and refused.
    """
    task_id = _new_task(test_client)
    loop = _make_loop(tmp_path)
    loop._outbox.append(task_id, OUTCOME_DONE, "agent exited with code 0", claim_token="0" * 32)

    loop._report_finished(test_client)

    assert _status(test_client, task_id) == "claimed"
    assert loop._outbox.pending() == []


def test_a_late_failure_from_the_same_claim_still_corrects_done(tmp_path: Path, test_client: TestClient) -> None:
    """F2's legitimate half, driven by the worker: DONE then FAILED."""
    task_id = _new_task(test_client, claim=False)
    token = _claim(test_client, task_id)
    loop = _make_loop(tmp_path)

    loop._outbox.append(task_id, OUTCOME_DONE, "agent exited with code 0", claim_token=token)
    loop._report_finished(test_client)
    assert _status(test_client, task_id) == "done"

    loop._outbox.append(task_id, OUTCOME_FAILED, "agent wrapper exited 71 after self-completing", claim_token=token)
    loop._report_finished(test_client)

    assert _status(test_client, task_id) == "failed"
    assert loop._outbox.pending() == []
