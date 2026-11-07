"""Tests for the fleet aggregator stub mounted at ``/api/v1/fleet/...``.

The stub is intentionally minimal; these tests pin its contract so the
SPA can rely on the response shape while a richer aggregator is being
built behind the same surface.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    # An operator shell that exports a fleet config must not turn these
    # stub-contract tests into aggregator tests.
    monkeypatch.delenv("BERNSTEIN_FLEET_CONFIG", raising=False)
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestFleetProjects:
    """``GET /fleet/projects`` - overview snapshots for the SPA grid."""

    @pytest.mark.anyio()
    async def test_returns_200(self, client: AsyncClient) -> None:
        resp = await client.get("/fleet/projects")
        assert resp.status_code == 200

    @pytest.mark.anyio()
    async def test_stub_shape_when_no_aggregator(self, client: AsyncClient) -> None:
        """No aggregator attached → empty list + ``stub: true`` flag."""
        resp = await client.get("/fleet/projects")
        data = resp.json()
        assert data["projects"] == []
        assert data["stub"] is True
        # Operator-facing hint must mention how to start the real aggregator.
        assert "bernstein fleet" in data["hint"].lower()

    @pytest.mark.anyio()
    async def test_versioned_alias(self, client: AsyncClient) -> None:
        """The ``/api/v1`` prefix must serve the same payload."""
        resp = await client.get("/api/v1/fleet/projects")
        assert resp.status_code == 200
        # Same stub flag - SPA must be able to detect "no aggregator" via /api/v1.
        assert resp.json()["stub"] is True


class TestFleetSearch:
    """``GET /fleet/search`` - operator search syntax round-trip."""

    @pytest.mark.anyio()
    async def test_parses_operator_syntax(self, client: AsyncClient) -> None:
        """`agent:claude status:running across:all` parses into filters."""
        resp = await client.get(
            "/fleet/search",
            params={"q": "agent:claude status:running across:all login flow"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["filters"] == {
            "agent": "claude",
            "status": "running",
            "across": "all",
        }
        assert data["free_text"] == "login flow"
        # Stub always returns empty matches while the real backend is pending.
        assert data["matches"] == []
        assert data["stub"] is True

    @pytest.mark.anyio()
    async def test_empty_query(self, client: AsyncClient) -> None:
        resp = await client.get("/fleet/search")
        assert resp.status_code == 200
        data = resp.json()
        assert data["filters"] == {}
        assert data["free_text"] == ""

    @pytest.mark.anyio()
    async def test_versioned_alias(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/fleet/search", params={"q": "agent:claude"})
        assert resp.status_code == 200
        assert resp.json()["filters"] == {"agent": "claude"}


class TestFleetConfigAttachesAggregator:
    """``BERNSTEIN_FLEET_CONFIG`` makes the task server serve the real overview."""

    def test_listed_projects_replace_the_stub(
        self, tmp_path: Path, jsonl_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_root = tmp_path / "alpha"
        project_root.mkdir()
        config = tmp_path / "projects.toml"
        # Port 9 (discard) refuses: the row must show up as offline, not vanish.
        config.write_text(
            f'[[project]]\nname = "alpha"\npath = "{project_root}"\ntask_server_url = "http://127.0.0.1:9"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("BERNSTEIN_FLEET_CONFIG", str(config))

        with TestClient(create_app(jsonl_path=jsonl_path)) as client:
            data = client.get("/fleet/projects").json()

        assert data["stub"] is False
        assert [p["name"] for p in data["projects"]] == ["alpha"]

    def test_config_without_projects_keeps_the_stub(
        self, tmp_path: Path, jsonl_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = tmp_path / "projects.toml"
        config.write_text("# no [[project]] blocks yet\n", encoding="utf-8")
        monkeypatch.setenv("BERNSTEIN_FLEET_CONFIG", str(config))

        with TestClient(create_app(jsonl_path=jsonl_path)) as client:
            data = client.get("/fleet/projects").json()

        assert data["stub"] is True
        assert data["projects"] == []
