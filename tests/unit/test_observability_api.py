"""Unit tests for observability API endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from bernstein.core.agent_signals import AgentSignalManager
from bernstein.core.completion_budget import CompletionBudget
from bernstein.core.effectiveness import EffectivenessScore, EffectivenessScorer
from bernstein.core.models import AgentHeartbeat
from fastapi.testclient import TestClient

from bernstein.core.server import create_app


def _app(tmp_path: Path):
    return create_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")


def _record_score(
    tmp_path: Path, *, role: str = "backend", total: int = 85, model: str = "opus", effort: str = "max"
) -> None:
    scorer = EffectivenessScorer(tmp_path)
    scorer.record(
        EffectivenessScore(
            session_id=f"{role}-{total}",
            task_id=f"T-{total}",
            role=role,
            model=model,
            effort=effort,
            time_score=90,
            quality_score=90,
            efficiency_score=80,
            retry_score=100,
            completion_score=100,
            total=total,
            grade="A" if total >= 90 else "B",
            wall_time_s=120.0,
            estimated_time_s=300.0,
            tokens_used=500,
            retry_count=0,
            fix_count=0,
            gate_pass_rate=1.0,
        )
    )


def test_agents_endpoint_returns_heartbeat(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        task_id = client.post(
            "/tasks",
            json={"title": "Agent task", "description": "Do work", "role": "backend"},
        ).json()["id"]
        runtime_dir = tmp_path / ".sdd" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "agents.json").write_text(
            json.dumps(
                {
                    "agents": [
                        {"id": "A-1", "role": "backend", "status": "running", "model": "sonnet", "task_ids": [task_id]},
                        {"id": "A-2", "role": "qa", "status": "running", "model": "opus", "task_ids": []},
                    ]
                }
            ),
            encoding="utf-8",
        )
        signal_mgr = AgentSignalManager(tmp_path)
        signal_mgr.write_heartbeat(
            "A-1",
            AgentHeartbeat(
                timestamp=1.0,
                files_changed=1,
                status="working",
                phase="implementing",
                progress_pct=45,
            ),
        )
        signal_mgr.write_heartbeat(
            "A-2",
            AgentHeartbeat(
                timestamp=1.0,
                files_changed=0,
                status="working",
                phase="testing",
                progress_pct=80,
            ),
        )

        response = client.get("/observability/agents")
        body = response.json()

    assert response.status_code == 200
    assert body["total"] == 2
    assert body["agents"][0]["heartbeat"]["progress_pct"] == 45


def test_agents_endpoint_empty(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        body = client.get("/observability/agents").json()

    assert body["total"] == 0
    assert body["agents"] == []


def test_effectiveness_endpoint(tmp_path: Path) -> None:
    _record_score(tmp_path, role="backend", total=90, model="opus", effort="max")
    _record_score(tmp_path, role="backend", total=85, model="opus", effort="max")
    _record_score(tmp_path, role="qa", total=75, model="sonnet", effort="high")
    _record_score(tmp_path, role="qa", total=78, model="sonnet", effort="high")
    _record_score(tmp_path, role="qa", total=80, model="sonnet", effort="high")

    with TestClient(_app(tmp_path)) as client:
        body = client.get("/observability/effectiveness").json()

    assert "backend" in body["per_role"]
    assert "best_configs" in body


def test_recommendations_endpoint(tmp_path: Path) -> None:
    recommendations_path = tmp_path / ".sdd" / "recommendations.yaml"
    recommendations_path.parent.mkdir(parents=True, exist_ok=True)
    recommendations_path.write_text(
        yaml.safe_dump(
            {
                "recommendations": [
                    {
                        "id": "use-uv",
                        "category": "tool_usage",
                        "severity": "critical",
                        "text": "Always use `uv run`",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with TestClient(_app(tmp_path)) as client:
        body = client.get("/observability/recommendations").json()

    assert body["recommendations"][0]["category"] == "tool_usage"


def test_budget_endpoint(tmp_path: Path, make_task: Any) -> None:
    budget = CompletionBudget(tmp_path)
    budget.record_attempt(make_task(title="Lineage A"))
    task_b = make_task(title="Lineage B")
    for _ in range(5):
        budget.record_attempt(task_b)

    with TestClient(_app(tmp_path)) as client:
        body = client.get("/observability/budget").json()

    assert len(body["lineages"]) == 2
    assert len(body["exhausted"]) == 1


def test_deps_endpoint(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        a = client.post("/tasks", json={"title": "A", "description": "A", "role": "backend"}).json()["id"]
        b = client.post("/tasks", json={"title": "B", "description": "B", "role": "backend"}).json()["id"]
        store = app.state.store
        store.get_task(a).depends_on = [b]
        store.get_task(b).depends_on = [a]

        body = client.get("/observability/deps").json()

    assert body["valid"] is False
    assert body["cycles"] == [[a, b, a]]
