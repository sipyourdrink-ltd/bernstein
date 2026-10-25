"""Claim fencing on the terminal endpoints (adversarial review F1/F2).

A remote worker's terminal report is durable and replayed, so a report can
reach the central long after it was produced. Three server behaviours combine
to make that dangerous:

* a departing node's in-flight claims are reopened (graceful unregister and
  the heartbeat reaper);
* ``/complete`` and ``/fail`` auto-claim a task that has reverted to OPEN;
* ``DONE -> FAILED`` is a legal transition.

Without a fence, a replayed report can therefore reopen-and-transition a task a
*different* node now owns. The fence is an opaque ``claim_token`` minted when a
claim is granted and compared **inside the store lock at transition time** --
a GET before the POST cannot close this, because check-then-act is not atomic.

These tests drive the real routes and pin both sides of the line the approved
design requires: a late correction from the same execution claim is accepted,
and a stale report against a newer claim is refused. Backward compatibility for
clients that send no token is deliberate and asserted here too, so the residual
risk is a tested property rather than an accident.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.testclient import TestClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI


_TASK = {
    "title": "Deliver a terminal outcome",
    "description": "Fence behaviour on /complete and /fail.",
    "role": "backend",
    "priority": 1,
    "scope": "small",
    "complexity": "low",
    "estimated_minutes": 5,
}


def _client(tmp_path: Path) -> TestClient:
    app: FastAPI = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    return TestClient(app)


def _new_task(client: TestClient) -> str:
    resp = client.post("/tasks", json=_TASK)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _claim(client: TestClient, task_id: str, *, node: str = "node-a") -> str:
    """Claim *task_id* as *node* and return the claim token the grant carries."""
    resp = client.post(f"/tasks/{task_id}/claim", params={"claimed_by_session": node})
    assert resp.status_code == 200, resp.text
    token = resp.json().get("claim_token")
    assert isinstance(token, str) and token, f"claim grant carried no claim_token: {resp.json()}"
    return token


def _status(client: TestClient, task_id: str) -> str:
    resp = client.get(f"/tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    return str(resp.json()["status"])


class TestClaimTokenIsIssued:
    def test_grant_carries_a_token_and_reopening_clears_it(self, tmp_path: Path) -> None:
        """The token identifies one execution claim, not the task forever."""
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            first = _claim(client, task_id)

            # The token is readable off the task record, so a worker that
            # restarts can recover the fence it owes reports for.
            assert client.get(f"/tasks/{task_id}").json()["claim_token"] == first

            assert client.post(f"/tasks/{task_id}/release", json={"reason": "retry me"}).status_code == 200
            assert client.get(f"/tasks/{task_id}").json()["claim_token"] is None

            second = _claim(client, task_id, node="node-b")
            assert second != first, "a re-claim must mint a fresh execution claim identity"


class TestStaleClaimIsRefused:
    """F1: a replayed report must not touch a task a newer claim owns."""

    def test_stale_completion_cannot_land_on_a_newer_claim(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            stale_token = _claim(client, task_id, node="node-a")

            # node-a's claim is released (unregister / reaper) and node-b takes over.
            assert client.post(f"/tasks/{task_id}/release", json={"reason": "node-a left"}).status_code == 200
            fresh_token = _claim(client, task_id, node="node-b")
            assert fresh_token != stale_token

            resp = client.post(
                f"/tasks/{task_id}/complete",
                json={"result_summary": "replayed from a dead claim", "claim_token": stale_token},
            )
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["error"] == "claim_superseded"
            # The newer claim is untouched: still node-b's, still in flight.
            assert _status(client, task_id) == "claimed"
            assert client.get(f"/tasks/{task_id}").json()["claim_token"] == fresh_token

    def test_stale_failure_cannot_flip_a_done_task_completed_by_a_newer_claim(self, tmp_path: Path) -> None:
        """F2's dangerous half: a *stale* failure must not overwrite DONE."""
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            stale_token = _claim(client, task_id, node="node-a")

            assert client.post(f"/tasks/{task_id}/release", json={"reason": "node-a left"}).status_code == 200
            fresh_token = _claim(client, task_id, node="node-b")
            done = client.post(
                f"/tasks/{task_id}/complete",
                json={"result_summary": "node-b shipped it", "claim_token": fresh_token},
            )
            assert done.status_code == 200, done.text
            assert _status(client, task_id) == "done"

            resp = client.post(f"/tasks/{task_id}/fail", json={"reason": "node-a died", "claim_token": stale_token})
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["error"] == "claim_superseded"
            assert _status(client, task_id) == "done", "a stale report must not rewrite a newer outcome"

    def test_fenced_report_does_not_auto_claim_a_reopened_task(self, tmp_path: Path) -> None:
        """The auto-claim-on-OPEN convenience must not adopt a dead claim.

        Without this, a replayed report would claim the reopened task itself and
        then complete it, which is the exact path F1 opens.
        """
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            stale_token = _claim(client, task_id, node="node-a")
            assert client.post(f"/tasks/{task_id}/release", json={"reason": "node-a left"}).status_code == 200
            assert _status(client, task_id) == "open"

            resp = client.post(
                f"/tasks/{task_id}/complete",
                json={"result_summary": "replay after reopen", "claim_token": stale_token},
            )
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["error"] == "claim_superseded"
            assert _status(client, task_id) == "open", "a stale report must not claim work it no longer owns"

    def test_fence_is_enforced_even_after_the_task_is_reopened(self, tmp_path: Path) -> None:
        """A janitor reopen retires the fence: the old execution is over."""
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            token = _claim(client, task_id)
            assert (
                client.post(
                    f"/tasks/{task_id}/complete", json={"result_summary": "done", "claim_token": token}
                ).status_code
                == 200
            )
            assert client.post(f"/tasks/{task_id}/reopen", json={"reason": "janitor: diff empty"}).status_code == 200

            resp = client.post(f"/tasks/{task_id}/fail", json={"reason": "late", "claim_token": token})
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["error"] == "claim_superseded"
            assert _status(client, task_id) == "open"


