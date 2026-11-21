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


def test_mount_leaves_icon_rendering_until_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[int] = []

    def render(size: int) -> bytes:
        requests.append(size)
        return b"fixture"

    monkeypatch.setattr(pwa, "render_icon_png", render)
    app = FastAPI()
    mount(app)
    assert requests == []
    with TestClient(app) as browser:
        for size in (192, 192, 512):
            assert browser.get(f"/ui/icon-{size}.png").content == b"fixture"
    assert requests == [192, 512]


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
