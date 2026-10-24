"""HTTP cancellation mode integration and ownership release coverage."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.server import create_app
from bernstein.core.tasks import lifecycle


@pytest.fixture
def plain_serve_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.delenv("BERNSTEIN_AUDIT", raising=False)
    monkeypatch.setattr(lifecycle, "_audit_log", None)
    app = create_app(jsonl_path=tmp_path / "runtime" / "tasks.jsonl")
    app.state.draining = False
    return app


def _create_and_claim(client: TestClient, *, claim: bool = True) -> str:
    created = client.post("/tasks", json={"title": "stop me", "description": "d", "role": "backend"})
    assert created.status_code == 201, created.text
    task_id = str(created.json()["id"])
    if claim:
        response = client.post(f"/tasks/{task_id}/claim", params={"claimed_by_session": "stop-holder"})
        assert response.status_code == 200, response.text
    return task_id


def test_request_confirm_and_release_receipt(plain_serve_app: FastAPI) -> None:
    with TestClient(plain_serve_app) as client:
        task_id = _create_and_claim(client)
        url = f"/tasks/{task_id}/cancel"
        response = client.post(url, params={"stop_mode": "request"}, json={"reason": "stop"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "stop_req"
        for mode in ("request", "immediate"):
            assert client.post(url, params={"stop_mode": mode}, json={"reason": "again"}).status_code == 409
        assert client.get(f"/tasks/{task_id}").json()["status"] == "stop_req"
        assert client.post(f"/tasks/{task_id}/complete", json={"result_summary": "done"}).status_code == 409
        counts = client.get("/tasks/counts")
        assert counts.status_code == 200, counts.text
        assert counts.json()["stop_req"] == 1
        assert counts.json()["total"] == 1
        assert plain_serve_app.state.audit_chain.query(event_type="task.release_receipt") == []
        confirmed = client.post(url, params={"stop_mode": "confirm"}, json={"reason": "stopped"})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "cancelled"
    receipts = plain_serve_app.state.audit_chain.query(event_type="task.release_receipt")
    assert len(receipts) == 1
    assert receipts[0].details["release_path"] == "stop_confirmed"
    assert plain_serve_app.state.audit_chain.verify() == (True, [])


@pytest.mark.parametrize(("claim", "mode"), [(False, "request"), (True, None)])
def test_unowned_request_and_legacy_default_cancel(plain_serve_app: FastAPI, claim: bool, mode: str | None) -> None:
    with TestClient(plain_serve_app) as client:
        task_id = _create_and_claim(client, claim=claim)
        response = client.post(
            f"/tasks/{task_id}/cancel",
            params={"stop_mode": mode} if mode else {},
            json={"reason": "stop"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "cancelled"


def test_confirm_requires_pending_stop_and_modes_are_validated(plain_serve_app: FastAPI) -> None:
    with TestClient(plain_serve_app) as client:
        task_id = _create_and_claim(client, claim=False)
        url = f"/tasks/{task_id}/cancel"
        assert client.post(url, params={"stop_mode": "confirm"}, json={"reason": "stop"}).status_code == 409
        assert client.post(url, params={"stop_mode": "unknown"}, json={"reason": "stop"}).status_code == 422
        assert client.get(f"/tasks/{task_id}").json()["status"] == "open"


@pytest.mark.auth_enabled
@pytest.mark.parametrize("mode", ["request", "confirm"])
def test_modes_preserve_task_scope(plain_serve_app: FastAPI, mode: str) -> None:
    _, token = plain_serve_app.state.identity_store.create_identity(
        "other-session", "backend", task_ids=["another-task"]
    )
    with TestClient(plain_serve_app) as client:
        response = client.post(
            "/tasks/victim/cancel",
            params={"stop_mode": mode},
            headers={"Authorization": f"Bearer {token}"},
            json={"reason": "stop"},
        )
        assert response.status_code == 403, response.text