class TestSameClaimCorrectionIsAccepted:
    """The half that must keep working: a late failure from the live claim."""

    def test_late_failure_from_the_same_claim_still_lands(self, tmp_path: Path) -> None:
        """DONE -> FAILED is preserved where the reporter holds the live claim.

        This is the case the design must not break: the agent completed its own
        task over HTTP, then the wrapper process died. The node that still owns
        the claim is authorized to correct DONE to FAILED.
        """
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            token = _claim(client, task_id)
            done = client.post(
                f"/tasks/{task_id}/complete", json={"result_summary": "agent self-reported", "claim_token": token}
            )
            assert done.status_code == 200, done.text

            resp = client.post(
                f"/tasks/{task_id}/fail",
                json={"reason": "agent wrapper exited 71 after self-completing", "claim_token": token},
            )
            assert resp.status_code == 200, resp.text
            assert _status(client, task_id) == "failed"

    def test_a_refusal_after_done_is_refused_by_the_table_not_the_fence(self, tmp_path: Path) -> None:
        """The fence must not be the thing that blocks a legal-looking correction.

        DONE -> REFUSED is not in the transition table (only CLAIMED/IN_PROGRESS
        may refuse), so a live claim cannot turn its own completion into a
        refusal. What matters here is *which* refusal the caller gets: a
        ``claim_superseded`` answer would tell the worker its claim is dead and
        send it hunting for a cause that does not exist. The transition table
        answers instead, and the distinction is asserted so the two mechanisms
        cannot be quietly conflated later.
        """
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            token = _claim(client, task_id)
            assert (
                client.post(
                    f"/tasks/{task_id}/complete", json={"result_summary": "agent self-reported", "claim_token": token}
                ).status_code
                == 200
            )

            resp = client.post(
                f"/tasks/{task_id}/complete",
                json={
                    "payload": {
                        "contract": "worker-completion/v1",
                        "kind": "blocked_on_dependency",
                        "detail": "upstream service unreachable from the sandbox",
                        "blocking_dep": "svc-network",
                    },
                    "claim_token": token,
                },
            )
            assert resp.status_code == 409, resp.text
            detail = resp.json()["detail"]
            assert not (isinstance(detail, dict) and detail.get("error") == "claim_superseded"), (
                f"a legal-claim report must not be told its claim is dead: {detail}"
            )
            assert _status(client, task_id) == "done"

    def test_a_live_claim_can_still_refuse_before_completing(self, tmp_path: Path) -> None:
        """Fencing must not disturb the refusal path for its current holder."""
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            token = _claim(client, task_id)

            resp = client.post(
                f"/tasks/{task_id}/complete",
                json={
                    "payload": {
                        "contract": "worker-completion/v1",
                        "kind": "blocked_on_dependency",
                        "detail": "upstream service unreachable from the sandbox",
                        "blocking_dep": "svc-network",
                    },
                    "claim_token": token,
                },
            )
            assert resp.status_code == 200, resp.text
            assert _status(client, task_id) == "refused"


