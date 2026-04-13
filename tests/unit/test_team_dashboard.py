"""Tests for team adoption dashboard route."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.models import Task, TaskStatus
from bernstein.core.task_store import TaskStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.routes.team_dashboard import router


@pytest.fixture()
def dashboard_app(tmp_path: Path) -> tuple[FastAPI, TaskStore, Path]:
    """Create a FastAPI app with team dashboard routes and empty state."""
    app = FastAPI()
    app.include_router(router)
    sdd_dir = tmp_path / ".sdd"
    sdd_dir.mkdir()
    (sdd_dir / "runtime").mkdir(parents=True)

    store = TaskStore(jsonl_path=sdd_dir / "runtime" / "tasks.jsonl")
    app.state.store = store  # type: ignore[attr-defined]
    app.state.sdd_dir = sdd_dir  # type: ignore[attr-defined]
    app.state.workdir = tmp_path  # type: ignore[attr-defined]
    return app, store, sdd_dir


class TestTeamDashboard:
    def test_empty_dashboard(self, dashboard_app: tuple[FastAPI, TaskStore, Path]) -> None:
        app, _store, _sdd = dashboard_app
        client = TestClient(app)
        resp = client.get("/dashboard/team")
        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert "timestamp" in data
        assert data["summary"]["total_runs"] == 0
        assert data["summary"]["tasks_completed"] == 0
        assert data["summary"]["cost_spent_usd"] == pytest.approx(0.0)
        assert data["summary"]["quality_gate_pass_rate_pct"] == pytest.approx(0.0)

    def test_with_completed_tasks(self, dashboard_app: tuple[FastAPI, TaskStore, Path]) -> None:
        app, store, _sdd = dashboard_app
        store._tasks["t1"] = Task(
            id="t1", title="Build API", description="Build it", role="backend", status=TaskStatus.DONE
        )
        store._tasks["t2"] = Task(
            id="t2", title="Write tests", description="Test it", role="qa", status=TaskStatus.DONE
        )
        store._tasks["t3"] = Task(
            id="t3", title="Deploy", description="Deploy it", role="devops", status=TaskStatus.OPEN
        )

        client = TestClient(app)
        resp = client.get("/dashboard/team")
        data = resp.json()
        assert data["summary"]["tasks_completed"] == 2
        assert data["tasks"]["total"] == 3
        assert data["tasks"]["by_role"]["backend"] == 1
        assert data["tasks"]["by_role"]["qa"] == 1

    def test_with_cost_data(self, dashboard_app: tuple[FastAPI, TaskStore, Path]) -> None:
        app, _store, sdd = dashboard_app
        costs_dir = sdd / "runtime" / "costs"
        costs_dir.mkdir(parents=True)
        cost_data = {
            "total_cost_usd": 1.25,
            "budget_usd": 5.00,
            "usages": [
                {"agent_id": "agent-1", "model": "opus", "cost_usd": 0.75},
                {"agent_id": "agent-2", "model": "sonnet", "cost_usd": 0.50},
            ],
        }
        (costs_dir / "run-001.json").write_text(json.dumps(cost_data))

        client = TestClient(app)
        resp = client.get("/dashboard/team")
        data = resp.json()
        assert data["summary"]["total_runs"] == 1
        assert data["summary"]["cost_spent_usd"] == pytest.approx(1.25)
        assert data["summary"]["cost_saved_usd"] == pytest.approx(3.75)
        assert data["costs"]["per_agent"]["agent-1"] == pytest.approx(0.75)
        assert data["costs"]["per_model"]["opus"] == pytest.approx(0.75)

    def test_with_quality_gate_data(self, dashboard_app: tuple[FastAPI, TaskStore, Path]) -> None:
        app, _store, sdd = dashboard_app
        quality_dir = sdd / "runtime" / "quality"
        quality_dir.mkdir(parents=True)
        (quality_dir / "gate-1.json").write_text(json.dumps({"passed": True, "status": "passed"}))
        (quality_dir / "gate-2.json").write_text(json.dumps({"passed": True, "status": "passed"}))
        (quality_dir / "gate-3.json").write_text(json.dumps({"passed": False, "status": "failed"}))

        client = TestClient(app)
        resp = client.get("/dashboard/team")
        data = resp.json()
        assert data["quality_gates"]["passed"] == 2
        assert data["quality_gates"]["failed"] == 1
        assert data["summary"]["quality_gate_pass_rate_pct"] == pytest.approx(66.7)

    def test_with_merge_data(self, dashboard_app: tuple[FastAPI, TaskStore, Path]) -> None:
        app, _store, sdd = dashboard_app
        merge_dir = sdd / "runtime" / "merge_queue"
        merge_dir.mkdir(parents=True)
        (merge_dir / "m1.json").write_text(json.dumps({"status": "merged", "files_changed": 5}))
        (merge_dir / "m2.json").write_text(json.dumps({"status": "merged", "files_changed": 3}))
        (merge_dir / "m3.json").write_text(json.dumps({"status": "pending", "files_changed": 0}))

        client = TestClient(app)
        resp = client.get("/dashboard/team")
        data = resp.json()
        assert data["summary"]["code_merged_count"] == 2
        assert data["merges"]["files_changed_total"] == 8

    def test_team_section_included(self, dashboard_app: tuple[FastAPI, TaskStore, Path]) -> None:
        app, _store, _sdd = dashboard_app
        client = TestClient(app)
        resp = client.get("/dashboard/team")
        data = resp.json()
        assert "team" in data
        assert "total_members" in data["team"]

    def test_malformed_cost_file_skipped(self, dashboard_app: tuple[FastAPI, TaskStore, Path]) -> None:
        app, _store, sdd = dashboard_app
        costs_dir = sdd / "runtime" / "costs"
        costs_dir.mkdir(parents=True)
        (costs_dir / "bad.json").write_text("not json at all")
        (costs_dir / "good.json").write_text(json.dumps({"total_cost_usd": 2.0, "budget_usd": 10.0, "usages": []}))

        client = TestClient(app)
        resp = client.get("/dashboard/team")
        data = resp.json()
        assert data["summary"]["total_runs"] == 1
        assert data["summary"]["cost_spent_usd"] == pytest.approx(2.0)
