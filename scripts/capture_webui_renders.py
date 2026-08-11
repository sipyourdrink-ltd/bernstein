#!/usr/bin/env python3
"""Re-capture the committed browser renders from the SPA bundle that ships today.

``scripts/bind_webui_renders.py`` fails when the bundle moves and the renders do
not, and tells the operator to "re-capture the affected screens". This is the
command that does it, so that step is reproducible rather than a ritual each
person reconstructs.

What it does: boots ``bernstein gui serve`` against a throwaway project
directory, drives headless Chromium over each documented screen, and overwrites
``docs/assets/webui-<screen>.png``.

Two kinds of screen are captured, from two differently prepared projects:

- **Zero-state screens** (``SCREENS``) come from an *empty* project. The
  committed renders show "No tasks yet", ``0 AGENTS · 0 RUNNING`` - the only
  state that looks the same on every machine. Capturing these against a live
  project would publish somebody's task titles and paths into the docs.
- **Seeded screens** (``SEEDED_SCREENS``) show the populated task board and
  agent panel the front page links to. They come from a throwaway project this
  script seeds itself - a small committed git repo with a working-tree diff,
  and a fixed backlog created through the server's own task API. Nothing in the
  seed derives from the operator's machine, so these renders are as
  reproducible in content (not pixels) as the zero-state ones, and no personal
  path or project can leak into the frame.

The run is all-or-nothing across *both* groups: screens are staged and
published only once every requested one succeeds. A run that dies half-way
would otherwise leave some screens from today's bundle and the rest from
whenever they were last taken, and ``--update`` preserves each render's prior
provenance - so the untouched ones would keep the word ``captured`` while bound
to a bundle they were never captured from.

Usage::

    python3 scripts/capture_webui_renders.py                    # every screen
    python3 scripts/capture_webui_renders.py tasks costs        # only these
    python3 scripts/capture_webui_renders.py agents-panel       # seeded only

Then rebind, because the renders are bound to a hash of the bundle:

    uv run python scripts/bind_webui_renders.py --update

and mark anything newly captured ``captured`` in
``docs/assets/webui-renders.json`` - the binding gate fails while a render
carries ``adopted``.

Requires Playwright and its Chromium (``python -m playwright install chromium``).
Playwright is deliberately not a project dependency - nothing in the wheel or
the test suite drives a browser, and this runs by hand when a UI change lands -
so the interpreter running this script is usually *not* the project venv. The
server is therefore spawned as a separate ``bernstein`` executable
(``--bernstein`` to point at a specific one) rather than as
``sys.executable -m bernstein``, which would require both to live together.
When Pillow is importable each published image is palette-quantised, which
roughly halves its size at no visible cost on these flat UI surfaces.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = REPO_ROOT / "docs" / "assets"

#: Screen name -> SPA route, captured against an *empty* project. The name is
#: the ``webui-<name>.png`` stem, so this mapping (with ``SEEDED_SCREENS``) is
#: also the list of renders this script claims to own.
SCREENS: dict[str, str] = {
    "tasks": "/ui/tasks",
    "agents": "/ui/agents",
    "approvals": "/ui/approvals",
    "audit": "/ui/audit",
    "costs": "/ui/costs",
    "missions": "/ui/missions",
    "fleet": "/ui/fleet",
    "settings": "/ui/settings",
}

#: Screen name -> SPA route, captured against the *seeded* project: the
#: populated task board with the diff drawer open, and the agent panel the
#: front page links to. Kept separate from ``SCREENS`` because they need a
#: different project, a larger viewport and per-screen interaction.
SEEDED_SCREENS: dict[str, str] = {
    "agents-panel": "/ui/agents",
    "agents-diffs": "/ui/tasks",
}

#: Matches the committed renders. Device scale factor 1 on purpose: a retina
#: capture would quadruple the byte size of every screenshot in the docs.
VIEWPORT = {"width": 1260, "height": 638}

#: The seeded screens are the front page's feature renders and match the
#: dimensions they were first published at.
SEEDED_VIEWPORT = {"width": 1712, "height": 1490}

#: The SPA polls after mount, so a screenshot taken at "network idle" can still
#: catch a spinner. This is the settle margin on top of it.
SETTLE_MS = 2_500

BOOT_TIMEOUT_S = 90

#: The seeded backlog. Two build tasks and sixty dependency upgrades - 62
#: tasks, of which one is completed and eleven are claimed, so the task board
#: reads "62 tasks · 11 running" and the agent panel shows eleven sessions.
#: Fixed here so a re-capture publishes the same content every time.
SEEDED_BUILD_TASKS: tuple[tuple[str, str], ...] = (
    ("Build a multi-module COBOL banking ledger system (gemini-flash)", "qa"),
    ("Build a multi-module COBOL payroll system (claude-haiku)", "qa"),
)
SEEDED_DEPENDENCY_UPGRADES: tuple[str, ...] = (
    "aiohttp",
    "banks",
    "black",
    "brotli",
    "cryptography",
    "diskcache",
    "ecdsa",
    "fastmcp",
    "flask",
    "fonttools",
    "gitpython",
    "gradio",
    "h2",
    "httpx",
    "jinja2",
    "jaraco-context",
    "langchain-core",
    "langchain-openai",
    "langchain-text-splitters",
    "langgraph",
    "langgraph-checkpoint",
    "langgraph-checkpoint-sqlite",
    "langsmith",
    "llama-index-core",
    "lxml",
    "markdown",
    "marshmallow",
    "mistune",
    "numpy",
    "oauthlib",
    "openpyxl",
    "orjson",
    "packaging",
    "pandas",
    "paramiko",
    "pdfminer-six",
    "pillow",
    "protobuf",
    "pyarrow",
    "pyasn1",
    "pycryptodome",
    "pydantic",
    "pygments",
    "pyjwt",
    "pyopenssl",
    "pypdf",
    "python-multipart",
    "pyyaml",
    "redis",
    "requests",
    "rich",
    "scikit-learn",
    "sqlalchemy",
    "starlette",
    "tornado",
    "tqdm",
    "twisted",
    "urllib3",
    "werkzeug",
    "xgrammar",
)
SEEDED_CLAIMED = 11


def resolve_bernstein(explicit: str | None) -> str:
    """Find the ``bernstein`` that should serve the GUI.

    Prefers the project venv over whatever is on PATH: a globally-installed
    Bernstein would serve a *different* bundle, and this script exists to
    photograph the one in this checkout.
    """
    if explicit:
        return explicit
    venv = REPO_ROOT / ".venv" / "bin" / "bernstein"
    if venv.is_file():
        return str(venv)
    found = shutil.which("bernstein")
    if not found:
        raise SystemExit("no `bernstein` executable found. Run `uv sync` in this checkout, or pass --bernstein <path>.")
    print(f"warning: using {found} from PATH; it may serve a different bundle than this checkout")
    return found


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_boot(url: str, process: subprocess.Popen[bytes], log: Path) -> None:
    """Block until the GUI answers, or explain why it never will."""
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"`bernstein gui serve` exited with {process.returncode}:\n{log.read_text()}")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.5)
    raise SystemExit(f"the GUI did not answer on {url} within {BOOT_TIMEOUT_S}s:\n{log.read_text()}")


@contextlib.contextmanager
def gui_server(
    executable: str,
    workdir: Path,
    token: str,
    dashboard_password: str | None = None,
) -> Iterator[str]:
    """Boot ``bernstein gui serve`` in *workdir* and yield its base URL.

    ``dashboard_password`` configures dashboard auth for the seeded captures:
    the diff drawer reads the ``/api/v1/dashboard/*`` mirror, which the
    general-API bearer does not unlock, so the capture logs a dashboard
    session in with this password instead.
    """
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = workdir / "serve.log"
    env = {
        **os.environ,
        # Resolved by create_app() as the general-API bearer, and seeded into
        # the browser as the same value.
        "BERNSTEIN_AUTH_TOKEN": token,
    }
    if dashboard_password is not None:
        env["BERNSTEIN_DASHBOARD_PASSWORD"] = dashboard_password
    print(f"booting `bernstein gui serve` on {base} in {workdir}")
    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            [executable, "gui", "serve", "--no-open", "--host", "127.0.0.1", "--port", str(port)],
            cwd=workdir,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    try:
        wait_for_boot(f"{base}/ui/", process, log_path)
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            process.kill()


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _git(project_dir: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Bernstein Demo", "-c", "user.email=demo@example.invalid", *args],
        cwd=project_dir,
        check=True,
        capture_output=True,
    )


def seed_project(project_dir: Path) -> None:
    """Write the committed demo repo whose working-tree diff the drawer shows.

    A small Flask service, committed, then edited in place - so the drawer's
    "working tree against HEAD" view has real hunks to draw. Every byte is
    written here: nothing is copied from the operator's machine, so nothing
    personal can appear in the published render.
    """
    (project_dir / "app.py").write_text(
        '"""A small task-queue service used to seed the documentation renders."""\n'
        "from flask import Flask, jsonify\n"
        "\n"
        "app = Flask(__name__)\n"
        "\n"
        'QUEUES = ["default", "priority", "batch"]\n'
        "\n"
        "\n"
        '@app.route("/")\n'
        "def index() -> object:\n"
        '    """List the queues this service schedules."""\n'
        '    return jsonify({"queues": QUEUES, "status": "ok"})\n'
        "\n"
        "\n"
        '@app.route("/queues/<name>")\n'
        "def queue_depth(name: str) -> object:\n"
        '    """Report the depth of one queue."""\n'
        "    if name not in QUEUES:\n"
        '        return jsonify({"error": "unknown queue"}), 404\n'
        '    return jsonify({"queue": name, "depth": 0})\n'
        "\n"
        "\n"
        '@app.route("/health")\n'
        "def health() -> object:\n"
        '    """Liveness probe."""\n'
        '    return jsonify({"status": "healthy"})\n'
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    app.run()\n"
    )
    (project_dir / "requirements.txt").write_text("flask>=3.0.0\ngunicorn>=21.2.0\npytest>=8.0.0\n")
    tests_dir = project_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text(
        '"""Tests for the seeded task-queue service."""\n'
        "from app import app as flask_app\n"
        "\n"
        "\n"
        "def test_index_lists_queues() -> None:\n"
        "    client = flask_app.test_client()\n"
        '    payload = client.get("/").get_json()\n'
        '    assert payload["status"] == "ok"\n'
        '    assert "default" in payload["queues"]\n'
        "\n"
        "\n"
        "def test_unknown_queue_is_a_404() -> None:\n"
        "    client = flask_app.test_client()\n"
        '    assert client.get("/queues/nope").status_code == 404\n'
    )
    _git(project_dir, "init", "--quiet")
    _git(project_dir, "add", "-A")
    _git(project_dir, "commit", "--quiet", "-m", "Initial service")

    # The working-tree edits the diff drawer renders: a new endpoint, its
    # test, and a dependency bump.
    app_py = project_dir / "app.py"
    app_py.write_text(
        app_py.read_text() + "\n" + '\n@app.route("/version")\n'
        "def version() -> object:\n"
        '    """Report the running service version."""\n'
        '    return jsonify({"version": "1.1.0"})\n'
    )
    test_py = tests_dir / "test_app.py"
    test_py.write_text(
        test_py.read_text() + "\n" + "\ndef test_version_is_reported() -> None:\n"
        "    client = flask_app.test_client()\n"
        '    assert client.get("/version").get_json() == {"version": "1.1.0"}\n'
    )
    requirements = project_dir / "requirements.txt"
    requirements.write_text(requirements.read_text().replace("flask>=3.0.0", "flask>=3.1.0"))


def _api(base: str, token: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    request = urllib.request.Request(
        f"{base}/api/v1{path}",
        data=json.dumps(body).encode() if body is not None else b"",
        method=method,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}")


def seed_backlog(base: str, token: str) -> None:
    """Create the fixed backlog through the server's own task API.

    Going through the API rather than writing store files keeps the seed on
    the same code path a real orchestrator uses - statuses are legal
    transitions, and the agent panel's synthetic sessions derive from claimed
    tasks exactly as they do in production.
    """
    created: list[str] = []
    for title, role in SEEDED_BUILD_TASKS:
        task = _api(base, token, "POST", "/tasks", {"title": title, "description": title, "role": role})
        created.append(task["id"])
    for name in SEEDED_DEPENDENCY_UPGRADES:
        title = f"Upgrade vulnerable dependency: {name}"
        task = _api(base, token, "POST", "/tasks", {"title": title, "description": title, "role": "security"})
        created.append(task["id"])

    # The second build task plus the first dependency upgrades stay claimed -
    # these are the panel's sessions - and the first build task completes.
    for task_id in [created[1], *created[2 : 1 + SEEDED_CLAIMED]]:
        _api(base, token, "POST", f"/tasks/{task_id}/claim")
    _api(base, token, "POST", f"/tasks/{created[0]}/claim")
    _api(
        base,
        token,
        "POST",
        f"/tasks/{created[0]}/complete",
        {"result_summary": "Ledger, posting and reconciliation modules implemented; acceptance tests pass."},
    )
    print(f"  seeded {len(created)} tasks ({SEEDED_CLAIMED} claimed, 1 done)")


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def optimize(png: Path) -> None:
    """Palette-quantise *png* in place when Pillow is available.

    These are flat UI surfaces; 256 colours reproduce them with no visible
    difference at roughly half the bytes. Best-effort on purpose: the capture
    must not fail on an interpreter without Pillow.
    """
    try:
        from PIL import Image

        quantised = Image.open(png).convert("RGB").quantize(colors=256, dither=Image.Dither.NONE)
        quantised.save(png, optimize=True)
    except (ImportError, OSError):
        return


def publish(staged: Path, names: Iterable[str]) -> list[Path]:
    """Move staged captures over the committed renders.

    Called only once every requested screen has been captured, so a run that
    dies part-way leaves ``docs/assets`` untouched. Overwriting each render as
    its screenshot lands would leave a mixed set - some screens from today's
    bundle, the rest from whenever they were last taken - and the documented
    next step (``bind_webui_renders.py --update``) preserves prior provenance,
    so the stale ones would keep the word ``captured`` while bound to a bundle
    they were never captured from. That is the one claim this gate exists to
    make, so it must not be a half-finished run away from being false.

    The staging directory lives inside ``docs/assets`` so this is a rename on
    one filesystem: :func:`os.replace` is atomic per file, and across
    filesystems it would not work at all.
    """
    published: list[Path] = []
    for name in names:
        target = ASSET_DIR / f"webui-{name}.png"
        os.replace(staged / target.name, target)
        published.append(target)
    return published


def _authenticate(page: Any, base: str, token: str) -> None:
    # The SPA's panels poll the SSO-gated general API, which accepts only the
    # process bearer. `#t=<token>` is the onboarding fragment the SPA already
    # parses on boot (bernstein.gui.pwa.compose_onboarding_url); it keeps the
    # credential out of the server's access log. Without it every panel
    # renders "Unauthorized" and the footer reads API · DEGRADED.
    # The authenticated SSE reader intentionally keeps a fetch pending for the
    # life of the page, so networkidle would never resolve after #3563.
    page.goto(f"{base}/ui/#t={token}", wait_until="domcontentloaded")
    page.wait_for_timeout(SETTLE_MS)


def _stage_screens(page: Any, screens: dict[str, str], base: str, staging: Path) -> None:
    for name, route in screens.items():
        page.goto(f"{base}{route}", wait_until="domcontentloaded")
        page.wait_for_timeout(SETTLE_MS)
        if name == "agents-diffs":
            # Drawer on the completed build task, Diff tab: the working-tree
            # diff of the seeded repo.
            page.get_by_text("COBOL banking ledger", exact=False).first.click()
            page.wait_for_timeout(1_000)
            page.get_by_role("button", name="Diff", exact=True).click()
            page.wait_for_timeout(SETTLE_MS)
        shot = staging / f"webui-{name}.png"
        page.screenshot(path=str(shot))
        optimize(shot)
        print(f"  captured webui-{name}.png")


def capture(screens: dict[str, str], base: str, token: str, staging: Path | None = None) -> list[Path]:
    """Capture zero-state *screens* against a served empty project.

    With no *staging*, captures are staged privately and published before
    returning - the single-group behaviour. When *staging* is supplied the
    screenshots are staged there and publishing is the caller's job, which is
    how a zero-state and a seeded group land atomically in one publish.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - operator tooling
        raise SystemExit(
            "Playwright is needed to drive the browser:\n"
            "  python -m pip install playwright && python -m playwright install chromium"
        ) from exc

    with contextlib.ExitStack() as stack:
        playwright = stack.enter_context(sync_playwright())
        if staging is None:
            target = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=ASSET_DIR, prefix=".staging-")))
        else:
            target = staging
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
            _authenticate(page, base, token)
            _stage_screens(page, screens, base, target)
        finally:
            browser.close()
        if staging is not None:
            return [target / f"webui-{name}.png" for name in screens]
        return publish(target, screens)


def capture_seeded(
    screens: dict[str, str],
    base: str,
    token: str,
    dashboard_password: str,
    staging: Path | None = None,
) -> list[Path]:
    """Capture seeded *screens* against a server whose backlog was seeded.

    Same staging contract as :func:`capture`. The extra step is a dashboard
    login: the diff drawer reads the ``/api/v1/dashboard/*`` mirror, which a
    general-API bearer does not unlock, so the browser opens a session with
    the password the server was booted with.
    """
    from playwright.sync_api import sync_playwright

    with contextlib.ExitStack() as stack:
        playwright = stack.enter_context(sync_playwright())
        if staging is None:
            target = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=ASSET_DIR, prefix=".staging-")))
        else:
            target = staging
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport=SEEDED_VIEWPORT, device_scale_factor=1)
            _authenticate(page, base, token)
            status = page.evaluate(
                """async (password) => {
                    const r = await fetch('/api/v1/dashboard/auth/login', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({password}),
                    });
                    return r.status;
                }""",
                dashboard_password,
            )
            if status != 200:
                raise SystemExit(f"dashboard login failed with HTTP {status}; the diff drawer would render a 401")
            _stage_screens(page, screens, base, target)
        finally:
            browser.close()
        if staging is not None:
            return [target / f"webui-{name}.png" for name in screens]
        return publish(target, screens)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "screens",
        nargs="*",
        choices=[*SCREENS, *SEEDED_SCREENS, []],
        help=f"screens to re-capture (default: all of {', '.join((*SCREENS, *SEEDED_SCREENS))})",
    )
    parser.add_argument(
        "--bernstein",
        help="path to the bernstein executable that should serve the GUI (default: this checkout's .venv)",
    )
    args = parser.parse_args(argv)
    requested = args.screens or [*SCREENS, *SEEDED_SCREENS]
    zero_state = {name: SCREENS[name] for name in requested if name in SCREENS}
    seeded = {name: SEEDED_SCREENS[name] for name in requested if name in SEEDED_SCREENS}
    executable = resolve_bernstein(args.bernstein)
    token = secrets.token_urlsafe(32)

    written: list[Path] = []
    with tempfile.TemporaryDirectory(dir=ASSET_DIR, prefix=".staging-") as tmp:
        staging = Path(tmp)
        if zero_state:
            with (
                tempfile.TemporaryDirectory(prefix="bernstein-webui-capture-") as workdir,
                gui_server(executable, Path(workdir), token) as base,
            ):
                capture(zero_state, base, token, staging=staging)
        if seeded:
            dashboard_password = secrets.token_urlsafe(32)
            with tempfile.TemporaryDirectory(prefix="bernstein-webui-capture-seeded-") as workdir:
                seed_project(Path(workdir))
                with gui_server(executable, Path(workdir), token, dashboard_password=dashboard_password) as base:
                    seed_backlog(base, token)
                    capture_seeded(seeded, base, token, dashboard_password, staging=staging)
        # Only now, with every requested screen in hand - see publish().
        written = publish(staging, [*zero_state, *seeded])

    print(
        f"\n{len(written)} render(s) re-captured. They are bound to a hash of the bundle, so rebind:\n"
        "  uv run python scripts/bind_webui_renders.py --update\n"
        "and mark them `captured` in docs/assets/webui-renders.json if they were `adopted`.\n"
        "See docs/contributing/render-freshness.md."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
