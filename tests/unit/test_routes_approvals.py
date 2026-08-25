"""Unit tests for approvals route (issue #4535).

Verifies that GET /approvals returns both task-review pending entries and
pre-spawn approval-spec pending entries, tagged with mechanism and resolution details.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.models import ApprovalSpec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.orchestration.approval_gate import write_pending_sentinel
from bernstein.core.routes import approvals


def _create_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    app.include_router(approvals.router)

    # Re-anchor directories under tmp_path
    monkeypatch.setattr(approvals, "_PENDING_DIR", tmp_path / ".sdd" / "runtime" / "pending_approvals")
    monkeypatch.setattr(approvals, "_APPROVALS_DIR", tmp_path / ".sdd" / "runtime" / "approvals")

    return TestClient(app)


def test_task_level_pending_entries_keep_their_existing_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _create_app(tmp_path, monkeypatch)

    pending_dir = tmp_path / ".sdd" / "runtime" / "pending_approvals"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "T-1.json").write_text(
        json.dumps(
            {
                "task_id": "T-1",
                "task_title": "Fix auth",
                "session_id": "S-1",
                "diff": "+line",
                "test_summary": "1 passed",
            }
        )
    )

    resp = client.get("/approvals")
    assert resp.status_code == 200
    data = resp.json()
    assert "pending" in data
    assert len(data["pending"]) == 1
    item = data["pending"][0]

    # Existing shape preserved
    assert item["task_id"] == "T-1"
    assert item["task_title"] == "Fix auth"
    assert item["session_id"] == "S-1"
    assert item["diff"] == "+line"
    assert item["test_summary"] == "1 passed"

    # Additive metadata present
    assert item["mechanism"] == "task_review"
    assert item["unblocks"] == "task completion and merge"
    assert item["resolution_endpoint"] == "/approvals/T-1/approve"


def test_pre_spawn_pending_is_listed_while_task_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _create_app(tmp_path, monkeypatch)

    spec = ApprovalSpec(prompt="Deploy to staging?", timeout_seconds=300, default_action="reject")
    write_pending_sentinel(tmp_path, "T-2", spec)

    resp = client.get("/approvals")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["pending"]) == 1
    item = data["pending"][0]

    assert item["task_id"] == "T-2"
    assert item["task_title"] == "Deploy to staging?"
    assert item["mechanism"] == "pre_spawn"
    assert item["prompt"] == "Deploy to staging?"
    assert item["default_action"] == "reject"
    assert item["timeout_seconds"] == 300
    assert item["unblocks"] == "agent spawn and task execution"
    assert item["resolution_endpoint"] == "/approvals/T-2/approve"


def test_resolved_pre_spawn_pending_leaves_the_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _create_app(tmp_path, monkeypatch)

    spec = ApprovalSpec(prompt="Deploy to staging?", timeout_seconds=300, default_action="reject")
    write_pending_sentinel(tmp_path, "T-3", spec)

    resp1 = client.get("/approvals")
    assert len(resp1.json()["pending"]) == 1

    # Approve via endpoint
    approve_resp = client.post("/approvals/T-3/approve", json={"reason": "operator verified"})
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "approved"

    # Must be gone from listing
    resp2 = client.get("/approvals")
    assert len(resp2.json()["pending"]) == 0


def test_both_mechanisms_coexist_in_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _create_app(tmp_path, monkeypatch)

    # 1. Task-level review pending
    pending_dir = tmp_path / ".sdd" / "runtime" / "pending_approvals"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "T-review.json").write_text(
        json.dumps(
            {
                "task_id": "T-review",
                "task_title": "Review diff",
                "session_id": "S-rev",
            }
        )
    )

    # 2. Pre-spawn pending
    spec = ApprovalSpec(prompt="Allow spawn?", timeout_seconds=120)
    write_pending_sentinel(tmp_path, "T-prespawn", spec)

    resp = client.get("/approvals")
    assert resp.status_code == 200
    items = resp.json()["pending"]
    assert len(items) == 2

    mechanisms = {item["task_id"]: item["mechanism"] for item in items}
    assert mechanisms["T-review"] == "task_review"
    assert mechanisms["T-prespawn"] == "pre_spawn"
