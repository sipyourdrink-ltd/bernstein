"""Track B server-route tests for runtime metadata endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import bernstein.core.routes.status_dashboard as status_routes
from bernstein.core.server import create_app


def _make_app(tmp_path: Path) -> FastAPI:
    jsonl_path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    return create_app(jsonl_path=jsonl_path)


@pytest.mark.anyio
async def test_health_reports_restart_count_and_memory(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    runtime_dir = Path(app_state.sdd_dir) / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "supervisor_state.json").write_text(
        json.dumps(
            {
                "started_at": 1.0,
                "restart_count": 3,
                "current_pid": 1234,
                "last_restart_at": 2.0,
            }
        ),
        encoding="utf-8",
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["restart_count"] == 3
    assert "memory_mb" in payload


@pytest.mark.anyio
async def test_status_includes_runtime_summary_block(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    archive_path = tmp_path / ".sdd" / "archive" / "tasks.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(
        json.dumps(
            {
                "task_id": "task-123",
                "title": "Fix auth flow",
                "role": "backend",
                "status": "done",
                "created_at": 1.0,
                "completed_at": 2.0,
                "duration_seconds": 1.0,
                "result_summary": "done",
                "cost_usd": None,
                "assigned_agent": "sess-123",
                "owned_files": ["src/auth.py"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    app_state.store._archive_path = archive_path  # pyright: ignore[reportPrivateUsage]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/status")

    assert response.status_code == 200
    runtime = response.json()["runtime"]
    assert "active_worktrees" in runtime
    assert "disk_usage_mb" in runtime
    assert runtime["last_completed"]["task_id"] == "task-123"


@pytest.mark.anyio
async def test_status_includes_config_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_routes, "_runtime_cache", {})
    monkeypatch.setattr(status_routes, "_runtime_cache_ts", 0.0)
    monkeypatch.setenv("BERNSTEIN_CLI", "qwen")
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    app_state.workdir = tmp_path
    home_dir = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home_dir))
    home_config = home_dir / ".bernstein" / "config.yaml"
    home_config.parent.mkdir(parents=True)
    home_config.write_text("cli: codex\n", encoding="utf-8")
    sdd_config = tmp_path / ".sdd" / "config.yaml"
    sdd_config.parent.mkdir(parents=True, exist_ok=True)
    sdd_config.write_text("cli: gemini\nmax_agents: 8\n", encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/status")

    assert response.status_code == 200
    provenance = response.json()["runtime"]["config_provenance"]
    assert provenance["cli"]["source"] == "session"
    assert [layer["source"] for layer in provenance["cli"]["source_chain"]] == [
        "session",
        "project",
        "global",
        "default",
    ]
    assert provenance["max_agents"]["source"] == "project"


@pytest.mark.anyio
async def test_routing_bandit_invalid_state_returns_generic_error(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    routing_dir = Path(app_state.store.jsonl_path).parent.parent / "routing"
    routing_dir.mkdir(parents=True, exist_ok=True)
    (routing_dir / "bandit_state.json").write_text("{", encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/routing/bandit")

    assert response.status_code == 500
    assert response.json() == {
        "mode": "bandit",
        "active": True,
        "error": "Failed to read routing bandit state",
    }


@pytest.mark.anyio
async def test_routing_bandit_exposes_linucb_shadow_and_exploration_stats(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    routing_dir = Path(app_state.store.jsonl_path).parent.parent / "routing"
    routing_dir.mkdir(parents=True, exist_ok=True)
    (routing_dir / "bandit_state.json").write_text(
        json.dumps(
            {
                "mode": "bandit",
                "total_completions": 12,
                "warmup_min": 5,
                "exploration_rate": 0.0866,
                "selection_counts": {"haiku": 3, "sonnet": 9},
                "exploration_stats": {
                    "haiku": {"samples": 3, "last": 0.12, "mean": 0.11, "variance": 0.001},
                    "sonnet": {"samples": 9, "last": 0.04, "mean": 0.05, "variance": 0.0004},
                },
                "shadow_stats": {
                    "total_decisions": 4,
                    "matched_outcomes": 3,
                    "pending_outcomes": 1,
                    "agreement_rate": 0.666667,
                    "disagreement_count": 1,
                    "avg_executed_reward_when_agree": 0.91,
                    "avg_executed_reward_when_disagree": 0.62,
                },
            }
        ),
        encoding="utf-8",
    )
    (routing_dir / "policy.json").write_text(json.dumps({"total_updates": 12}), encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/routing/bandit")

    assert response.status_code == 200
    payload = response.json()
    assert payload["active"] is True
    assert payload["total_policy_updates"] == 12
    assert payload["selection_frequency"]["sonnet"] == 9
    assert payload["exploration_stats"]["haiku"]["samples"] == 3
    assert payload["shadow_stats"]["pending_outcomes"] == 1


@pytest.mark.anyio
async def test_cache_stats_invalid_manifest_hides_parse_details(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    caching_dir = Path(app_state.sdd_dir) / "caching"
    caching_dir.mkdir(parents=True, exist_ok=True)
    (caching_dir / "manifest.jsonl").write_text("{", encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/cache-stats")

    assert response.status_code == 500
    assert response.json() == {"error": "Failed to read cache manifest"}


@pytest.mark.anyio
async def test_memory_audit_invalid_json_hides_parse_details(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    memory_dir = Path(app_state.sdd_dir) / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "lessons.jsonl").write_text("{", encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/memory/audit")

    assert response.status_code == 200
    assert response.json()["errors"] == ["Line 1: invalid JSON entry"]
