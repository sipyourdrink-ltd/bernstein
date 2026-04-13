"""Tests for config reload diff surfacing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from bernstein.core.config_diff import diff_config_snapshots, load_redacted_config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


@pytest.fixture()
def jsonl_path(tmp_path: Path) -> Path:
    """Return a runtime-style JSONL path so create_app resolves the right workdir."""

    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir / "tasks.jsonl"


@pytest.fixture()
def app(jsonl_path: Path) -> FastAPI:
    """Create a test app bound to the temporary workdir."""

    return create_app(jsonl_path=jsonl_path)


@pytest.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Return an async client for the app."""

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


def test_diff_config_redacts_secret_fields(tmp_path: Path) -> None:
    """Secret-like config keys should be redacted in the diff summary."""

    before = tmp_path / "before.yaml"
    after = tmp_path / "after.yaml"
    before.write_text("goal: one\nwebhook_secret: old-secret\n", encoding="utf-8")
    after.write_text("goal: two\nwebhook_secret: new-secret\n", encoding="utf-8")

    summary = diff_config_snapshots(load_redacted_config(before), load_redacted_config(after))

    assert summary.changed is True
    assert any(change.path == "goal" and change.after == "two" for change in summary.changes)
    assert any(change.path == "webhook_secret" and change.before == "<redacted>" for change in summary.changes)
    assert any(change.path == "webhook_secret" and change.after == "<redacted>" for change in summary.changes)


@pytest.mark.anyio
async def test_reload_seed_config_surfaces_last_diff(client: AsyncClient, app: FastAPI, tmp_path: Path) -> None:
    """Reloading bernstein.yaml should expose a human-readable diff in status and dashboard data."""

    seed_path = tmp_path / "bernstein.yaml"
    seed_path.write_text("goal: first\ncli: codex\nuse_worktrees: false\n", encoding="utf-8")
    app.state.reload_seed_config()

    seed_path.write_text("goal: second\ncli: codex\nuse_worktrees: true\n", encoding="utf-8")
    app.state.reload_seed_config()

    status_response = await client.get("/status")
    dashboard_response = await client.get("/dashboard/data")

    assert status_response.status_code == 200
    runtime = status_response.json()["runtime"]
    diff = runtime["config_last_diff"]
    assert diff["changed"] is True
    assert any(change["path"] == "goal" and change["after"] == "second" for change in diff["changes"])
    assert any(change["path"] == "use_worktrees" and change["after"] == "True" for change in diff["changes"])

    assert dashboard_response.status_code == 200
    assert dashboard_response.json()["config_last_diff"]["changed"] is True
