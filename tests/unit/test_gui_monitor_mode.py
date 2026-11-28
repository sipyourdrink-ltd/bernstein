"""Unit tests for GUI runner-active / monitor-only metadata detection."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.server.server_app import (
    create_app as _create_app,  # pyright: ignore[reportUnknownVariableType]
)
from bernstein.gui import mount

_AUTH_HEADERS = {"Authorization": "Bearer monitor-test-token"}


def _build_full_app(tmp_path: Path, *, readonly: bool = False) -> FastAPI:
    jsonl_path = tmp_path / ".sdd" / "tasks" / "tasks.jsonl"
    return _create_app(jsonl_path=jsonl_path, auth_token="monitor-test-token", readonly=readonly)


def test_gui_meta_reports_runner_inactive_in_minimal_mode() -> None:
    app = FastAPI()
    mount(app)
    client = TestClient(app)

    for path in ("/gui-meta", "/api/v1/gui-meta"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert resp.json()["runner_active"] is False


def test_gui_meta_reports_runner_inactive_without_spawner_pid(tmp_path: Path) -> None:
    app = _build_full_app(tmp_path)
    client = TestClient(app)

    resp = client.get("/api/v1/gui-meta", headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["runner_active"] is False


def test_gui_meta_reports_runner_active_when_live_spawner_pid_exists(tmp_path: Path) -> None:
    app = _build_full_app(tmp_path)
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "spawner.pid").write_text(str(os.getpid()), encoding="utf-8")

    client = TestClient(app)
    resp = client.get("/api/v1/gui-meta", headers=_AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["runner_active"] is True


def test_gui_meta_ignores_stale_or_invalid_spawner_pid(tmp_path: Path) -> None:
    app = _build_full_app(tmp_path)
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "spawner.pid").write_text("not-a-pid", encoding="utf-8")

    client = TestClient(app)
    assert client.get("/api/v1/gui-meta", headers=_AUTH_HEADERS).json()["runner_active"] is False

    (runtime_dir / "spawner.pid").write_text("99999999", encoding="utf-8")
    assert client.get("/api/v1/gui-meta", headers=_AUTH_HEADERS).json()["runner_active"] is False


def test_gui_meta_readonly_forces_runner_inactive(tmp_path: Path) -> None:
    app = _build_full_app(tmp_path, readonly=True)
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "spawner.pid").write_text(str(os.getpid()), encoding="utf-8")

    client = TestClient(app)
    assert client.get("/api/v1/gui-meta", headers=_AUTH_HEADERS).json()["runner_active"] is False
