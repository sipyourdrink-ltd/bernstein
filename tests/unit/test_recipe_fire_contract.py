"""The payload ``recipes fire`` submits is one the task server accepts (#2654).

A dispatcher that posts a body the server rejects fails in the most
expensive way available: the fire reports "the dispatcher submitted no work",
which is a legitimate outcome, so the failure is indistinguishable from a
correctly-refused fire and an operator goes looking at their recipe.

Every test here validates against the *real* ``TaskCreate`` model or the real
route. A stub that accepts any payload cannot test a submission contract - it
only proves a function was called - so field, enum, or required-key drift in
the task model must fail this file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

from bernstein.cli.commands.recipes_cmd import recipe_fire_payload
from bernstein.core.server import create_app
from bernstein.core.server.server_models import TaskCreate

if TYPE_CHECKING:
    from pathlib import Path

_METADATA = {
    "source_type": "schedule",
    "schedule_id": "b" * 64,
    "fire_time": 1_800_000_000.0,
    "misfire_policy": "skip",
    "projection_hash": "a" * 64,
    "recipe_name": "nightly-triage",
    "recipe_hash": "b" * 64,
    "recipe_schedule_id": "",
}


class TestPayloadSatisfiesTheTaskModel:
    def test_payload_validates_against_the_real_task_create_model(self) -> None:
        """The single test that makes an invalid-field defect impossible.

        ``TaskCreate`` is what POST /tasks binds, so validating here is the
        same check FastAPI performs before the handler runs.
        """
        task = TaskCreate(**recipe_fire_payload(_METADATA))
        assert task.title
        assert task.description
        assert task.role

    def test_task_type_is_one_the_model_accepts(self) -> None:
        payload = recipe_fire_payload(_METADATA)
        # Pinned explicitly: this exact field silently bricked every fire.
        assert payload["task_type"] in {"standard", "upgrade_proposal", "fix", "research"}

    def test_payload_carries_the_fire_provenance(self) -> None:
        payload = recipe_fire_payload(_METADATA)
        assert "nightly-triage" in payload["title"]
        assert _METADATA["projection_hash"] in payload["description"]
        assert payload["metadata"]["recipe_hash"] == _METADATA["recipe_hash"]

    def test_a_minimal_metadata_payload_still_validates(self) -> None:
        # A schedule-neutral manual fire carries almost nothing; the body must
        # still satisfy the model rather than depending on optional keys.
        TaskCreate(**recipe_fire_payload({}))


class TestPayloadIsAcceptedByTheRealRoute:
    @pytest.mark.anyio
    async def test_post_tasks_accepts_the_dispatch_payload(self, tmp_path: Path) -> None:
        """End-to-end against the actual FastAPI route, not a stub.

        Proves the fire path can succeed against a real server: the route
        binds ``TaskCreate``, so a 422 here is exactly the production failure.
        """
        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/tasks", json=recipe_fire_payload(_METADATA))

        assert response.status_code in (200, 201), (
            f"the task server rejected the recipe fire payload: {response.status_code} {response.text}"
        )
        created = response.json()
        assert str(created.get("id", "")).strip(), "a submitted fire must come back with a task id"
