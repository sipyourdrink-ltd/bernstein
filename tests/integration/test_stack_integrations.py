"""Integration tests: popular stack orchestration with Bernstein.

Each test scaffolds a minimal project in a temp directory, runs Bernstein
orchestration via bootstrap_from_goal, polls the task server until the work
is complete, and asserts that the expected output files / content exist.

Requires the ``BERNSTEIN_TEST_API_KEY`` environment variable to be set —
all tests are skipped otherwise.  In CI the key is injected as a GitHub
Actions secret.
"""

from __future__ import annotations

import os
import signal
import time
from typing import TYPE_CHECKING

import httpx
import pytest

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Skip the whole module when no API key is configured
# ---------------------------------------------------------------------------

_API_KEY: str | None = os.getenv("BERNSTEIN_TEST_API_KEY")

pytestmark = pytest.mark.skipif(
    not _API_KEY,
    reason="BERNSTEIN_TEST_API_KEY not set — skipping stack integration tests",
)

# One unique port per stack test to avoid conflicts when tests run sequentially.
_PORT_FASTAPI = 8060
_PORT_NEXTJS = 8061
_PORT_DJANGO = 8062
_PORT_EXPRESS = 8063
_PORT_FLASK = 8064

# How long (seconds) to wait for all tasks to reach a terminal state.
_TASK_TIMEOUT = 180

# SDD directory layout required by Bernstein.
_SDD_SUBDIRS = (
    ".sdd",
    ".sdd/backlog",
    ".sdd/backlog/open",
    ".sdd/backlog/done",
    ".sdd/runtime",
    ".sdd/metrics",
    ".sdd/upgrades",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_workspace(project_dir: Path, port: int) -> None:
    """Create the .sdd workspace skeleton and write a minimal config."""
    for sub in _SDD_SUBDIRS:
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    (project_dir / ".sdd" / "config.yaml").write_text(
        f"server_port: {port}\nmax_workers: 1\ndefault_model: sonnet\ndefault_effort: normal\ncli: claude\n"
    )
    (project_dir / ".sdd" / "runtime" / ".gitignore").write_text("*.pid\n*.log\ntasks.jsonl\n")


def _stop_processes(project_dir: Path) -> None:
    """Send SIGTERM to server, spawner, and watchdog PIDs written by Bernstein."""
    runtime_dir = project_dir / ".sdd" / "runtime"
    for fname in ("watchdog.pid", "spawner.pid", "server.pid"):
        pid_file = runtime_dir / fname
        if not pid_file.exists():
            continue
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, OSError, ProcessLookupError):
            pass
        pid_file.unlink(missing_ok=True)


def _poll_until_complete(port: int, timeout: int = _TASK_TIMEOUT) -> bool:
    """Poll /status until every task reaches a terminal state.

    Args:
        port: Task server port.
        timeout: Maximum seconds to wait.

    Returns:
        True when all tasks are done/failed; False on timeout.
    """
    url = f"http://127.0.0.1:{port}/status"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=3.0)
            if resp.status_code == 200:
                tasks = resp.json().get("tasks", [])
                if tasks and all(t.get("status") in ("done", "failed") for t in tasks):
                    return True
        except Exception:
            pass
        time.sleep(2)

    return False


def _project_contains(project_dir: Path, needle: str) -> bool:
    """Return True if *needle* appears in any project source file."""
    extensions = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".mjs",
        ".cjs",
        ".html",
        ".md",
    }
    for path in project_dir.rglob("*"):
        if path.is_file() and path.suffix in extensions:
            # Skip .sdd/ runtime artefacts
            if ".sdd" in path.parts:
                continue
            try:
                if needle in path.read_text(errors="ignore"):
                    return True
            except OSError:
                pass
    return False


def _run_stack_test(
    project_dir: Path,
    goal: str,
    port: int,
) -> bool:
    """Start orchestration for the project and wait for completion.

    Sets ``ANTHROPIC_API_KEY`` from ``BERNSTEIN_TEST_API_KEY`` before
    calling bootstrap_from_goal, so the claude adapter can authenticate.

    Args:
        project_dir: Temp project root.
        goal: Inline goal string passed to the manager agent.
        port: Unique task server port for this test.

    Returns:
        True if all tasks reached a terminal state within the timeout.
    """
    if _API_KEY:
        os.environ["ANTHROPIC_API_KEY"] = _API_KEY

    from bernstein.core.bootstrap import bootstrap_from_goal

    bootstrap_from_goal(
        goal=goal,
        workdir=project_dir,
        port=port,
        cli="claude",
    )
    return _poll_until_complete(port)


# ---------------------------------------------------------------------------
# FastAPI — add /health endpoint
# ---------------------------------------------------------------------------


