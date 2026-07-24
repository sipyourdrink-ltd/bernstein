"""Dashboard capacity denominator equals the run's effective ``max_agents``.

``status_dashboard`` builds ``config_provenance`` from ``resolve_config_bundle``,
whose project layer only reads ``.sdd/config.yaml``. A run configured by a seed
(``bernstein.yaml``) with a non-default ``max_agents`` therefore fell back to the
built-in default of 6, so the ``Agents active/max`` denominator lied about the
run's real capacity. The server now threads the seed it loaded at bootstrap into
the provenance chain as the top-precedence ``seed`` layer (issue #2874).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from bernstein.core.home import BernsteinHome, resolve_config, resolve_config_bundle
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import bernstein.core.routes.status_dashboard as status_routes
from bernstein.core.server import create_app


class TestSeedProvenanceLayer:
    """``resolve_config`` injects the seed value as the winning layer."""

    def test_seed_override_becomes_winning_source(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        sdd_config = tmp_path / ".sdd" / "config.yaml"
        sdd_config.parent.mkdir(parents=True, exist_ok=True)
        sdd_config.write_text("max_agents: 8\n", encoding="utf-8")

        resolved = resolve_config(
            "max_agents",
            home=home,
            project_dir=tmp_path,
            seed_overrides={"max_agents": 3},
            seed_overrides_path=str(tmp_path / "bernstein.yaml"),
        )

        assert resolved["value"] == 3
        assert resolved["source"] == "seed"
        # The seed layer sits above the project layer in the chain.
        chain_sources = [layer["source"] for layer in resolved["source_chain"]]
        assert chain_sources[0] == "seed"
        assert "project" in chain_sources
        assert resolved["source_chain"][0]["path"] == str(tmp_path / "bernstein.yaml")

    def test_key_absent_from_seed_is_unchanged(self, tmp_path: Path) -> None:
        home = BernsteinHome(tmp_path / ".bernstein")
        bundle = resolve_config_bundle(
            home=home,
            project_dir=tmp_path,
            keys=("cli", "max_agents"),
            seed_overrides={"max_agents": 4},
        )
        # ``cli`` is not in the seed overrides, so no seed layer appears for it.
        assert [layer["source"] for layer in bundle["cli"]["source_chain"]] == ["default"]
        assert bundle["max_agents"]["value"] == 4
        assert bundle["max_agents"]["source"] == "seed"


def _make_app(tmp_path: Path) -> FastAPI:
    jsonl_path = tmp_path / ".sdd" / "runtime" / "tasks.jsonl"
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    return create_app(jsonl_path=jsonl_path)


@pytest.mark.anyio
async def test_seed_max_agents_reaches_config_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-default seed ``max_agents`` reaches the dashboard denominator."""
    monkeypatch.setattr(status_routes, "_runtime_cache", {})
    monkeypatch.setattr(status_routes, "_runtime_cache_ts", 0.0)
    home_dir = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home_dir))

    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    app_state.workdir = tmp_path
    # The server loads the seed at bootstrap; simulate that carried value.
    app_state.seed_config = SimpleNamespace(max_agents=3, budget_usd=None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/status")

    assert response.status_code == 200
    body = response.json()
    provenance = body["runtime"]["config_provenance"]
    assert provenance["max_agents"]["value"] == 3
    assert provenance["max_agents"]["source"] == "seed"
    # The summary denominator the TUI header binds to also updates.
    assert body["summary"]["max_agents"] == 3


@pytest.mark.anyio
async def test_no_seed_falls_back_to_file_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no seed loaded, provenance is the unchanged file-based chain."""
    monkeypatch.setattr(status_routes, "_runtime_cache", {})
    monkeypatch.setattr(status_routes, "_runtime_cache_ts", 0.0)
    home_dir = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home_dir))

    app = _make_app(tmp_path)
    app_state = cast(Any, app.state)
    app_state.workdir = tmp_path
    app_state.seed_config = None
    sdd_config = tmp_path / ".sdd" / "config.yaml"
    sdd_config.parent.mkdir(parents=True, exist_ok=True)
    sdd_config.write_text("max_agents: 8\n", encoding="utf-8")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/status")

    provenance = response.json()["runtime"]["config_provenance"]
    assert provenance["max_agents"]["value"] == 8
    assert provenance["max_agents"]["source"] == "project"
