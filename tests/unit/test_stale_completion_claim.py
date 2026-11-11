"""Regression tests for stale tokenless task completion reports (#6644)."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.mark.asyncio
async def test_tokenless_completion_after_reclaim_documents_legacy_gap(
    tmp_path: Path,
) -> None:
    """Document current behavior when A's tokenless report arrives after B reclaims."""
    app = create_app(jsonl_path=tmp_path / "runtime" / "tasks.jsonl")
    app.state.draining = False
    store = app.state.store

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/tasks",
            json={
                "title": "Late tokenless report",
                "description": "Document legacy completion behavior after reclaim",
                "role": "backend",
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]

        claim_a = await client.post(
            f"/tasks/{task_id}/claim",
            params={"claimed_by_session": "worker-a"},
        )
        assert claim_a.status_code == 200, claim_a.text

        released = await client.post(
            f"/tasks/{task_id}/release",
            json={"reason": "handoff to worker B"},
        )
        assert released.status_code == 200, released.text

        claim_b = await client.post(
            f"/tasks/{task_id}/claim",
            params={"claimed_by_session": "worker-b"},
        )
        assert claim_b.status_code == 200, claim_b.text
        assert store.get_task(task_id).claimed_by_session == "worker-b"

        stale_response = await client.post(
            f"/tasks/{task_id}/complete",
            json={"result_summary": "late report from worker A"},
        )

        current = await client.get(f"/tasks/{task_id}")
        assert stale_response.status_code == 200, stale_response.text
        assert current.status_code == 200, current.text

        # Characterize the unresolved legacy gap: the tokenless report is
        # accepted even though worker B has reclaimed the task. The recorded
        # claimant remains B, but A's report transitions the task to DONE.
        assert current.json()["status"] == "done"
        assert current.json()["result_summary"] == "late report from worker A"
        assert store.get_task(task_id).claimed_by_session == "worker-b"


@pytest.mark.asyncio
async def test_duplicate_completion_preserves_first_result(tmp_path: Path) -> None:
    """A duplicate terminal report must not overwrite the first completion."""
    app = create_app(jsonl_path=tmp_path / "runtime" / "tasks.jsonl")
    app.state.draining = False

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/tasks",
            json={
                "title": "Duplicate report",
                "description": "Check duplicate completion delivery",
                "role": "backend",
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]

        claimed = await client.post(
            f"/tasks/{task_id}/claim",
            params={"claimed_by_session": "worker-a"},
        )
        assert claimed.status_code == 200, claimed.text

        first = await client.post(
            f"/tasks/{task_id}/complete",
            json={"result_summary": "first result"},
        )
        assert first.status_code == 200, first.text

        duplicate = await client.post(
            f"/tasks/{task_id}/complete",
            json={"result_summary": "duplicate result"},
        )
        assert duplicate.status_code == 409, duplicate.text

        current = await client.get(f"/tasks/{task_id}")
        assert current.status_code == 200, current.text
        assert current.json()["status"] == "done"
        assert current.json()["result_summary"] == "first result"


@pytest.mark.asyncio
async def test_current_claimant_can_complete_task(tmp_path: Path) -> None:
    """A legitimate completion from the current claim must still succeed."""
    app = create_app(jsonl_path=tmp_path / "runtime" / "tasks.jsonl")
    app.state.draining = False
    store = app.state.store

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/tasks",
            json={
                "title": "Valid completion",
                "description": "Check normal completion",
                "role": "backend",
            },
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]

        claimed = await client.post(
            f"/tasks/{task_id}/claim",
            params={"claimed_by_session": "worker-current"},
        )
        assert claimed.status_code == 200, claimed.text
        assert store.get_task(task_id).claimed_by_session == "worker-current"

        completed = await client.post(
            f"/tasks/{task_id}/complete",
            json={"result_summary": "legitimate result"},
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "done"
        assert completed.json()["result_summary"] == "legitimate result"
