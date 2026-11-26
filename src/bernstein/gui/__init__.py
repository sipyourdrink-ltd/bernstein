"""Bernstein web GUI - Vite + React SPA mounted on FastAPI.

Optional component installed via ``pip install bernstein[gui]``.

Public surface:
  - ``mount(app)``       - attach SPA + meta endpoint + PWA assets to FastAPI
  - ``STATIC_DIR``       - path to the built Vite assets
  - ``pwa``              - PWA helpers (manifest builder, SW source, auth tokens)
  - ``qr``               - terminal QR code rendering for onboarding flows

The Python deps are minimal (``sse-starlette`` for streaming endpoints,
which downstream tickets will use). The React build itself is committed
under ``src/bernstein/gui/static/`` so the wheel ships pre-built - no Node
required at install time.

PWA wiring (#1218): the mount also serves a web app manifest, a service
worker, and an offline fallback page from canonical paths under ``/ui/``
and ``/`` so iOS Safari and Android Chrome can install the app to the
home screen.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.gui import pwa, qr

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

__all__ = ["STATIC_DIR", "mount", "pwa", "qr"]

STATIC_DIR = Path(__file__).parent / "static"


def mount(app: FastAPI) -> None:
    """Mount the GUI on a FastAPI app at ``/ui`` and add ``/api/v1/gui-meta``.

    Also serves the PWA assets (manifest, service worker, offline page,
    icons) under the canonical paths Apple / Chromium expect.
    """
    import json

    from fastapi import APIRouter
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
    from fastapi.staticfiles import StaticFiles

    if not STATIC_DIR.exists() or not (STATIC_DIR / "index.html").exists():
        raise RuntimeError(
            f"GUI static assets not found at {STATIC_DIR}. Build them with: `cd web && npm install && npm run build`"
        )
    if getattr(app.state, "_bernstein_gui_mounted", False):
        return
    app.state._bernstein_gui_mounted = True

    # Bidirectional parity: register on BOTH the root app AND under /api/v1/.
    # AUDIT-126's ``test_every_v1_route_has_root_counterpart`` asserts every
    # versioned route has a root mirror. Build two independent ``APIRouter``
    # instances via a local factory so each mount receives its own object -
    # FastAPI's ``include_router`` mutates per-route state on the included
    # instance, so reusing one router for both mounts (or nesting it inside an
    # aggregator that is then mounted) trips python:S8413.
    #
    # NB: ``from __future__ import annotations`` (top of this file) turns every
    # return annotation into a string. FastAPI's OpenAPI builder then tries to
    # resolve ``JSONResponse`` / ``FileResponse`` as response *models* (via
    # pydantic ``TypeAdapter``), fails, and crashes ``/openapi.json`` with a
    # PydanticUserError. Declaring them as ``response_class`` instead - and
    # dropping the return annotation - keeps the endpoint's runtime behaviour
    # identical while signalling to FastAPI that the response is a Starlette
    # ``Response`` subclass that should NOT be schema-modelled.
    def _build_gui_meta_router() -> APIRouter:
        sub_router = APIRouter(tags=["gui"])

        @sub_router.get("/gui-meta", response_class=JSONResponse)
        def gui_meta():  # pyright: ignore[reportUnusedFunction]
            return JSONResponse(
                {
                    "version": _package_version(),
                    "commit": _git_sha(),
                    "build_time": _build_time(),
                    "runner_active": _is_runner_active(app),
                },
            )

        return sub_router

    app.include_router(_build_gui_meta_router())
    app.include_router(_build_gui_meta_router(), prefix="/api/v1")

    assets_dir = STATIC_DIR / "assets"
    if assets_dir.exists():
        app.mount("/ui/assets", StaticFiles(directory=assets_dir), name="gui-assets")

    # ------------------------------------------------------------------
    # PWA assets
    # ------------------------------------------------------------------

    manifest_payload = pwa.build_manifest()

    # Pre-render icons once at mount time - small (a few KB) and the
    # generator is deterministic, so caching the bytes here is a wash
    # vs. a disk read.
    icon_192 = pwa.render_icon_png(192)
    icon_512 = pwa.render_icon_png(512)

    # The closure-defined handlers are registered with FastAPI via the
    # ``@app.get`` decorator; pyright cannot see that traversal so it flags
    # them as ``reportUnusedFunction``. The suppression below matches the
    # pre-existing pattern on ``gui_meta`` / ``gui_index``.

    @app.get("/ui/manifest.webmanifest", include_in_schema=False, response_class=JSONResponse)
    @app.get("/manifest.webmanifest", include_in_schema=False, response_class=JSONResponse)
    def pwa_manifest() -> Response:  # pyright: ignore[reportUnusedFunction]
        # ``application/manifest+json`` is the spec-mandated media type;
        # Chromium logs a warning if served as plain JSON. Encode by hand
        # so we control the media type while keeping the body bit-stable
        # for snapshot tests.
        return Response(
            content=json.dumps(manifest_payload, indent=2, sort_keys=True),
            media_type="application/manifest+json",
        )

    @app.get("/ui/sw.js", include_in_schema=False)
    @app.get("/sw.js", include_in_schema=False)
    def pwa_service_worker() -> Response:  # pyright: ignore[reportUnusedFunction]
        # Service workers must be served with the ``Service-Worker-Allowed``
        # header set to a path at or above the script's own location for
        # the SW to control routes outside its directory. We serve from
        # ``/`` so the scope can be ``/`` (covering both ``/ui`` and the
        # API endpoints we cache).
        return Response(
            content=pwa.SERVICE_WORKER_JS,
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/"},
        )

    @app.get("/ui/offline.html", include_in_schema=False, response_class=HTMLResponse)
    @app.get("/offline.html", include_in_schema=False, response_class=HTMLResponse)
    def pwa_offline() -> HTMLResponse:  # pyright: ignore[reportUnusedFunction]
        return HTMLResponse(content=pwa.OFFLINE_HTML)

    @app.get("/ui/icon-192.png", include_in_schema=False)
    def pwa_icon_192() -> Response:  # pyright: ignore[reportUnusedFunction]
        return Response(content=icon_192, media_type="image/png")

    @app.get("/ui/icon-512.png", include_in_schema=False)
    def pwa_icon_512() -> Response:  # pyright: ignore[reportUnusedFunction]
        return Response(content=icon_512, media_type="image/png")

    @app.get("/ui", include_in_schema=False, response_class=FileResponse)
    @app.get("/ui/{full_path:path}", include_in_schema=False, response_class=FileResponse)
    def gui_index(full_path: str = "") -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        # Client-side routing: every /ui/* request returns index.html unless
        # it's an asset (handled by the StaticFiles mount above).
        # full_path is FastAPI path-capture; consumed by the SPA router, not Python.
        del full_path
        return FileResponse(STATIC_DIR / "index.html")


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("bernstein")
    except Exception:  # pragma: no cover
        return "dev"


def _git_sha() -> str:
    try:
        import subprocess

        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[3],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def _build_time() -> str:
    marker = STATIC_DIR / "index.html"
    if not marker.exists():
        return ""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(marker.stat().st_mtime).isoformat(timespec="seconds")


def _read_pid_file(path: Path) -> int | None:
    """Read an integer PID from *path*, returning None if absent or malformed."""
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _is_pid_alive(pid: int | None) -> bool:
    """Return True when *pid* corresponds to a running process."""
    if pid is None or pid <= 0:
        return False
    try:
        from bernstein.core.config.platform_compat import process_alive
    except ImportError:  # pragma: no cover - minimal GUI wheel without core
        import os

        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    return process_alive(pid)


def _is_runner_active(app: FastAPI) -> bool:
    """Return True when an active task runner is attached to *app*."""
    if bool(getattr(app.state, "readonly", False)):
        return False
    explicit = getattr(app.state, "runner_active", None)
    if isinstance(explicit, bool):
        return explicit
    sdd_dir = getattr(app.state, "sdd_dir", None)
    if not isinstance(sdd_dir, Path):
        return False
    return _is_pid_alive(_read_pid_file(sdd_dir / "runtime" / "spawner.pid"))
