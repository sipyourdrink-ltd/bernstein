#!/usr/bin/env python3
"""Re-capture the committed browser renders from the SPA bundle that ships today.

``scripts/bind_webui_renders.py`` fails when the bundle moves and the renders do
not, and tells the operator to "re-capture the affected screens". This is the
command that does it, so that step is reproducible rather than a ritual each
person reconstructs.

What it does: boots ``bernstein gui serve`` against a throwaway, empty project
directory, drives headless Chromium over each documented screen, and overwrites
``docs/assets/webui-<screen>.png``.

The empty project directory is the point. The committed renders show the
zero-state - "No tasks yet", ``0 AGENTS · 0 RUNNING`` - which is the only state
that looks the same on every machine. Capturing against a live project would
publish somebody's task titles and paths into the docs.

Two committed renders are deliberately out of scope: ``webui-agents-panel.png``
and ``webui-agents-diffs.png`` show a populated agent panel with real diffs,
which needs a live run to exist. They carry ``adopted`` provenance in
``docs/assets/webui-renders.json``, which is the honest word for "bound to this
bundle without evidence it was captured from it".

Usage::

    python3 scripts/capture_webui_renders.py               # every screen below
    python3 scripts/capture_webui_renders.py tasks costs   # only these

Then rebind, because the renders are bound to a hash of the bundle:

    uv run python scripts/bind_webui_renders.py --update

Requires Playwright and its Chromium (``python -m playwright install chromium``).
Playwright is deliberately not a project dependency - nothing in the wheel or
the test suite drives a browser, and this runs by hand when a UI change lands -
so the interpreter running this script is usually *not* the project venv. The
server is therefore spawned as a separate ``bernstein`` executable
(``--bernstein`` to point at a specific one) rather than as
``sys.executable -m bernstein``, which would require both to live together.
"""

from __future__ import annotations

import argparse
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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = REPO_ROOT / "docs" / "assets"

#: Screen name -> SPA route. The name is the ``webui-<name>.png`` stem, so this
#: mapping is also the list of renders this script claims to own.
SCREENS: dict[str, str] = {
    "tasks": "/ui/tasks",
    "agents": "/ui/agents",
    "approvals": "/ui/approvals",
    "audit": "/ui/audit",
    "costs": "/ui/costs",
    "fleet": "/ui/fleet",
    "settings": "/ui/settings",
}

#: Matches the committed renders. Device scale factor 1 on purpose: a retina
#: capture would quadruple the byte size of every screenshot in the docs.
VIEWPORT = {"width": 1260, "height": 638}

#: The SPA polls after mount, so a screenshot taken at "network idle" can still
#: catch a spinner. This is the settle margin on top of it.
SETTLE_MS = 2_500

BOOT_TIMEOUT_S = 90


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


def capture(screens: dict[str, str], base: str, token: str) -> list[Path]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - operator tooling
        raise SystemExit(
            "Playwright is needed to drive the browser:\n"
            "  python -m pip install playwright && python -m playwright install chromium"
        ) from exc

    with sync_playwright() as playwright, tempfile.TemporaryDirectory(dir=ASSET_DIR, prefix=".staging-") as tmp:
        staging = Path(tmp)
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)

        # The SPA's panels poll the SSO-gated general API, which accepts only the
        # process bearer. `#t=<token>` is the onboarding fragment the SPA already
        # parses on boot (bernstein.gui.pwa.compose_onboarding_url); it keeps the
        # credential out of the server's access log. Without it every panel
        # renders "Unauthorized" and the footer reads API · DEGRADED.
        page.goto(f"{base}/ui/#t={token}", wait_until="networkidle")
        page.wait_for_timeout(SETTLE_MS)

        for name, route in screens.items():
            page.goto(f"{base}{route}", wait_until="networkidle")
            page.wait_for_timeout(SETTLE_MS)
            page.screenshot(path=str(staging / f"webui-{name}.png"))
            print(f"  captured webui-{name}.png")

        browser.close()
        # Only now, with every requested screen in hand - see publish().
        return publish(staging, screens)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "screens",
        nargs="*",
        choices=[*SCREENS, []],
        help=f"screens to re-capture (default: all of {', '.join(SCREENS)})",
    )
    parser.add_argument(
        "--bernstein",
        help="path to the bernstein executable that should serve the GUI (default: this checkout's .venv)",
    )
    args = parser.parse_args(argv)
    selected = {name: SCREENS[name] for name in (args.screens or SCREENS)}
    executable = resolve_bernstein(args.bernstein)

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    token = secrets.token_urlsafe(32)

    with tempfile.TemporaryDirectory(prefix="bernstein-webui-capture-") as workdir:
        log_path = Path(workdir) / "serve.log"
        env = {
            **os.environ,
            # Resolved by create_app() as the general-API bearer, and seeded into
            # the browser below as the same value.
            "BERNSTEIN_AUTH_TOKEN": token,
        }
        print(f"booting `bernstein gui serve` on {base} in an empty project ({workdir})")
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
            written = capture(selected, base, token)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
                process.kill()

    print(
        f"\n{len(written)} render(s) re-captured. They are bound to a hash of the bundle, so rebind:\n"
        "  uv run python scripts/bind_webui_renders.py --update\n"
        "and mark them `captured` in docs/assets/webui-renders.json if they were `adopted`.\n"
        "See docs/contributing/render-freshness.md."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