class TestRequeueRetiresAClaim:
    """N1: a requeued task is a fresh start, so the report that requeued it is spent.

    Two paths move a task back to OPEN without going through ``release``, so
    before this fix they left the old ``claim_token`` sitting in the column. An
    OPEN task carries no live claim, yet the stale token matched itself and the
    replayed report was accepted -- it could re-fail a task a retry had just put
    back in the pool, or one a *different* node had since picked up.

    Both paths are reproduced because they are separate callers of separate
    code: batch retry mutates the task in ``batch_ops``, the unblock cascade
    mutates it in the task store. Only the first was visible in the report.
    """

    def test_a_replayed_failure_cannot_land_on_a_batch_retried_task(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            token = _claim(client, task_id)

            # The report lands, but the acknowledgement is lost on the way back,
            # so the node's outbox still considers it owed and will replay it.
            failed = client.post(f"/tasks/{task_id}/fail", json={"reason": "agent exited 71", "claim_token": token})
            assert failed.status_code == 200, failed.text
            assert _status(client, task_id) == "failed"

            # An operator (or the janitor) retries it back into the pool.
            resp = client.post("/tasks/batch-ops", json={"ids": [task_id], "action": "retry"})
            assert resp.status_code == 200, resp.text
            assert _status(client, task_id) == "open"

            replay = client.post(f"/tasks/{task_id}/fail", json={"reason": "agent exited 71", "claim_token": token})
            assert replay.status_code == 409, f"a spent claim must not re-fail a requeued task: {replay.text}"
            assert replay.json()["detail"]["error"] == "claim_superseded"
            assert _status(client, task_id) == "open", "the retried task must stay queued for whoever picks it up next"

    def test_a_replayed_failure_cannot_land_on_a_task_unblocked_by_its_dependency(self, tmp_path: Path) -> None:
        """The unblock cascade is the same hole reached from the other direction."""
        with _client(tmp_path) as client:
            upstream = _new_task(client)
            assert client.post(f"/tasks/{upstream}/complete", json={"result_summary": "first pass"}).status_code == 200

            created = client.post("/tasks", json={**_TASK, "title": "Downstream", "depends_on": [upstream]})
            assert created.status_code == 201, created.text
            downstream = str(created.json()["id"])

            # The dependent is only claimable once its dependency is done, so the
            # live claim necessarily post-dates the upstream's first success.
            token = _claim(client, downstream, node="node-a")

            # Upstream is corrected to FAILED (a legal DONE -> FAILED), which
            # strands the in-flight dependent.
            assert (
                client.post(f"/tasks/{upstream}/fail", json={"reason": "upstream result was bogus"}).status_code == 200
            )
            assert _status(client, downstream) == "blocked_by_failed_dep"

            # Upstream is retried and succeeds, cascading the dependent back to
            # OPEN -- a fresh start that retires node-a's spent claim.
            retry = client.post("/tasks/batch-ops", json={"ids": [upstream], "action": "retry"})
            assert retry.status_code == 200, retry.text
            completed = client.post(f"/tasks/{upstream}/complete", json={"result_summary": "fixed on the second pass"})
            assert completed.status_code == 200, completed.text
            assert _status(client, downstream) == "open"

            replay = client.post(
                f"/tasks/{downstream}/fail", json={"reason": "late replay from node-a", "claim_token": token}
            )
            assert replay.status_code == 409, (
                f"a spent claim must not re-fail a task the unblock cascade just reopened: {replay.text}"
            )
            assert replay.json()["detail"]["error"] == "claim_superseded"
            assert _status(client, downstream) == "open"


class TestUnfencedClientsStayAccepted:
    """Backward compatibility is deliberate; the residual risk is asserted, not implied.

    A legacy worker, an in-agent completion curl, or any client that predates
    fencing sends no ``claim_token``. Refusing those would break every existing
    deployment, so they keep today's behaviour. The consequence -- an unfenced
    report can still land on a newer claim -- is pinned here so it is visible
    as a known, documented gap rather than a surprise.
    """

    def test_unfenced_completion_still_lands_on_another_nodes_claim(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            _claim(client, task_id, node="node-b")

            resp = client.post(f"/tasks/{task_id}/complete", json={"result_summary": "legacy client, no token"})
            assert resp.status_code == 200, resp.text
            assert _status(client, task_id) == "done"

    def test_unfenced_report_still_auto_claims_a_reopened_task(self, tmp_path: Path) -> None:
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            assert client.post(f"/tasks/{task_id}/claim").status_code == 200
            assert client.post(f"/tasks/{task_id}/release", json={"reason": "back to pool"}).status_code == 200

            resp = client.post(f"/tasks/{task_id}/complete", json={"result_summary": "legacy replay"})
            assert resp.status_code == 200, resp.text
            assert _status(client, task_id) == "done"

    def test_a_supplied_but_bogus_token_is_refused_not_ignored(self, tmp_path: Path) -> None:
        """Sending a wrong token is not the same as sending none.

        Otherwise a truncated or mixed-up token would silently downgrade to the
        unfenced path, which is exactly the hole the fence exists to close.
        """
        with _client(tmp_path) as client:
            task_id = _new_task(client)
            _claim(client, task_id, node="node-a")

            resp = client.post(
                f"/tasks/{task_id}/complete",
                json={"result_summary": "forged", "claim_token": "0" * 32},
            )
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["error"] == "claim_superseded"
            assert _status(client, task_id) == "claimed"