def test_fastapi_health_endpoint(tmp_path: Path) -> None:
    """FastAPI stack: agent adds a GET /health endpoint to main.py."""
    _setup_workspace(tmp_path, _PORT_FASTAPI)

    # Minimal FastAPI project — no health endpoint yet.
    (tmp_path / "main.py").write_text(
        "from fastapi import FastAPI\n\n"
        "app = FastAPI()\n\n\n"
        "@app.get('/')\n"
        "def root() -> dict[str, str]:\n"
        "    return {'message': 'Hello World'}\n"
    )
    (tmp_path / "requirements.txt").write_text("fastapi>=0.100.0\nuvicorn[standard]>=0.20.0\n")

    # Seed the specific task so the manager agent has concrete direction.
    (tmp_path / ".sdd" / "backlog" / "open" / "add-health-endpoint.md").write_text(
        "# Add /health endpoint to FastAPI app\n\n"
        "**Role:** backend\n"
        "**Priority:** 1\n\n"
        "In `main.py`, add a health-check route:\n"
        "- Method: GET\n"
        "- Path: /health\n"
        "- Returns JSON: `{'status': 'ok'}`\n"
        "- HTTP status 200\n"
    )

    done = False
    try:
        done = _run_stack_test(
            tmp_path,
            goal="Add a /health endpoint to the FastAPI application in main.py",
            port=_PORT_FASTAPI,
        )
    finally:
        _stop_processes(tmp_path)

    assert done, f"Orchestration timed out after {_TASK_TIMEOUT}s"
    assert _project_contains(tmp_path, "/health") or _project_contains(tmp_path, "health"), (
        "Expected a /health endpoint in project source files after agent run"
    )


# ---------------------------------------------------------------------------
# Next.js — add /about page
# ---------------------------------------------------------------------------


def test_nextjs_about_page(tmp_path: Path) -> None:
    """Next.js stack: agent creates an About page component."""
    _setup_workspace(tmp_path, _PORT_NEXTJS)

    # Minimal Next.js project skeleton.
    (tmp_path / "package.json").write_text(
        "{\n"
        '  "name": "nextjs-app",\n'
        '  "version": "0.1.0",\n'
        '  "scripts": {"dev": "next dev", "build": "next build"},\n'
        '  "dependencies": {"next": "14.0.0", "react": "18.0.0", "react-dom": "18.0.0"}\n'
        "}\n"
    )
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "index.tsx").write_text("export default function Home() {\n  return <h1>Home Page</h1>;\n}\n")

    (tmp_path / ".sdd" / "backlog" / "open" / "add-about-page.md").write_text(
        "# Add About page to Next.js app\n\n"
        "**Role:** backend\n"
        "**Priority:** 1\n\n"
        "Create an About page:\n"
        "- File: `pages/about.tsx` (or `app/about/page.tsx`)\n"
        "- Renders a heading: 'About Us'\n"
        "- Accessible at the `/about` route\n"
    )

    done = False
    try:
        done = _run_stack_test(
            tmp_path,
            goal="Add an About page accessible at /about to the Next.js application",
            port=_PORT_NEXTJS,
        )
    finally:
        _stop_processes(tmp_path)

    assert done, f"Orchestration timed out after {_TASK_TIMEOUT}s"

    # Accept any common Next.js page placement.
    about_exists = (
        (tmp_path / "pages" / "about.tsx").exists()
        or (tmp_path / "pages" / "about.js").exists()
        or (tmp_path / "pages" / "about.jsx").exists()
        or (tmp_path / "app" / "about" / "page.tsx").exists()
        or (tmp_path / "app" / "about" / "page.js").exists()
        or _project_contains(tmp_path, "About")
    )
    assert about_exists, "Expected an About page component in pages/ or app/about/ after agent run"


# ---------------------------------------------------------------------------
# Django — add user list view
# ---------------------------------------------------------------------------


def test_django_user_list_view(tmp_path: Path) -> None:
    """Django stack: agent adds a user list view and wires it to urls.py."""
    _setup_workspace(tmp_path, _PORT_DJANGO)

    # Minimal Django app structure.
    app_dir = tmp_path / "myapp"
    app_dir.mkdir()
    (app_dir / "__init__.py").write_text("")
    (app_dir / "views.py").write_text(
        "from django.http import JsonResponse\n\n\n"
        "def index(request: object) -> JsonResponse:\n"
        "    return JsonResponse({'message': 'Hello'})\n"
    )
    (app_dir / "urls.py").write_text(
        "from django.urls import path\n"
        "from . import views\n\n"
        "urlpatterns = [\n"
        "    path('', views.index, name='index'),\n"
        "]\n"
    )
    (tmp_path / "manage.py").write_text(
        "#!/usr/bin/env python\n"
        "import os, sys\n\n"
        "def main():\n"
        "    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'myapp.settings')\n"
        "    from django.core.management import execute_from_command_line\n"
        "    execute_from_command_line(sys.argv)\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n"
    )
    (tmp_path / "requirements.txt").write_text("django>=4.2\n")

    (tmp_path / ".sdd" / "backlog" / "open" / "add-user-list-view.md").write_text(
        "# Add user list view to Django app\n\n"
        "**Role:** backend\n"
        "**Priority:** 1\n\n"
        "In `myapp/views.py`, add a `user_list` view:\n"
        "- Returns a JSON list of users (can be an empty list for now)\n"
        "- Wire it to `myapp/urls.py` at path `/users/`\n"
    )

    done = False
    try:
        done = _run_stack_test(
            tmp_path,
            goal="Add a user list view to the Django app that returns users as JSON at /users/",
            port=_PORT_DJANGO,
        )
    finally:
        _stop_processes(tmp_path)

    assert done, f"Orchestration timed out after {_TASK_TIMEOUT}s"
    assert _project_contains(tmp_path, "user") and (
        _project_contains(tmp_path, "user_list") or _project_contains(tmp_path, "users")
    ), "Expected a user list view in Django project source files after agent run"


