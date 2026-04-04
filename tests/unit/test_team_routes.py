"""Tests for team state HTTP API routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.routes.team import router
from bernstein.core.team_state import TeamStateStore


@pytest.fixture()
def app_with_team(tmp_path: Path) -> tuple[FastAPI, TeamStateStore]:
    """Create a FastAPI app with team routes and a writable sdd dir."""
    app = FastAPI()
    app.include_router(router)
    app.state.sdd_dir = tmp_path  # type: ignore[attr-defined]

    store = TeamStateStore(tmp_path)
    return app, store


class TestTeamRoutes:
    def test_get_team_empty(self, app_with_team: tuple[FastAPI, TeamStateStore]) -> None:
        app, _store = app_with_team
        client = TestClient(app)
        resp = client.get("/team")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_members"] == 0
        assert data["active_count"] == 0
        assert data["members"] == []

    def test_get_team_with_members(self, app_with_team: tuple[FastAPI, TeamStateStore]) -> None:
        app, store = app_with_team
        store.on_spawn("a1", "backend", model="sonnet")
        store.on_spawn("a2", "qa", model="haiku")

        client = TestClient(app)
        resp = client.get("/team")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_members"] == 2
        assert data["active_count"] == 2
        assert len(data["members"]) == 2

    def test_get_team_active(self, app_with_team: tuple[FastAPI, TeamStateStore]) -> None:
        app, store = app_with_team
        store.on_spawn("a1", "backend")
        store.on_spawn("a2", "qa")
        store.on_complete("a1")

        client = TestClient(app)
        resp = client.get("/team/active")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["members"]) == 1
        assert data["members"][0]["agent_id"] == "a2"

    def test_get_team_member_found(self, app_with_team: tuple[FastAPI, TeamStateStore]) -> None:
        app, store = app_with_team
        store.on_spawn("a1", "backend", model="opus", provider="claude")

        client = TestClient(app)
        resp = client.get("/team/a1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "a1"
        assert data["role"] == "backend"
        assert data["model"] == "opus"
        assert data["provider"] == "claude"

    def test_get_team_member_not_found(self, app_with_team: tuple[FastAPI, TeamStateStore]) -> None:
        app, _store = app_with_team
        client = TestClient(app)
        resp = client.get("/team/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"]
