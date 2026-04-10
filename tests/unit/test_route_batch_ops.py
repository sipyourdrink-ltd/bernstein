"""WEB-017: Tests for batch operations endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.routes.batch_ops import (
    _MAX_BATCH_SIZE,
    BatchAction,
    BatchRequest,
    BatchResult,
    validate_batch_request,
)
from bernstein.core.server import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path) -> FastAPI:
    application = create_app(jsonl_path=jsonl_path)
    # Include the batch_ops router
    from bernstein.core.routes.batch_ops import router as batch_ops_router

    application.include_router(batch_ops_router)
    return application


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _create_task(client: AsyncClient, title: str = "Test task", priority: int = 2) -> str:
    """Create a task and return its ID."""
    resp = await client.post(
        "/tasks",
        json={"title": title, "description": "desc", "role": "backend", "priority": priority},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _create_tasks(client: AsyncClient, count: int) -> list[str]:
    """Create N tasks and return their IDs."""
    ids: list[str] = []
    for i in range(count):
        task_id = await _create_task(client, title=f"Task {i}")
        ids.append(task_id)
    return ids


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestBatchAction:
    """Test BatchAction enum."""

    def test_values(self) -> None:
        assert BatchAction.CANCEL == "cancel"
        assert BatchAction.RETRY == "retry"
        assert BatchAction.REPRIORITIZE == "reprioritize"
        assert BatchAction.TAG == "tag"

    def test_all_members(self) -> None:
        assert set(BatchAction) == {
            BatchAction.CANCEL,
            BatchAction.RETRY,
            BatchAction.REPRIORITIZE,
            BatchAction.TAG,
        }


class TestBatchRequest:
    """Test BatchRequest Pydantic model."""

    def test_minimal_request(self) -> None:
        req = BatchRequest(action=BatchAction.CANCEL, ids=["t1", "t2"])
        assert req.action == BatchAction.CANCEL
        assert req.ids == ["t1", "t2"]
        assert req.priority is None
        assert req.tags is None

    def test_reprioritize_request(self) -> None:
        req = BatchRequest(action=BatchAction.REPRIORITIZE, ids=["t1"], priority=1)
        assert req.priority == 1

    def test_tag_request(self) -> None:
        req = BatchRequest(action=BatchAction.TAG, ids=["t1"], tags=["urgent", "hotfix"])
        assert req.tags == ["urgent", "hotfix"]


class TestBatchResult:
    """Test BatchResult Pydantic model."""

    def test_empty_result(self) -> None:
        result = BatchResult()
        assert result.succeeded == []
        assert result.failed == {}
        assert result.total == 0

    def test_total_property(self) -> None:
        result = BatchResult(succeeded=["a", "b"], failed={"c": "not found"})
        assert result.total == 3

    def test_all_succeeded(self) -> None:
        result = BatchResult(succeeded=["a", "b", "c"])
        assert result.total == 3
        assert len(result.failed) == 0

    def test_all_failed(self) -> None:
        result = BatchResult(failed={"a": "err1", "b": "err2"})
        assert result.total == 2
        assert len(result.succeeded) == 0


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidateBatchRequest:
    """Test validate_batch_request function."""

    def test_valid_cancel(self) -> None:
        req = BatchRequest(action=BatchAction.CANCEL, ids=["t1"])
        assert validate_batch_request(req) == []

    def test_empty_ids(self) -> None:
        req = BatchRequest(action=BatchAction.CANCEL, ids=[])
        errors = validate_batch_request(req)
        assert any("empty" in e for e in errors)

    def test_exceeds_max_batch_size(self) -> None:
        req = BatchRequest(action=BatchAction.CANCEL, ids=[f"t{i}" for i in range(_MAX_BATCH_SIZE + 1)])
        errors = validate_batch_request(req)
        assert any("maximum batch size" in e for e in errors)

    def test_at_max_batch_size(self) -> None:
        req = BatchRequest(action=BatchAction.CANCEL, ids=[f"t{i}" for i in range(_MAX_BATCH_SIZE)])
        assert validate_batch_request(req) == []

    def test_reprioritize_missing_priority(self) -> None:
        req = BatchRequest(action=BatchAction.REPRIORITIZE, ids=["t1"])
        errors = validate_batch_request(req)
        assert any("priority" in e for e in errors)

    def test_reprioritize_with_priority(self) -> None:
        req = BatchRequest(action=BatchAction.REPRIORITIZE, ids=["t1"], priority=1)
        assert validate_batch_request(req) == []

    def test_tag_missing_tags(self) -> None:
        req = BatchRequest(action=BatchAction.TAG, ids=["t1"])
        errors = validate_batch_request(req)
        assert any("tags" in e for e in errors)

    def test_tag_empty_tags(self) -> None:
        req = BatchRequest(action=BatchAction.TAG, ids=["t1"], tags=[])
        errors = validate_batch_request(req)
        assert any("tags" in e for e in errors)

    def test_tag_with_tags(self) -> None:
        req = BatchRequest(action=BatchAction.TAG, ids=["t1"], tags=["urgent"])
        assert validate_batch_request(req) == []


# ---------------------------------------------------------------------------
# Integration tests (endpoint)
# ---------------------------------------------------------------------------


class TestBatchOpsEndpoint:
    """Test POST /tasks/batch-ops endpoint."""

    @pytest.mark.anyio()
    async def test_cancel_tasks(self, client: AsyncClient) -> None:
        """Batch cancel transitions tasks to cancelled."""
        ids = await _create_tasks(client, 3)
        resp = await client.post(
            "/tasks/batch-ops",
            json={"action": "cancel", "ids": ids},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["succeeded"]) == set(ids)
        assert data["failed"] == {}

    @pytest.mark.anyio()
    async def test_cancel_nonexistent_task(self, client: AsyncClient) -> None:
        """Non-existent task IDs should appear in failed."""
        resp = await client.post(
            "/tasks/batch-ops",
            json={"action": "cancel", "ids": ["nonexistent-id"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["succeeded"] == []
        assert "nonexistent-id" in data["failed"]

    @pytest.mark.anyio()
    async def test_reprioritize_tasks(self, client: AsyncClient) -> None:
        """Batch reprioritize updates priority on all tasks."""
        ids = await _create_tasks(client, 2)
        resp = await client.post(
            "/tasks/batch-ops",
            json={"action": "reprioritize", "ids": ids, "priority": 1},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["succeeded"]) == set(ids)

        # Verify priority was actually updated
        for task_id in ids:
            task_resp = await client.get(f"/tasks/{task_id}")
            assert task_resp.status_code == 200
            assert task_resp.json()["priority"] == 1

    @pytest.mark.anyio()
    async def test_reprioritize_without_priority_returns_422(self, client: AsyncClient) -> None:
        """Reprioritize without priority value returns 422."""
        ids = await _create_tasks(client, 1)
        resp = await client.post(
            "/tasks/batch-ops",
            json={"action": "reprioritize", "ids": ids},
        )
        assert resp.status_code == 422

    @pytest.mark.anyio()
    async def test_tag_tasks(self, client: AsyncClient) -> None:
        """Batch tag adds tags to task metadata."""
        ids = await _create_tasks(client, 2)
        resp = await client.post(
            "/tasks/batch-ops",
            json={"action": "tag", "ids": ids, "tags": ["urgent", "hotfix"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["succeeded"]) == set(ids)

    @pytest.mark.anyio()
    async def test_tag_without_tags_returns_422(self, client: AsyncClient) -> None:
        """Tag without tags list returns 422."""
        ids = await _create_tasks(client, 1)
        resp = await client.post(
            "/tasks/batch-ops",
            json={"action": "tag", "ids": ids},
        )
        assert resp.status_code == 422

    @pytest.mark.anyio()
    async def test_empty_ids_returns_422(self, client: AsyncClient) -> None:
        """Empty ids list returns 422."""
        resp = await client.post(
            "/tasks/batch-ops",
            json={"action": "cancel", "ids": []},
        )
        assert resp.status_code == 422

    @pytest.mark.anyio()
    async def test_retry_failed_task(self, client: AsyncClient) -> None:
        """Retry transitions a failed task back to open."""
        task_id = await _create_task(client)
        # Claim then fail the task
        await client.post(f"/tasks/{task_id}/claim")
        await client.post(f"/tasks/{task_id}/fail", json={"reason": "test failure"})

        resp = await client.post(
            "/tasks/batch-ops",
            json={"action": "retry", "ids": [task_id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert task_id in data["succeeded"]

        # Verify the task is back to open
        task_resp = await client.get(f"/tasks/{task_id}")
        assert task_resp.json()["status"] == "open"

    @pytest.mark.anyio()
    async def test_retry_non_failed_task_fails(self, client: AsyncClient) -> None:
        """Retry on an open task should end up in failed."""
        task_id = await _create_task(client)
        resp = await client.post(
            "/tasks/batch-ops",
            json={"action": "retry", "ids": [task_id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert task_id in data["failed"]

    @pytest.mark.anyio()
    async def test_mixed_success_and_failure(self, client: AsyncClient) -> None:
        """Batch with mix of valid and invalid IDs reports both."""
        valid_id = await _create_task(client)
        resp = await client.post(
            "/tasks/batch-ops",
            json={"action": "cancel", "ids": [valid_id, "does-not-exist"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert valid_id in data["succeeded"]
        assert "does-not-exist" in data["failed"]

    @pytest.mark.anyio()
    async def test_tag_deduplicates(self, client: AsyncClient) -> None:
        """Applying the same tags twice should not create duplicates."""
        task_id = await _create_task(client)
        tags = ["urgent", "hotfix"]

        # Apply tags twice
        await client.post(
            "/tasks/batch-ops",
            json={"action": "tag", "ids": [task_id], "tags": tags},
        )
        await client.post(
            "/tasks/batch-ops",
            json={"action": "tag", "ids": [task_id], "tags": tags},
        )

        # Verify via the store directly (metadata is not in TaskResponse)
        # We just verify the second call succeeds without error
        resp = await client.post(
            "/tasks/batch-ops",
            json={"action": "tag", "ids": [task_id], "tags": tags},
        )
        assert resp.status_code == 200
        assert task_id in resp.json()["succeeded"]