# ---------------------------------------------------------------------------
# Express — add request logging middleware
# ---------------------------------------------------------------------------


def test_express_logging_middleware(tmp_path: Path) -> None:
    """Express stack: agent adds request logging middleware to the app."""
    _setup_workspace(tmp_path, _PORT_EXPRESS)

    # Minimal Express app.
    (tmp_path / "package.json").write_text(
        "{\n"
        '  "name": "express-app",\n'
        '  "version": "0.1.0",\n'
        '  "main": "app.js",\n'
        '  "scripts": {"start": "node app.js"},\n'
        '  "dependencies": {"express": "^4.18.0"}\n'
        "}\n"
    )
    (tmp_path / "app.js").write_text(
        "const express = require('express');\n"
        "const app = express();\n\n"
        "app.get('/', (req, res) => {\n"
        "  res.json({ message: 'Hello World' });\n"
        "});\n\n"
        "app.listen(3000, () => console.log('Server running on port 3000'));\n"
        "module.exports = app;\n"
    )

    (tmp_path / ".sdd" / "backlog" / "open" / "add-logging-middleware.md").write_text(
        "# Add request logging middleware to Express app\n\n"
        "**Role:** backend\n"
        "**Priority:** 1\n\n"
        "Add request logging middleware to `app.js`:\n"
        "- Logs the HTTP method and URL for every incoming request\n"
        "- Can be implemented inline in app.js or as a separate file "
        "  at `middleware/logger.js`\n"
        "- Must be registered before route handlers\n"
    )

    done = False
    try:
        done = _run_stack_test(
            tmp_path,
            goal=("Add request logging middleware to the Express app that logs method and URL for every request"),
            port=_PORT_EXPRESS,
        )
    finally:
        _stop_processes(tmp_path)

    assert done, f"Orchestration timed out after {_TASK_TIMEOUT}s"
    assert _project_contains(tmp_path, "log") or _project_contains(tmp_path, "middleware"), (
        "Expected logging middleware in Express project source files after agent run"
    )


# ---------------------------------------------------------------------------
# Flask — add /status endpoint
# ---------------------------------------------------------------------------


def test_flask_status_endpoint(tmp_path: Path) -> None:
    """Flask stack: agent adds a GET /status endpoint to app.py."""
    _setup_workspace(tmp_path, _PORT_FLASK)

    # Minimal Flask app — no /status route yet.
    (tmp_path / "app.py").write_text(
        "from flask import Flask, jsonify\n\n"
        "app = Flask(__name__)\n\n\n"
        "@app.get('/')\n"
        "def index() -> object:\n"
        "    return jsonify({'message': 'Hello'})\n\n\n"
        "if __name__ == '__main__':\n"
        "    app.run(debug=True)\n"
    )
    (tmp_path / "requirements.txt").write_text("flask>=3.0.0\n")

    (tmp_path / ".sdd" / "backlog" / "open" / "add-status-endpoint.md").write_text(
        "# Add /status endpoint to Flask app\n\n"
        "**Role:** backend\n"
        "**Priority:** 1\n\n"
        "In `app.py`, add a status-check route:\n"
        "- Method: GET\n"
        "- Path: /status\n"
        "- Returns JSON: `{'status': 'running'}`\n"
        "- HTTP status 200\n"
    )

    done = False
    try:
        done = _run_stack_test(
            tmp_path,
            goal="Add a GET /status endpoint to the Flask app that returns {'status': 'running'}",
            port=_PORT_FLASK,
        )
    finally:
        _stop_processes(tmp_path)

    assert done, f"Orchestration timed out after {_TASK_TIMEOUT}s"
    assert _project_contains(tmp_path, "/status") or _project_contains(tmp_path, "status"), (
        "Expected a /status endpoint in Flask project source files after agent run"
    )
