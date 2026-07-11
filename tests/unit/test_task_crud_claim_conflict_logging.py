"""Logging-gap smoke test for the /tasks/{id}/claim 409 path (task_crud.py).

Before this change, a claim conflict (stale ``expected_version`` or a task
already claimed) raised HTTPException(409) with no server-side log line --
the only visibility was the client's own HTTP response, which is invisible
in the orchestrator's server-side log stream.  On 2026-07-02 a 144-iteration
claim-retry loop against this exact endpoint left the operator with a bare
401/409 storm and no way to tell WHY the conflicts were happening (expected
vs. actual version) from the server log alone.

This test drives the real FastAPI route (not the store layer directly) via
TestClient so it exercises exactly the code path that fired the loop, and
asserts the new WARNING log line carries the expected/actual version and
status needed to diagnose a churn loop from the server log alone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from starlette.testclient import TestClient

from bernstein.core.server import create_app

if TYPE_CHECKING:
    from pathlib import Path


def test_claim_version_conflict_logs_expected_vs_actual(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bernstein.core.routes.task_crud")
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")

    with TestClient(app) as client:
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

        # First claim succeeds and bumps the version.
        first = client.post(f"/tasks/{task_id}/claim")
        assert first.status_code == 200, first.text

        # Second claim against a stale expected_version=1 must 409 -- the
        # task is already claimed (version bumped past 1 by the first claim).
        second = client.post(f"/tasks/{task_id}/claim", params={"expected_version": 1})
        assert second.status_code == 409, second.text

    conflict_records = [
        r.message
        for r in caplog.records
        if "task.claim 409" in r.message  # type: ignore[attr-defined]
    ]
    assert conflict_records, f"expected a task.claim 409 log line, got: {caplog.text}"  # type: ignore[attr-defined]
    line = conflict_records[0]
    assert task_id in line
    assert "expected_version=1" in line
    assert "actual_version=" in line
    assert "pre_claim_status=" in line

    ok_records = [r.message for r in caplog.records if "task.claim ok" in r.message]  # type: ignore[attr-defined]
    assert any(task_id in m for m in ok_records), "expected a task.claim ok line for the first successful claim"
