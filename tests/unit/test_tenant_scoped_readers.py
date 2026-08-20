"""Tenant scoping for readers that used to list the whole task table.

Each call site gets the same pair: a caller scoped to tenant A must see a figure
that does not move when tenant B's tasks change, plus a positive control proving
A's own tasks still count (so the scoping cannot pass by returning nothing).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app
from bernstein.core.server.server_models import TaskCreate


def _write_seed(tmp_path: Path) -> None:
    (tmp_path / "bernstein.yaml").write_text(
        'goal: "Ship multi-tenant platform"\n'
        "tenants:\n"
        "  - id: team-a\n"
        "    budget: 100\n"
        "  - id: team-b\n"
        "    budget: 250\n",
        encoding="utf-8",
    )


@pytest.fixture()
def app(tmp_path: Path) -> FastAPI:
    _write_seed(tmp_path)
    application = create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
    application.state.reload_seed_config()
    return application


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


async def _create(app: FastAPI, title: str, tenant_id: str, **extra: object) -> None:
    desc = extra.pop("description", "scoping fixture")
    await app.state.store.create(TaskCreate(title=title, description=desc, tenant_id=tenant_id, **extra))


class TestBudgetForecastTenantScope:
    @pytest.mark.asyncio
    async def test_forecast_ignores_another_tenants_tasks(self, app: FastAPI, client: AsyncClient) -> None:
        baseline = (await client.get("/quality/budget-forecast", headers={"X-Tenant-Id": "team-a"})).json()

        await _create(app, "team-b work", "team-b")

        after = await client.get("/quality/budget-forecast", headers={"X-Tenant-Id": "team-a"})
        assert after.status_code == 200
        assert after.json()["task_count"] == baseline["task_count"]

    @pytest.mark.asyncio
    async def test_forecast_still_counts_own_tasks(self, app: FastAPI, client: AsyncClient) -> None:
        baseline = (await client.get("/quality/budget-forecast", headers={"X-Tenant-Id": "team-a"})).json()[
            "task_count"
        ]

        await _create(app, "team-a work", "team-a")

        after = await client.get("/quality/budget-forecast", headers={"X-Tenant-Id": "team-a"})
        assert after.json()["task_count"] == baseline + 1


class TestCiFixCounterTenantScope:
    @pytest.mark.asyncio
    async def test_ci_fix_count_ignores_another_tenants_tasks(self, app: FastAPI) -> None:
        from bernstein.core.routes.webhooks import _count_ci_fix_attempts

        branch = "main"
        # Team B has an active ci-fix task
        await _create(app, "[ci-fix] team-b failure", "team-b", description=f"branch: {branch}")

        # Team A's count should be 0 (unaffected by team B)
        count_a = _count_ci_fix_attempts(app.state.store, branch, "team-a")
        assert count_a == 0

    @pytest.mark.asyncio
    async def test_ci_fix_count_still_counts_own_tasks(self, app: FastAPI) -> None:
        from bernstein.core.routes.webhooks import _count_ci_fix_attempts

        branch = "main"
        # Team A has an active ci-fix task
        await _create(app, "[ci-fix] team-a failure", "team-a", description=f"branch: {branch}")

        # Team A's count should be 1
        count_a = _count_ci_fix_attempts(app.state.store, branch, "team-a")
        assert count_a == 1


class TestGitlabCiFixCounterTenantScope:
    @pytest.mark.asyncio
    async def test_gitlab_ci_fix_count_ignores_another_tenants_tasks(self, app: FastAPI) -> None:
        from bernstein.core.routes.webhooks import _count_gitlab_ci_fix_attempts

        ref = "main"
        # Team B has an active ci-fix task
        await _create(app, "[ci-fix] team-b gitlab failure", "team-b", description=f"ref: {ref}")

        # Team A's count should be 0 (unaffected by team B)
        count_a = _count_gitlab_ci_fix_attempts(app.state.store, ref, "team-a")
        assert count_a == 0

    @pytest.mark.asyncio
    async def test_gitlab_ci_fix_count_still_counts_own_tasks(self, app: FastAPI) -> None:
        from bernstein.core.routes.webhooks import _count_gitlab_ci_fix_attempts

        ref = "main"
        # Team A has an active ci-fix task
        await _create(app, "[ci-fix] team-a gitlab failure", "team-a", description=f"ref: {ref}")

        # Team A's count should be 1
        count_a = _count_gitlab_ci_fix_attempts(app.state.store, ref, "team-a")
        assert count_a == 1


class TestHealthCheckTenantScope:
    @pytest.mark.asyncio
    async def test_health_ignores_another_tenants_tasks(self, app: FastAPI, client: AsyncClient) -> None:
        baseline = (await client.get("/health", headers={"X-Tenant-Id": "team-a"})).json()

        await _create(app, "team-b work", "team-b")

        after = await client.get("/health", headers={"X-Tenant-Id": "team-a"})
        assert after.status_code == 200
        assert after.json()["task_count"] == baseline["task_count"]

    @pytest.mark.asyncio
    async def test_health_still_counts_own_tasks(self, app: FastAPI, client: AsyncClient) -> None:
        baseline = (await client.get("/health", headers={"X-Tenant-Id": "team-a"})).json()["task_count"]

        await _create(app, "team-a work", "team-a")

        after = await client.get("/health", headers={"X-Tenant-Id": "team-a"})
        assert after.json()["task_count"] == baseline + 1
