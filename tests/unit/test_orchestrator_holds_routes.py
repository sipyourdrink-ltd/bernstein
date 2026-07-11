"""Tests for the orchestrator hold/release routes (/orchestrator/holds).

Regression coverage for router registration: the holds feature only works if
the ``orchestrator_holds`` router is actually mounted on the server app -
``fetch_active_holds`` treats any error (including a 404 from an unmounted
router) as "no active holds", so a missing registration silently disables
the whole feature instead of failing loudly.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setitem(os.environ, "BERNSTEIN_AUTH_DISABLED", "1")
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    return TestClient(app)


def test_holds_router_is_mounted(client: TestClient) -> None:
    """GET /orchestrator/holds must be a real route, not a 404."""
    resp = client.get("/orchestrator/holds")
    assert resp.status_code == 200
    body = resp.json()
    # The registry is a module-level singleton shared across app instances,
    # so assert shape rather than exact emptiness.
    assert body["count"] == len(body["holds"])


def test_holds_router_is_mounted_under_api_v1(client: TestClient) -> None:
    resp = client.get("/api/v1/orchestrator/holds")
    assert resp.status_code == 200


def test_hold_lifecycle_acquire_renew_release(client: TestClient) -> None:
    resp = client.post("/orchestrator/holds", json={"reason": "test hold", "ttl_seconds": 30})
    assert resp.status_code == 200
    hold = resp.json()
    assert hold["reason"] == "test hold"
    assert hold["ttl_seconds"] == 30.0
    assert hold["last_renewed_at"] is None
    hold_id = hold["id"]

    listing = client.get("/orchestrator/holds").json()
    assert listing["count"] == 1
    assert listing["holds"][0]["id"] == hold_id

    renewed = client.post(f"/orchestrator/holds/{hold_id}/renew")
    assert renewed.status_code == 200
    assert renewed.json()["last_renewed_at"] is not None
    assert renewed.json()["expires_at"] >= hold["expires_at"]

    released = client.delete(f"/orchestrator/holds/{hold_id}")
    assert released.status_code == 200
    assert released.json() == {"released": True}

    assert client.delete(f"/orchestrator/holds/{hold_id}").status_code == 404
    assert client.post(f"/orchestrator/holds/{hold_id}/renew").status_code == 404
    assert client.get("/orchestrator/holds").json()["count"] == 0


def test_hold_create_rejects_unknown_fields(client: TestClient) -> None:
    """extra="forbid" - a misspelled TTL field must 422, not silently default."""
    resp = client.post("/orchestrator/holds", json={"reason": "x", "ttl_s": 5})
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_ttl", ["-5", "0", "NaN", "Infinity"])
def test_hold_create_rejects_non_positive_or_non_finite_ttl(client: TestClient, bad_ttl: str) -> None:
    """A NaN TTL makes expires_at NaN, so the hold would never expire or purge.

    Negative/zero TTLs reject with a clean 422. Non-finite TTLs are also
    rejected before any hold is created, but FastAPI cannot serialise the
    offending float back into the 422 body, so the crash guard converts
    those to a 500 - either way the request must fail and no hold may be
    registered.
    """
    before = client.get("/orchestrator/holds").json()["count"]
    resp = client.post(
        "/orchestrator/holds",
        content=f'{{"reason": "x", "ttl_seconds": {bad_ttl}}}',
        headers={"content-type": "application/json"},
    )
    assert resp.status_code >= 400, (bad_ttl, resp.status_code, resp.text)
    assert client.get("/orchestrator/holds").json()["count"] == before
