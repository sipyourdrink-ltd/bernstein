"""Tests for the volunteer hub FastAPI app.

Covers enrollment, claim, heartbeat, submit, and release endpoints, including
error cases for unauthorized workers, already-leased tasks, non-lease holders,
and unknown tasks.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from bernstein.core.volunteer.hub_app import (
    build_hub_app,
)
from bernstein.core.volunteer.lease_store import LeaseStore

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


T0 = 1_700_000_000.0
TTL = 60


class _FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    return private, public


def _make_app(tmp_path: Path, clock: _FakeClock) -> tuple[TestClient, LeaseStore]:
    lease_store = LeaseStore(tmp_path / "leases.jsonl", clock=clock)
    app = build_hub_app(lease_store)
    client = TestClient(app)
    return client, lease_store


def _enroll_worker(client: TestClient, pubkey: Ed25519PublicKey) -> str:
    pem = pubkey.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    resp = client.post(
        "/volunteer/enroll",
        json={"public_key_pem": pem},
    )
    assert resp.status_code == 201, f"enroll failed: {resp.text}"
    return resp.json()["worker_id"]


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


def test_enroll_returns_pending_worker(tmp_path: Path) -> None:
    """An enrollment returns a worker_id with pending status."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    worker_id = _enroll_worker(client, pubkey)
    assert isinstance(worker_id, str)
    assert len(worker_id) > 0


def test_enroll_idempotent(tmp_path: Path) -> None:
    """Enrolling the same key twice returns the same worker_id."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    first = _enroll_worker(client, pubkey)
    second = _enroll_worker(client, pubkey)
    assert first == second


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


def test_claim_task_succeeds(tmp_path: Path) -> None:
    """A claim from an enrolled worker succeeds."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    worker_id = _enroll_worker(client, pubkey)

    resp = client.post(
        "/volunteer/tasks/t-1/claim",
        json={"worker_id": worker_id, "ttl_seconds": TTL},
    )
    assert resp.status_code == 201, f"claim failed: {resp.text}"
    data = resp.json()
    assert data["task_id"] == "t-1"
    assert data["worker_id"] == worker_id


