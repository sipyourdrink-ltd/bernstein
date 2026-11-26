"""End-to-end-ish integration tests for the GUI + PWA mount (#1218).

The tests build a minimal FastAPI app via the same code path the
``bernstein gui serve --minimal`` command exercises, hit the mounted
endpoints through TestClient, and assert that the PWA assets are
correctly wired (manifest, service worker, offline page, icons).

These tests are integration-flavoured because they go through the
FastAPI routing layer, but they stay in-process - no real network, no
real tunnel binary, no real Vite dev server.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.gui import mount, pwa


@pytest.fixture
def client() -> TestClient:
    app = FastAPI(title="Bernstein", description="GUI integration test")
    mount(app)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def test_manifest_endpoint_returns_200(client: TestClient) -> None:
    r = client.get("/ui/manifest.webmanifest")
    assert r.status_code == 200


def test_manifest_endpoint_uses_correct_media_type(client: TestClient) -> None:
    r = client.get("/ui/manifest.webmanifest")
    assert r.headers["content-type"].startswith("application/manifest+json")


def test_manifest_body_matches_pure_builder(client: TestClient) -> None:
    r = client.get("/ui/manifest.webmanifest")
    body = json.loads(r.text)
    assert body == pwa.build_manifest()


def test_manifest_root_alias_returns_same_payload(client: TestClient) -> None:
    a = client.get("/manifest.webmanifest").json()
    b = client.get("/ui/manifest.webmanifest").json()
    assert a == b


def test_manifest_snapshot(client: TestClient, snapshot: object) -> None:
    """Snapshot the manifest body to lock the public PWA surface."""
    r = client.get("/ui/manifest.webmanifest")
    assert r.text == snapshot  # type: ignore[comparison-overlap]


# ---------------------------------------------------------------------------
# Service worker
# ---------------------------------------------------------------------------


def test_service_worker_returns_200(client: TestClient) -> None:
    r = client.get("/sw.js")
    assert r.status_code == 200


def test_service_worker_media_type(client: TestClient) -> None:
    r = client.get("/sw.js")
    assert r.headers["content-type"].startswith("application/javascript")


def test_service_worker_advertises_root_scope(client: TestClient) -> None:
    r = client.get("/sw.js")
    assert r.headers.get("service-worker-allowed") == "/"


def test_service_worker_under_ui_prefix(client: TestClient) -> None:
    """Same payload from /ui/sw.js for fallback installers."""
    a = client.get("/sw.js").text
    b = client.get("/ui/sw.js").text
    assert a == b


def test_service_worker_body_matches_module_constant(client: TestClient) -> None:
    assert client.get("/sw.js").text == pwa.SERVICE_WORKER_JS


# ---------------------------------------------------------------------------
# Offline page
# ---------------------------------------------------------------------------


def test_offline_html_returns_200(client: TestClient) -> None:
    r = client.get("/ui/offline.html")
    assert r.status_code == 200


def test_offline_html_text_html_content_type(client: TestClient) -> None:
    r = client.get("/ui/offline.html")
    assert r.headers["content-type"].startswith("text/html")


def test_offline_html_body(client: TestClient) -> None:
    assert "Offline" in client.get("/ui/offline.html").text


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------


def test_icon_192_is_png(client: TestClient) -> None:
    r = client.get("/ui/icon-192.png")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")
    assert r.headers["content-type"] == "image/png"


def test_icon_512_is_png(client: TestClient) -> None:
    r = client.get("/ui/icon-512.png")
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")


def test_icons_have_distinct_payloads(client: TestClient) -> None:
    a = client.get("/ui/icon-192.png").content
    b = client.get("/ui/icon-512.png").content
    assert a != b


# ---------------------------------------------------------------------------
# SPA routing still works
# ---------------------------------------------------------------------------


def test_spa_index_html_served_at_ui_root(client: TestClient) -> None:
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_spa_deep_link_returns_index_html(client: TestClient) -> None:
    """Client-side routing - every /ui/<deep>/<link> falls through to index.html."""
    r = client.get("/ui/approvals/some-deep-link")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_gui_meta_returns_json(client: TestClient) -> None:
    r = client.get("/gui-meta")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert "commit" in body
    assert "build_time" in body


def test_gui_meta_versioned_alias(client: TestClient) -> None:
    a = client.get("/gui-meta").json()
    b = client.get("/api/v1/gui-meta").json()
    assert set(a.keys()) == set(b.keys())


# ---------------------------------------------------------------------------
# Full onboarding flow
# ---------------------------------------------------------------------------


def test_full_pwa_assets_available_concurrently(client: TestClient) -> None:
    """Pull every PWA asset and confirm they all return 200."""
    paths = [
        "/ui/manifest.webmanifest",
        "/sw.js",
        "/ui/offline.html",
        "/ui/icon-192.png",
        "/ui/icon-512.png",
        "/ui/",
    ]
    for p in paths:
        r = client.get(p)
        assert r.status_code == 200, f"failed at {p}"


def test_onboarding_token_makes_url_unique() -> None:
    """Two issued URLs differ by token; the base portion is stable."""
    a = pwa.compose_onboarding_url("https://x.example.com", pwa.new_auth_token())
    b = pwa.compose_onboarding_url("https://x.example.com", pwa.new_auth_token())
    assert a.split("#", 1)[0] == b.split("#", 1)[0]
    assert a != b


@pytest.mark.auth_enabled
@pytest.mark.anyio
async def test_e2e_authenticated_spa_sse_and_task_log_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Authenticated create_app()+mount() serves /ui/ publicly and unlocks API, /events, and logs stream."""
    import asyncio
    import time

    from httpx import ASGITransport, AsyncClient

    from bernstein.core.run_auth_token import persist_run_auth_token, read_run_auth_token
    from bernstein.core.server.dashboard_tokens import (
        SCOPE_OPERATOR,
        DashboardTokenRegistry,
        resolve_dashboard_hmac_key,
    )
    from bernstein.core.server.server_app import create_app

    token = "e2e-spa-bearer-token"
    monkeypatch.delenv("BERNSTEIN_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", token)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))

    sdd = tmp_path / ".sdd"
    (sdd / "runtime").mkdir(parents=True)
    persist_run_auth_token(tmp_path, token)
    persisted = read_run_auth_token(tmp_path)
    assert persisted == token

    key = resolve_dashboard_hmac_key(sdd)
    reg = DashboardTokenRegistry(sdd / "auth" / "dashboard_tokens.jsonl", hmac_key=key)
    dash_tok, _ = reg.issue(principal="operator", scope=SCOPE_OPERATOR, now=int(time.time()))

    app = create_app(jsonl_path=sdd / "runtime" / "tasks.jsonl")
    mount(app)
    hdr = {"Authorization": f"Bearer {persisted}"}
    dash_hdr = {"Authorization": f"Bearer {dash_tok}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8052") as c:
        assert (await c.get("/ui/")).status_code == 200
        assert (await c.get("/api/v1/tasks")).status_code == 401
        assert (await c.get("/api/v1/events")).status_code == 401
        assert (await c.get("/api/v1/tasks/missing/logs/stream")).status_code == 401

        assert (await c.get("/api/v1/tasks", headers=hdr)).status_code == 200
        created = await c.post(
            "/api/v1/tasks",
            json={"title": "Log stream task", "description": "Check SPA SSE", "role": "backend"},
            headers=hdr,
        )
        assert created.status_code == 201
        task_id = created.json()["id"]
        assert (await c.post(f"/tasks/{task_id}/claim", json={"agent_id": "a1"}, headers=hdr)).status_code == 200
        assert (
            await c.post(f"/tasks/{task_id}/complete", json={"result_summary": "ok"}, headers=hdr)
        ).status_code == 200

        async with c.stream("GET", "/api/v1/events", headers=hdr) as events_resp:
            assert events_resp.status_code == 200
            assert "text/event-stream" in events_resp.headers.get("content-type", "")
            lines = events_resp.aiter_lines()
            first_line = await anext(lines)
            assert "event: heartbeat" in first_line
            asyncio.get_running_loop().call_later(0.05, app.state.sse_bus.publish, "heartbeat")

        assert (await c.get(f"/api/v1/tasks/{task_id}/logs/stream", headers=dash_hdr)).status_code == 401
        async with c.stream("GET", f"/api/v1/tasks/{task_id}/logs/stream", headers=hdr) as logs_resp:
            assert logs_resp.status_code == 200
            assert "text/event-stream" in logs_resp.headers.get("content-type", "")
            body = (await logs_resp.aread()).decode("utf-8")
            assert "event: complete" in body
