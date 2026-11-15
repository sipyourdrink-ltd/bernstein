"""Operator authority and mirrors without accessing real installation configuration."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from bernstein.core.task_store import TaskStore
from fastapi import FastAPI

from bernstein.core.routes.claim_admission import router
from bernstein.core.routes.task_claim_receipt import router as receipt_router
from bernstein.core.security.auth import AuthRole, AuthUser
from bernstein.core.security.auth_middleware import _get_required_permission


def _app(tmp_path: Path, identity: str) -> FastAPI:
    directory = tmp_path / "control"
    directory.mkdir(mode=0o700)
    app = FastAPI()
    app.state.store = TaskStore(
        tmp_path / "tasks.jsonl", claim_control_path=directory / "state.sqlite", claim_controller_id="installation-a"
    )
    app.state.draining = False
    app.include_router(router)
    app.include_router(router, prefix="/api/v1")
    app.include_router(receipt_router)

    @app.middleware("http")
    async def authenticated_context(request, call_next):
        if identity == "legacy":
            request.state.auth_claims = {"legacy": True}
        elif identity == "cluster":
            request.state.auth_claims = {"cluster": True}
        elif identity == "agent":
            request.state.agent_identity = SimpleNamespace()
            request.state.auth_claims = {"legacy": True}
        elif identity in ("admin", "viewer"):
            request.state.auth_claims = {"sub": identity}
            request.state.user = AuthUser(
                id=identity,
                email="fixture@example.invalid",
                display_name="Fixture",
                role=AuthRole.ADMIN if identity == "admin" else AuthRole.VIEWER,
            )
        return await call_next(request)

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["none", "cluster", "agent", "viewer"])
@pytest.mark.parametrize("prefix", ["", "/api/v1"])
async def test_untrusted_principals_cannot_enroll_or_resume(tmp_path: Path, identity: str, prefix: str):
    app = _app(tmp_path, identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        for action in ("enroll", "halt", "resume"):
            response = await client.post(
                f"{prefix}/claim-admission/{action}", json={"reason": "Try", "expected_revision": 0}
            )
            assert response.status_code == 403
    assert not (tmp_path / "control" / "state.sqlite").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["legacy", "admin"])
@pytest.mark.parametrize("prefix", ["", "/api/v1"])
async def test_operator_context_controls_only_configured_scope(tmp_path: Path, identity: str, prefix: str):
    app = _app(tmp_path, identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        for revision, (action, halted) in enumerate([("enroll", True), ("resume", False), ("halt", True)]):
            response = await client.post(
                f"{prefix}/claim-admission/{action}", json={"reason": "Operator reason", "expected_revision": revision}
            )
            assert response.status_code == 200, response.text
            assert response.json()["halted"] is halted
            assert response.json()["controller_id"] == "installation-a"
        response = await client.post(
            f"{prefix}/claim-admission/resume",
            json={"reason": "Try", "expected_revision": 3, "actor": "forged", "controller_id": "other"},
        )
        assert response.status_code == 422
        assert (
            await client.post(f"{prefix}/claim-admission/resume", json={"reason": "  ", "expected_revision": 3})
        ).status_code == 422


@pytest.mark.parametrize("prefix", ["", "/api/v1"])
@pytest.mark.parametrize("action", ["enroll", "halt", "resume"])
def test_middleware_requires_operator_permission(prefix: str, action: str):
    assert _get_required_permission(f"{prefix}/claim-admission/{action}", "POST") == "admin:manage"
    assert _get_required_permission(f"{prefix}/claim-admission", "GET") == "admin:manage"


@pytest.mark.asyncio
async def test_legacy_backlog_endpoint_refuses_before_any_mutation(tmp_path: Path, monkeypatch):
    app = _app(tmp_path, "legacy")
    legacy_claim = AsyncMock(side_effect=AssertionError("legacy path must not run"))
    monkeypatch.setattr("bernstein.core.routes.task_claim_receipt.claim_next_entry", legacy_claim)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        response = await client.post("/tasks/claim-receipt", json={"claimer_id": "worker"})
    assert response.status_code == 503, response.text
    legacy_claim.assert_not_called()


@pytest.mark.asyncio
async def test_production_factory_halt_controls_real_http_claim_routes(tmp_path: Path, monkeypatch):
    import os

    from bernstein.core.server import create_app

    home = tmp_path / "fixture-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in tuple(os.environ):
        if name.startswith("BERNSTEIN_"):
            monkeypatch.delenv(name)
    directory = tmp_path / "control"
    directory.mkdir(mode=0o700)
    app = create_app(
        jsonl_path=tmp_path / "project" / ".sdd" / "runtime" / "tasks.jsonl",
        auth_token="fixture-operator",
        claim_control_path=directory / "state.sqlite",
        claim_controller_id="installation-http",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={"Authorization": "Bearer fixture-operator"},
    ) as client:
        created = await client.post(
            "/tasks",
            json={
                "title": "Work",
                "description": "Fixture",
                "role": "backend",
                "priority": 1,
                "scope": "medium",
                "complexity": "medium",
            },
        )
        assert created.status_code in (200, 201), created.text
        task_id = created.json()["id"]
        assert (
            await client.post("/claim-admission/enroll", json={"reason": "Enroll", "expected_revision": 0})
        ).status_code == 200
        assert (
            await client.post("/claim-admission/resume", json={"reason": "Go", "expected_revision": 1})
        ).status_code == 200
        assert (
            await client.post("/api/v1/claim-admission/halt", json={"reason": "Incident", "expected_revision": 2})
        ).status_code == 200
        before = app.state.store.get_task(task_id).version
        requests = [
            ("GET", "/tasks/next/backend", None),
            ("POST", f"/tasks/{task_id}/claim", None),
            ("POST", "/tasks/claim-batch", {"task_ids": [task_id], "agent_id": "fixture-worker"}),
            ("POST", "/tasks/claim-receipt", {"claimer_id": "fixture-worker"}),
        ]
        for method, path, payload in requests:
            response = await client.request(method, path, json=payload)
            assert response.status_code == 503, (path, response.text)
        assert (await client.post("/drain/cancel")).status_code == 200
        assert (await client.get("/tasks/next/backend")).status_code == 503
        assert app.state.store.get_task(task_id).status.value == "open"
        assert app.state.store.get_task(task_id).version == before
        assert (
            await client.post("/claim-admission/resume", json={"reason": "Release", "expected_revision": 3})
        ).status_code == 200
        assert (await client.get("/tasks/next/backend")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/api/v1"])
@pytest.mark.parametrize("role", [AuthRole.ADMIN, AuthRole.OPERATOR, AuthRole.VIEWER])
async def test_real_sso_middleware_preserves_operator_revision_workflow(
    tmp_path: Path, prefix: str, role: AuthRole, monkeypatch
):
    from bernstein.core.security.auth_middleware import SSOAuthMiddleware

    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    app = _app(tmp_path, "none")
    user = AuthUser(id="validated-user", email="fixture@example.invalid", display_name="Fixture", role=role)
    service = SimpleNamespace(validate_token=lambda token, **kwargs: (user, {"sub": user.id, "tenant_id": "default"}))
    app.add_middleware(SSOAuthMiddleware, auth_service=service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={"Authorization": "Bearer fixture-validated-sso"},
    ) as client:
        enrolled = await client.post(
            f"{prefix}/claim-admission/enroll", json={"reason": "Enroll", "expected_revision": 0}
        )
        status = await client.get(f"{prefix}/claim-admission")
        if role != AuthRole.ADMIN:
            assert enrolled.status_code == status.status_code == 403
            assert not (tmp_path / "control" / "state.sqlite").exists()
            return
        assert enrolled.status_code == 200, enrolled.text
        assert status.status_code == 200, status.text
        assert status.json()["revision"] == 1
        resumed = await client.post(
            f"{prefix}/claim-admission/resume",
            json={"reason": "Release", "expected_revision": status.json()["revision"]},
        )
        assert resumed.status_code == 200
        assert resumed.json()["actor"] == "operator:validated-user"
        assert (await client.get(f"{prefix}/claim-admission")).json()["revision"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "/api/v1"])
async def test_real_auth_disabled_middleware_cannot_create_operator_authority(tmp_path: Path, prefix: str):
    from bernstein.core.security.auth_middleware import SSOAuthMiddleware

    app = _app(tmp_path, "none")
    app.add_middleware(SSOAuthMiddleware, auth_disabled=True, legacy_token="fixture-operator")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://fixture",
        headers={"Authorization": "Bearer fixture-operator"},
    ) as client:
        response = await client.post(f"{prefix}/claim-admission/enroll", json={"reason": "Try", "expected_revision": 0})
        assert response.status_code == 403
    assert not (tmp_path / "control" / "state.sqlite").exists()