def test_claim_task_already_leased_to_another_returns_409(tmp_path: Path) -> None:
    """Claiming a task already leased to another worker returns 409."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey1 = _make_keypair()
    _, pubkey2 = _make_keypair()
    worker_id1 = _enroll_worker(client, pubkey1)
    worker_id2 = _enroll_worker(client, pubkey2)

    # First worker claims
    resp1 = client.post(
        "/volunteer/tasks/t-100/claim",
        json={"worker_id": worker_id1, "ttl_seconds": TTL},
    )
    assert resp1.status_code == 201

    # Second worker tries to claim same task
    resp2 = client.post(
        "/volunteer/tasks/t-100/claim",
        json={"worker_id": worker_id2, "ttl_seconds": TTL},
    )
    assert resp2.status_code == 409
    body = resp2.json()
    assert "already leased" in body["detail"].lower() or "leased" in body["detail"].lower()


def test_claim_task_not_enrolled_returns_401(tmp_path: Path) -> None:
    """Claiming with an unknown worker returns 401."""
    client, _ = _make_app(tmp_path, _FakeClock())

    resp = client.post(
        "/volunteer/tasks/t-200/claim",
        json={"worker_id": "unknown-worker", "ttl_seconds": TTL},
    )
    assert resp.status_code == 401


def test_claim_unknown_task_returns_201(tmp_path: Path) -> None:
    """Claiming a task that doesn't exist yet should succeed (creates lease)."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    worker_id = _enroll_worker(client, pubkey)

    resp = client.post(
        "/volunteer/tasks/t-new/claim",
        json={"worker_id": worker_id, "ttl_seconds": TTL},
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_from_lease_holder_succeeds(tmp_path: Path) -> None:
    """A heartbeat from the lease holder extends the lease."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    worker_id = _enroll_worker(client, pubkey)

    # Claim first
    resp = client.post(
        "/volunteer/tasks/t-hb/claim",
        json={"worker_id": worker_id, "ttl_seconds": TTL},
    )
    assert resp.status_code == 201

    # Heartbeat
    resp = client.post(
        "/volunteer/tasks/t-hb/heartbeat",
        json={"worker_id": worker_id},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_id"] == "t-hb"
    assert data["worker_id"] == worker_id


def test_heartbeat_from_non_lease_holder_returns_403(tmp_path: Path) -> None:
    """A heartbeat from a worker that doesn't hold the lease returns 403."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey1 = _make_keypair()
    _, pubkey2 = _make_keypair()
    worker_id1 = _enroll_worker(client, pubkey1)
    worker_id2 = _enroll_worker(client, pubkey2)

    # Worker 1 claims
    client.post(
        "/volunteer/tasks/t-hb2/claim",
        json={"worker_id": worker_id1, "ttl_seconds": TTL},
    )

    # Worker 2 tries to heartbeat
    resp = client.post(
        "/volunteer/tasks/t-hb2/heartbeat",
        json={"worker_id": worker_id2},
    )
    assert resp.status_code == 403


def test_heartbeat_on_unknown_task_returns_404(tmp_path: Path) -> None:
    """A heartbeat on a task with no lease returns 404."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    worker_id = _enroll_worker(client, pubkey)

    resp = client.post(
        "/volunteer/tasks/t-noexist/heartbeat",
        json={"worker_id": worker_id},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def test_submit_by_lease_holder_succeeds(tmp_path: Path) -> None:
    """A submit from the lease holder records the result."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    worker_id = _enroll_worker(client, pubkey)

    # Claim first
    client.post(
        "/volunteer/tasks/t-sub/claim",
        json={"worker_id": worker_id, "ttl_seconds": TTL},
    )

    # Submit
    resp = client.post(
        "/volunteer/tasks/t-sub/submit",
        json={
            "worker_id": worker_id,
            "bundle_digest": "sha256:abc123",
            "location": "https://example.com/bundle.tar.gz",
        },
    )
    assert resp.status_code == 200, f"submit failed: {resp.text}"
    data = resp.json()
    assert data["task_id"] == "t-sub"
    assert data["submission"]["bundle_digest"] == "sha256:abc123"


def test_submit_by_non_lease_holder_returns_403(tmp_path: Path) -> None:
    """A submit from a worker that doesn't hold the lease returns 403."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey1 = _make_keypair()
    _, pubkey2 = _make_keypair()
    worker_id1 = _enroll_worker(client, pubkey1)
    worker_id2 = _enroll_worker(client, pubkey2)

    # Worker 1 claims
    client.post(
        "/volunteer/tasks/t-sub2/claim",
        json={"worker_id": worker_id1, "ttl_seconds": TTL},
    )

    # Worker 2 tries to submit
    resp = client.post(
        "/volunteer/tasks/t-sub2/submit",
        json={
            "worker_id": worker_id2,
            "bundle_digest": "sha256:def456",
            "location": "https://example.com/other.tar.gz",
        },
    )
    assert resp.status_code == 403


def test_submit_twice_returns_409(tmp_path: Path) -> None:
    """Submitting twice on the same lease returns 409."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    worker_id = _enroll_worker(client, pubkey)

    # Claim
    client.post(
        "/volunteer/tasks/t-sub3/claim",
        json={"worker_id": worker_id, "ttl_seconds": TTL},
    )

    # First submit
    resp1 = client.post(
        "/volunteer/tasks/t-sub3/submit",
        json={
            "worker_id": worker_id,
            "bundle_digest": "sha256:first",
            "location": "https://example.com/first.tar.gz",
        },
    )
    assert resp1.status_code == 200

    # Second submit
    resp2 = client.post(
        "/volunteer/tasks/t-sub3/submit",
        json={
            "worker_id": worker_id,
            "bundle_digest": "sha256:second",
            "location": "https://example.com/second.tar.gz",
        },
    )
    assert resp2.status_code == 409


def test_submit_on_unknown_task_returns_404(tmp_path: Path) -> None:
    """A submit on a task with no lease returns 404."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    worker_id = _enroll_worker(client, pubkey)

    resp = client.post(
        "/volunteer/tasks/t-noexist2/submit",
        json={
            "worker_id": worker_id,
            "bundle_digest": "sha256:abc",
            "location": "https://example.com/x.tar.gz",
        },
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


def test_release_by_lease_holder_returns_204_and_frees_task(tmp_path: Path) -> None:
    """A release by the lease holder returns 204 and makes the task available."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    worker_id = _enroll_worker(client, pubkey)

    # Claim
    client.post(
        "/volunteer/tasks/t-rel/claim",
        json={"worker_id": worker_id, "ttl_seconds": TTL},
    )

    # Release
    resp = client.post(
        "/volunteer/tasks/t-rel/release",
        json={"worker_id": worker_id},
    )
    assert resp.status_code == 204

    # Now another worker should be able to claim it
    _, pubkey2 = _make_keypair()
    worker_id2 = _enroll_worker(client, pubkey2)
    resp2 = client.post(
        "/volunteer/tasks/t-rel/claim",
        json={"worker_id": worker_id2, "ttl_seconds": TTL},
    )
    assert resp2.status_code == 201


def test_release_by_non_lease_holder_returns_403(tmp_path: Path) -> None:
    """A release by a worker that doesn't hold the lease returns 403."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey1 = _make_keypair()
    _, pubkey2 = _make_keypair()
    worker_id1 = _enroll_worker(client, pubkey1)
    worker_id2 = _enroll_worker(client, pubkey2)

    # Worker 1 claims
    client.post(
        "/volunteer/tasks/t-rel2/claim",
        json={"worker_id": worker_id1, "ttl_seconds": TTL},
    )

    # Worker 2 tries to release
    resp = client.post(
        "/volunteer/tasks/t-rel2/release",
        json={"worker_id": worker_id2},
    )
    assert resp.status_code == 403


def test_release_on_unknown_task_returns_404(tmp_path: Path) -> None:
    """A release on a task with no lease returns 404."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    worker_id = _enroll_worker(client, pubkey)

    resp = client.post(
        "/volunteer/tasks/t-noexist3/release",
        json={"worker_id": worker_id},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth verification
# ---------------------------------------------------------------------------


def test_enroll_without_auth_works(tmp_path: Path) -> None:
    """Enrollment is open (no auth required for self-enrollment)."""
    client, _ = _make_app(tmp_path, _FakeClock())
    _, pubkey = _make_keypair()
    pem = pubkey.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    resp = client.post(
        "/volunteer/enroll",
        json={"public_key_pem": pem},
    )
    assert resp.status_code == 201


def test_healthz_returns_ok(tmp_path: Path) -> None:
    """The healthz endpoint returns ok."""
    client, _ = _make_app(tmp_path, _FakeClock())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
