"""Bare-task receipt claims keep authenticated tenant boundaries."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from bernstein.core.tasks.claim import Backlog, BacklogEntry
from tests.unit.tenant_scope.conftest import Fixture, _credential

pytestmark = [pytest.mark.anyio, pytest.mark.auth_enabled]


@pytest.mark.parametrize("credential_name", ["agent_task_scoped", "sso_operator"])
async def test_bare_claim_never_takes_another_tenants_task_or_legacy_backlog(
    fx: Fixture, client: AsyncClient, credential_name: str
) -> None:
    credential = _credential(fx, credential_name)
    path = fx.app.state.runtime_dir / "task-backlog.json"
    Backlog.write(path, [BacklogEntry(id="legacy-private", role="backend")])
    response = await client.post(
        "/api/v1/tasks/claim-receipt",
        headers=credential.headers,
        json={"claimer_id": "worker", "role": "backend"},
    )
    assert response.status_code == 200, response.text
    wire = response.json()
    assert wire["taskId"] == fx.task_a_id
    assert fx.task_b_id not in response.text
    assert fx.task_default_id not in response.text
    assert "legacy-private" not in response.text
    assert Backlog.load(path).entries[0].status == "open"
    crossed = await client.post(
        "/tasks/claim-receipt",
        headers=credential.crossing_headers(),
        json={"claimer_id": "worker", "role": "backend"},
    )
    assert crossed.status_code == 403


async def test_empty_scope_worker_gets_no_task_or_snapshot(fx: Fixture, client: AsyncClient) -> None:
    credential = _credential(fx, "agent_unrestricted")
    response = await client.post(
        "/tasks/claim-receipt",
        headers=credential.headers,
        json={"claimer_id": "worker", "role": "backend"},
    )
    assert response.status_code == 200
    assert response.json()["granted"] is False
    assert response.json()["backlogSnapshot"] == []
