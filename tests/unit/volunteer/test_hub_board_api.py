"""Tests for the hub-native task board HTTP surface.

These cover the path the hub exists to make possible: a task that originates at
the hub, is listed by the hub, and is claimed by a donor with no git forge
anywhere in the loop.  They also pin the reserved-namespace guard, so an id in
the hub-native namespace cannot be leased unless the board really carries it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from bernstein.core.volunteer.budget import VolunteerBudget
from bernstein.core.volunteer.hub_app import (
    SCOPE_VOLUNTEER_CLAIM,
    SCOPE_VOLUNTEER_PUBLISH,
    VolunteerAuthenticator,
    build_hub_app,
)
from bernstein.core.volunteer.lease_store import LeaseStore
from bernstein.core.volunteer.task_board import HUB_TASK_ID_PREFIX, TaskBoard

if TYPE_CHECKING:
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


T0 = 1_700_000_000.0
TTL = 60

REPO_URL = "https://example.invalid/proj.git"

TASK_BODY = {
    "repo_url": REPO_URL,
    "title": "Fix the thing",
    "body": "The thing is broken.",
}


class _FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _enroll(client: TestClient) -> str:
    pubkey: Ed25519PublicKey = Ed25519PrivateKey.generate().public_key()
    pem = pubkey.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    resp = client.post("/volunteer/enroll", json={"public_key_pem": pem})
    assert resp.status_code == 201, f"enroll failed: {resp.text}"
    return str(resp.json()["worker_id"])


def _make_app(
    tmp_path: Path,
    *,
    with_board: bool = True,
    budget: VolunteerBudget | None = None,
    authenticator: VolunteerAuthenticator | None = None,
) -> TestClient:
    clock = _FakeClock()
    lease_store = LeaseStore(
        tmp_path / "leases.jsonl",
        clock=clock,
        budget=budget,
        budget_ledger_path=tmp_path / "ledger.json",
    )
    board = TaskBoard(tmp_path / "tasks.jsonl", clock=clock) if with_board else None
    app = build_hub_app(lease_store, authenticator=authenticator, task_board=board)
    return TestClient(app)


def test_hub_native_task_is_claimable_with_no_git_forge_issue_behind_it(
    tmp_path: Path,
) -> None:
    """A task published at the hub can be listed and leased end to end."""
    client = _make_app(tmp_path)
    worker_id = _enroll(client)

    published = client.post("/volunteer/tasks", json=TASK_BODY)
    assert published.status_code == 201, published.text
    task_id = published.json()["task_id"]
    assert task_id.startswith(HUB_TASK_ID_PREFIX)

    listed = client.get("/volunteer/tasks")
    assert listed.status_code == 200, listed.text
    assert [task["task_id"] for task in listed.json()["tasks"]] == [task_id]

    claimed = client.post(
        f"/volunteer/tasks/{task_id}/claim",
        json={"worker_id": worker_id, "ttl_seconds": TTL},
    )
    assert claimed.status_code == 201, claimed.text
    assert claimed.json()["task_id"] == task_id


def test_claim_of_an_unpublished_hub_native_task_is_refused(tmp_path: Path) -> None:
    """An id in the hub namespace that the board never issued has no lease."""
    client = _make_app(tmp_path)
    worker_id = _enroll(client)

    resp = client.post(
        f"/volunteer/tasks/{HUB_TASK_ID_PREFIX}{'0' * 64}/claim",
        json={"worker_id": worker_id, "ttl_seconds": TTL},
    )
    assert resp.status_code == 404, resp.text


def test_hub_namespace_is_unclaimable_when_no_board_is_configured(
    tmp_path: Path,
) -> None:
    """Without a board there are no hub-native tasks, so none may be leased."""
    client = _make_app(tmp_path, with_board=False)
    worker_id = _enroll(client)

    resp = client.post(
        f"/volunteer/tasks/{HUB_TASK_ID_PREFIX}{'0' * 64}/claim",
        json={"worker_id": worker_id, "ttl_seconds": TTL},
    )
    assert resp.status_code == 404, resp.text


def test_forge_sourced_claim_is_unaffected_by_the_board(tmp_path: Path) -> None:
    """An opaque, forge-sourced id still leases without touching the board."""
    client = _make_app(tmp_path)
    worker_id = _enroll(client)

    resp = client.post(
        "/volunteer/tasks/owner-repo-42/claim",
        json={"worker_id": worker_id, "ttl_seconds": TTL},
    )
    assert resp.status_code == 201, resp.text

    listed = client.get("/volunteer/tasks")
    assert listed.json()["tasks"] == []


def test_hub_native_claim_uses_the_board_declared_size(tmp_path: Path) -> None:
    """The board's size wins over the size the claimant asks with."""
    client = _make_app(tmp_path, budget=VolunteerBudget(max_size="xs"))
    worker_id = _enroll(client)

    published = client.post("/volunteer/tasks", json={**TASK_BODY, "task_size": "m"})
    assert published.status_code == 201, published.text
    task_id = published.json()["task_id"]

    resp = client.post(
        f"/volunteer/tasks/{task_id}/claim",
        json={"worker_id": worker_id, "ttl_seconds": TTL, "task_size": "xs"},
    )
    assert resp.status_code == 409, resp.text
    assert "size" in resp.json()["detail"].lower()


def test_publishing_a_task_requires_the_publish_scope(tmp_path: Path) -> None:
    """Publishing is an operator mutation and is not open to a claim token."""
    authenticator = VolunteerAuthenticator(require_auth=True)
    authenticator.add_token("claim-only", (SCOPE_VOLUNTEER_CLAIM,))
    authenticator.add_token("publisher", (SCOPE_VOLUNTEER_PUBLISH,))
    client = _make_app(tmp_path, authenticator=authenticator)

    assert client.post("/volunteer/tasks", json=TASK_BODY).status_code == 401
    with_claim = client.post(
        "/volunteer/tasks",
        json=TASK_BODY,
        headers={"Authorization": "Bearer claim-only"},
    )
    assert with_claim.status_code == 401
    with_publish = client.post(
        "/volunteer/tasks",
        json=TASK_BODY,
        headers={"Authorization": "Bearer publisher"},
    )
    assert with_publish.status_code == 201, with_publish.text


def test_listing_the_board_requires_the_claim_scope(tmp_path: Path) -> None:
    """The board is readable only by a caller entitled to claim from it."""
    authenticator = VolunteerAuthenticator(require_auth=True)
    authenticator.add_token("claimer", (SCOPE_VOLUNTEER_CLAIM,))
    client = _make_app(tmp_path, authenticator=authenticator)

    assert client.get("/volunteer/tasks").status_code == 401
    allowed = client.get("/volunteer/tasks", headers={"Authorization": "Bearer claimer"})
    assert allowed.status_code == 200, allowed.text


def test_publishing_an_unusable_repo_url_is_refused(tmp_path: Path) -> None:
    """A URL git must not be handed is refused before it reaches the board."""
    client = _make_app(tmp_path)
    resp = client.post("/volunteer/tasks", json={**TASK_BODY, "repo_url": "ext::sh -c whoami"})
    assert resp.status_code == 422, resp.text
    assert client.get("/volunteer/tasks").json()["tasks"] == []


def test_publish_and_list_are_absent_without_a_board(tmp_path: Path) -> None:
    """A hub with no board reports the board missing rather than pretending."""
    client = _make_app(tmp_path, with_board=False)
    assert client.post("/volunteer/tasks", json=TASK_BODY).status_code == 404
    assert client.get("/volunteer/tasks").status_code == 404
