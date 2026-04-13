"""Tests for multi-repo orchestration v2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType
from bernstein.core.seed import parse_seed
from bernstein.core.workspace import RepoConfig, Workspace
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import TaskCreate, create_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from bernstein.core.task_store import TaskStore


def _write_seed(tmp_path: Path) -> None:
    (tmp_path / "bernstein.yaml").write_text(
        'goal: "Ship API"\nrepos:\n  - name: backend\n    path: ./backend\n  - name: frontend\n    path: ./frontend\n',
        encoding="utf-8",
    )


@dataclass(frozen=True)
class ClientBundle:
    """Typed holder for the test client and underlying store."""

    client: AsyncClient
    store: TaskStore


@pytest_asyncio.fixture()
async def client_bundle(tmp_path: Path) -> AsyncGenerator[ClientBundle, None]:
    jsonl_path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    app = create_app(jsonl_path=jsonl_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield ClientBundle(client=async_client, store=app.state.store)


class TestMultiRepoConfig:
    def test_seed_parses_top_level_repos_shorthand(self, tmp_path: Path) -> None:
        _write_seed(tmp_path)

        config = parse_seed(tmp_path / "bernstein.yaml")

        assert config.workspace is not None
        assert [repo.name for repo in config.workspace.repos] == ["backend", "frontend"]

    def test_workspace_merge_order_respects_cross_repo_edges(self, tmp_path: Path) -> None:
        workspace = Workspace(
            root=tmp_path,
            repos=[
                RepoConfig(name="backend", path=Path("./backend")),
                RepoConfig(name="frontend", path=Path("./frontend")),
            ],
        )
        backend_task = Task(
            id="T-backend",
            title="Backend task",
            description="Backend work",
            role="backend",
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
            repo="backend",
        )
        frontend_task = Task(
            id="T-frontend",
            title="Frontend task",
            description="Frontend work",
            role="frontend",
            scope=Scope.MEDIUM,
            complexity=Complexity.MEDIUM,
            status=TaskStatus.OPEN,
            task_type=TaskType.STANDARD,
            repo="frontend",
            depends_on=["T-backend"],
            depends_on_repo="backend",
        )

        assert workspace.merge_order([frontend_task, backend_task]) == ["backend", "frontend"]


class TestCrossRepoDependencies:
    @pytest.mark.asyncio
    async def test_task_store_blocks_cross_repo_claim_until_dependency_done(
        self,
        client_bundle: ClientBundle,
    ) -> None:
        store = client_bundle.store

        backend_task = await store.create(
            TaskCreate(
                title="Update backend schema",
                description="Schema task",
                role="backend",
                repo="backend",
            )
        )
        frontend_task = await store.create(
            TaskCreate(
                title="Update frontend client",
                description="Frontend task",
                role="frontend",
                repo="frontend",
                depends_on=[backend_task.id],
                depends_on_repo="backend",
            )
        )

        claimed_before = await store.claim_next("frontend")
        assert claimed_before is None

        claimed_backend = await store.claim_next("backend")
        assert claimed_backend is not None
        assert claimed_backend.id == backend_task.id
        await store.complete(backend_task.id, "done")
        claimed_after = await store.claim_next("frontend")
        assert claimed_after is not None
        assert claimed_after.id == frontend_task.id

    @pytest.mark.asyncio
    async def test_workspace_merge_order_route_uses_current_tasks(
        self,
        client_bundle: ClientBundle,
        tmp_path: Path,
    ) -> None:
        _write_seed(tmp_path)
        store = client_bundle.store

        backend_task = await store.create(
            TaskCreate(
                title="Backend schema",
                description="Schema task",
                role="backend",
                repo="backend",
            )
        )
        await store.create(
            TaskCreate(
                title="Frontend schema consumer",
                description="Frontend task",
                role="frontend",
                repo="frontend",
                depends_on=[backend_task.id],
                depends_on_repo="backend",
            )
        )

        response = await client_bundle.client.post("/workspace/merge-order")

        assert response.status_code == 200
        assert response.json()["repos"] == ["backend", "frontend"]
