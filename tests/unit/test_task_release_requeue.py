"""Releasing a claimed task returns it to the open pool (#3018).

A cluster worker that claims a task but cannot start its agent (e.g. the
workspace is unusable, or the adapter spawn fails) must return the task to the
pool so another node can pick it up -- rather than stranding it in ``claimed``,
and without marking it terminally ``failed`` (which ``/fail`` does). This covers
both the store transition (``TaskStore.release``) and the HTTP surface
(``POST /tasks/{task_id}/release``).
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from starlette.testclient import TestClient

from bernstein.core.lifecycle import IllegalTransitionError
from bernstein.core.server import create_app
from bernstein.core.tasks.models import TaskStatus
from bernstein.core.tasks.task_store_core import TaskStore

if TYPE_CHECKING:
    from pathlib import Path


def _req(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "title": "T",
        "description": "D",
        "role": "backend",
        "priority": 2,
        "scope": "medium",
        "complexity": "medium",
        "estimated_minutes": 30,
        "depends_on": [],
        "owned_files": [],
        "cell_id": None,
        "task_type": "standard",
        "upgrade_details": None,
        "model": None,
        "effort": None,
        "batch_eligible": False,
        "completion_signals": [],
        "slack_context": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "runtime" / "tasks.jsonl", archive_path=tmp_path / "archive" / "tasks.jsonl")


# ---------------------------------------------------------------------------
# TaskStore.release
# ---------------------------------------------------------------------------


class TestStoreRelease:
    @pytest.mark.anyio
    async def test_release_returns_claimed_task_to_open_and_is_claimable(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        created = await store.create(_req())
        claimed = await store.claim_next("backend", claimed_by_session="node-A")
        assert claimed is not None
        assert store.get_task(created.id).status is TaskStatus.CLAIMED

        released = await store.release(created.id, "agent spawn failed on worker")

        # Back to OPEN, ownership cleared.
        assert released.status is TaskStatus.OPEN
        assert store.get_task(created.id).status is TaskStatus.OPEN
        assert store.get_task(created.id).claimed_by_session is None

        # Claimable again -- by another node.
        reclaimed = await store.claim_next("backend", claimed_by_session="node-B")
        assert reclaimed is not None
        assert reclaimed.id == created.id
        assert store.get_task(created.id).claimed_by_session == "node-B"

    @pytest.mark.anyio
    async def test_release_preserves_priority(self, tmp_path: Path) -> None:
        """A release does not bump the task ahead of untried work."""
        store = _store(tmp_path)
        created = await store.create(_req(priority=5))
        await store.claim_next("backend", claimed_by_session="node-A")

        released = await store.release(created.id, "spawn failed")

        assert released.priority == 5

    @pytest.mark.anyio
    async def test_release_rejects_open_task(self, tmp_path: Path) -> None:
        """Releasing a task that is not in-flight is an illegal transition."""
        store = _store(tmp_path)
        created = await store.create(_req())

        with pytest.raises(IllegalTransitionError):
            await store.release(created.id, "not claimed")

    @pytest.mark.anyio
    async def test_release_unknown_task_raises_keyerror(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(KeyError):
            await store.release("does-not-exist", "x")


# ---------------------------------------------------------------------------
# POST /tasks/{task_id}/release
# ---------------------------------------------------------------------------


def _create_and_claim(client: TestClient) -> str:
    create_resp = client.post(
        "/tasks",
        json={
            "title": "Do the thing",
            "description": "Some work.",
            "role": "backend",
            "priority": 1,
            "scope": "small",
            "complexity": "low",
            "estimated_minutes": 10,
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    task_id = create_resp.json()["id"]
    claim = client.post(f"/tasks/{task_id}/claim")
    assert claim.status_code == 200, claim.text
    return task_id


class TestReleaseRoute:
    def test_release_route_returns_task_to_open_and_reclaimable(self, tmp_path: Path) -> None:
        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        with TestClient(app) as client:
            task_id = _create_and_claim(client)

            release = client.post(f"/tasks/{task_id}/release", json={"reason": "spawn failed"})
            assert release.status_code == 200, release.text
            assert release.json()["status"] == "open"

            # Another node can claim it again.
            reclaim = client.post(f"/tasks/{task_id}/claim")
            assert reclaim.status_code == 200, reclaim.text

    def test_release_unknown_task_is_404(self, tmp_path: Path) -> None:
        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        with TestClient(app) as client:
            resp = client.post("/tasks/nope/release", json={"reason": "x"})
            assert resp.status_code == 404, resp.text

    def test_release_open_task_is_409(self, tmp_path: Path) -> None:
        """Releasing a task that was never claimed is a 409, not a silent no-op."""
        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        with TestClient(app) as client:
            create_resp = client.post(
                "/tasks",
                json={
                    "title": "t",
                    "description": "d",
                    "role": "backend",
                    "priority": 1,
                    "scope": "small",
                    "complexity": "low",
                    "estimated_minutes": 10,
                },
            )
            task_id = create_resp.json()["id"]
            resp = client.post(f"/tasks/{task_id}/release", json={"reason": "x"})
            assert resp.status_code == 409, resp.text
