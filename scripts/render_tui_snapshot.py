#!/usr/bin/env python3
"""Render the `bernstein live` dashboard from a frozen fixture, and gate it.

The repository publishes renders of its operator surfaces. Nothing checked
whether any of them still matched what the code draws, and nothing would have
noticed when they stopped - a screenshot is committed once and then quietly
describes a version of the tool that no longer exists.

This closes that for the terminal surface, the half that is deterministically
reproducible. The dashboard is driven headless against a committed fixture -
a real frame captured from a `bernstein demo` run, with the clock pinned so
every elapsed-time cell renders identically - and exported as SVG through
Textual's own screenshot path. The export is byte-stable, so the committed
render can be compared exactly rather than approximately.

Usage::

    python scripts/render_tui_snapshot.py           # verify, exit 1 on drift
    python scripts/render_tui_snapshot.py --update  # regenerate the render

On drift the report names the region that changed - AGENTS, TASKS, ACTIVITY -
rather than reporting a byte mismatch, because "the SVG differs" tells the
reader nothing about what to look at.

The browser surface is not reproducible this way (font hinting, antialiasing
and GPU compositing all move the pixels), and is gated differently - see
``scripts/bind_webui_renders.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import html
import json
import re
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "tui_live_frame.json"
RENDER = REPO_ROOT / "docs" / "assets" / "tui-live.svg"

#: The front page's terminal render: the same gated SVG, rasterised. GitHub's
#: README pipeline is where the render is actually seen, and an SVG that
#: declares webfonts renders differently across viewers there - a PNG does
#: not. The PNG cannot be byte-gated (rasterisation moves pixels between
#: machines), so it is bound to the SHA-256 of the SVG it was rasterised from
#: instead: when the gated SVG moves and the PNG does not, verification fails.
PNG_RENDER = REPO_ROOT / "docs" / "assets" / "tui-agents.png"
PNG_BINDING = REPO_ROOT / "docs" / "assets" / "tui-renders.json"

#: Terminal geometry the render is taken at. Committed with the render because
#: changing it changes every line of the output.
TERMINAL_SIZE = (120, 40)

#: Panel titles the dashboard draws. Each is centred over its panel, so their
#: coordinates are what a changed cell is attributed against.
REGION_TITLES = ("AGENTS", "TASKS", "ACTIVITY")

_TEXT_NODE = re.compile(r"<text[^>]*\bx=\"([\d.]+)\"[^>]*\by=\"([\d.]+)\"[^>]*>([^<]*)</text>")

#: Rich namespaces every id and CSS class in an exported SVG with a per-export
#: number that is not stable across processes. It carries no information about
#: what was drawn - two renders of the same screen differ in nothing else - so
#: it is pinned to a constant, which is what makes byte comparison meaningful.
_EXPORT_ID = re.compile(r"terminal-\d+")
_STABLE_ID = "terminal-bernstein-live"

#: Rich's SVG export declares its own webfont and points it at a CDN. A
#: published asset that fetches from a third party on view tells that third
#: party who is reading the docs, and lets them change how the committed render
#: looks without the committed bytes changing. The ``local()`` source and the
#: ``monospace`` fallback in the stylesheet cover rendering; every glyph carries
#: its own x coordinate, so layout does not depend on the metrics either way.
_REMOTE_FONT_SRC = re.compile(r',\s*\n\s*url\("https?://[^"]+"\) format\("[^"]+"\)')

#: Rich writes one clip path per terminal row, each a rect on a uniform grid.
_CLIP_DEF = re.compile(
    r'<clipPath id="' + _STABLE_ID + r'-line-(\d+)">\s*\n\s*'
    r'<rect x="([\d.]+)" y="([\d.]+)" width="([\d.]+)" height="([\d.]+)"/>'
)
_CLIP_REF = re.compile(r'clip-path="url\(#' + _STABLE_ID + r'-line-(\d+)\)"')


def _display(path: Path) -> str:
    """Path as the reader knows it: repo-relative when it is inside the repo."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _load_fixture() -> tuple[dict[str, object], float]:
    """Return the frozen dashboard payload and the instant it was captured."""
    payload: dict[str, object] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    frozen_now = payload.pop("frozen_now")
    assert isinstance(frozen_now, (int, float)), "fixture must pin the clock via frozen_now"
    return payload, float(frozen_now)


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` is the capture instant.

    The activity log stamps each line with the wall clock at the moment it is
    written (``bernstein.tui.agent_log``), which is both machine-local and
    timezone-local: without this the render differs between two runs seconds
    apart, and between a laptop and a UTC CI runner.
    """

    _frozen: ClassVar[float] = 0.0

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return datetime.fromtimestamp(cls._frozen, tz=tz or UTC)


async def _export(payload: dict[str, object], frozen_now: float) -> str:
    """Drive the dashboard headless against *payload* and export its screen."""
    import bernstein.cli.dashboard_app as dashboard_app
    import bernstein.tui.agent_log as agent_log

    # The app polls through this one function, so replacing it is the whole
    # fixture seam - no HTTP, no server, no task store.
    def fetch() -> dict[str, object]:
        return json.loads(json.dumps(payload))

    _FrozenDatetime._frozen = frozen_now

    # ``time.strftime()`` with no time tuple reads the C-level clock, which
    # neither the ``time.time`` patch nor the frozen datetime reaches. Bind the
    # real implementation first: ``datetime.strftime`` delegates back to this
    # module attribute, so a replacement that formats a datetime recurses.
    real_strftime = time.strftime
    frozen_struct = time.gmtime(frozen_now)

    def frozen_strftime(fmt: str, when: time.struct_time | None = None) -> str:
        return real_strftime(fmt, when if when is not None else frozen_struct)

    with (
        patch.object(dashboard_app, "_fetch_all", fetch),
        patch.object(agent_log, "datetime", _FrozenDatetime),
        patch("time.strftime", frozen_strftime),
        patch("time.time", lambda: frozen_now),
    ):
        app = dashboard_app.BernsteinApp()
        async with app.run_test(size=TERMINAL_SIZE) as pilot:
            # One pause starts the poll worker; the rest let its result land
            # and the widgets repaint before the screen is captured.
            for _ in range(6):
                await pilot.pause()
            return app.export_screenshot()


def drop_remote_fonts(svg: str) -> str:
    """Remove the CDN font sources Rich writes into every export."""
    return _REMOTE_FONT_SRC.sub("", svg)


def complete_line_clips(svg: str) -> str:
    """Define the clip paths the export references but never declares.

    Rich emits one clipPath per terminal row but numbers the footer row one
    past the last definition, so the exported SVG carries a ``clip-path``
    pointing at an id that does not exist. A renderer resolves that by not
    clipping at all, which is how a footer overflows the terminal frame in the
    published asset while looking correct in the terminal it came from.

    The rows are a uniform grid, so a missing one is derived from the two
    before it rather than guessed.
    """
    defined = {int(match.group(1)): match for match in _CLIP_DEF.finditer(svg)}
    referenced = {int(match.group(1)) for match in _CLIP_REF.finditer(svg)}
    missing = sorted(referenced - set(defined))
    if not missing or len(defined) < 2:
        return svg

    last, previous = max(defined), max(defined) - 1
    if previous not in defined:
        return svg
    _, x, y_last, width, height = defined[last].groups()
    step = round(float(y_last) - float(defined[previous].group(3)), 4)

    additions = []
    for index in missing:
        y = round(float(y_last) + step * (index - last), 4)
        additions.append(
            f'<clipPath id="{_STABLE_ID}-line-{index}">\n'
            f'    <rect x="{x}" y="{y}" width="{width}" height="{height}"/>\n'
            f"            </clipPath>"
        )
    anchor = defined[last].group(0)
    tail = "\n            </clipPath>"
    return svg.replace(anchor + tail, anchor + tail + "\n" + "\n".join(additions), 1)


def render() -> str:
    """Return the SVG the current code draws for the committed fixture."""
    payload, frozen_now = _load_fixture()
    exported = _EXPORT_ID.sub(_STABLE_ID, asyncio.run(_export(payload, frozen_now)))
    return complete_line_clips(drop_remote_fonts(exported))


def text_layer(svg: str) -> list[str]:
    """Reconstruct the visible text of *svg*, one entry per terminal row.

    Rows are recovered from the y coordinate of each text node and ordered by
    x, which is enough to say what changed and where. This is a reading aid
    for the diff, not the comparison itself - the comparison is on the bytes.
    """
    rows: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for x, y, content in _TEXT_NODE.findall(svg):
        # Rich pads cells with non-breaking spaces so the SVG keeps its
        # columns; they read as ordinary spacing and are folded back here so
        # the diff shows text a reader recognises.
        rows[y].append((float(x), html.unescape(content).replace("\xa0", " ")))
    ordered = sorted(rows.items(), key=lambda item: float(item[0]))
    return ["".join(text for _, text in sorted(runs)).rstrip() for _, runs in ordered]


def cells(svg: str) -> dict[tuple[float, float], str]:
    """Return the drawn text keyed by (row y, column x).

    The dashboard puts AGENTS and TASKS side by side, so a terminal row crosses
    both panels: attributing a change to a region by row alone reports the
    left-hand panel for a change in the right-hand one. Keeping the column
    keeps that answer honest.
    """
    return {
        (float(y), float(x)): html.unescape(content).replace("\xa0", " ") for x, y, content in _TEXT_NODE.findall(svg)
    }


def _anchors(drawn: dict[tuple[float, float], str]) -> dict[str, tuple[float, float]]:
    """Locate each panel title, which is what the regions are measured from."""
    found: dict[str, tuple[float, float]] = {}
    for (y, x), text in drawn.items():
        for title in REGION_TITLES:
            if title in text and title not in found:
                found[title] = (y, x)
    return found


def regions_changed(committed: str, current: str) -> list[str]:
    """Name the regions whose drawn cells differ between two renders."""
    before, after = cells(committed), cells(current)
    anchors = _anchors(before or after)
    activity_y = anchors.get("ACTIVITY", (float("inf"), 0.0))[0]
    panels_y = min(anchors.get("AGENTS", (0.0, 0.0))[0], anchors.get("TASKS", (0.0, 0.0))[0])
    # Each panel title is centred over its own panel, so the divider between
    # the two is halfway between the titles. Taking the TASKS title's own x
    # would put most of the tasks table on the AGENTS side of the line.
    agents_title_x = anchors.get("AGENTS", (0.0, 0.0))[1]
    tasks_title_x = anchors.get("TASKS", (0.0, float("inf")))[1]
    divider_x = (agents_title_x + tasks_title_x) / 2

    named: list[str] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        y, x = key
        if y >= activity_y:
            region = "ACTIVITY"
        elif y < panels_y:
            region = "header"
        else:
            region = "TASKS" if x >= divider_x else "AGENTS"
        if region not in named:
            named.append(region)
    return named


def drift_report(committed: str, current: str) -> str:
    """Describe the drift between two renders in terms a reader can act on."""
    before, after = text_layer(committed), text_layer(current)
    names = regions_changed(committed, current) or ["(no visible text changed - styling or geometry moved)"]
    diff = difflib.unified_diff(before, after, "committed render", "current render", lineterm="", n=1)
    return "\n".join([f"changed regions: {', '.join(names)}", "", *list(diff)[:60]])


def svg_digest() -> str:
    """SHA-256 of the committed SVG - what the PNG binding records."""
    return hashlib.sha256(RENDER.read_bytes()).hexdigest()


def verify_png_binding() -> list[str]:
    """Return problems with the rasterised front-page render, if any.

    Pure file reads on purpose: this runs in the same CI gate as the SVG
    comparison, which has no browser. Rasterising needs one
    (``--rasterize``), noticing a stale raster does not.
    """
    if not PNG_RENDER.exists():
        # Deleting the asset is not a way to pass: the README and both
        # translated front pages link it by raw URL, so an absent file is a
        # broken image on the project's front page rather than one less thing
        # to check.
        return [
            f"{_display(PNG_RENDER)} is missing, and the README front pages link it; "
            "restore it with: python3 scripts/render_tui_snapshot.py --rasterize"
        ]
    if not PNG_BINDING.exists():
        return [f"{_display(PNG_RENDER)} is committed but {_display(PNG_BINDING)} does not bind it; run --rasterize"]
    binding = json.loads(PNG_BINDING.read_text(encoding="utf-8"))
    recorded = binding.get(PNG_RENDER.name, {}).get("source_sha256")
    current = svg_digest()
    if recorded != current:
        return [
            f"{_display(PNG_RENDER)} was rasterised from a different {_display(RENDER)} "
            "than the one committed, so the front page shows a dashboard that no longer exists.\n"
            f"  rasterised from: {recorded}\n"
            f"  committed SVG:   {current}\n"
            "  Re-rasterise with: python3 scripts/render_tui_snapshot.py --rasterize"
        ]
    return []


def rasterize() -> int:
    """Rasterise the committed SVG to ``PNG_RENDER`` and bind it.

    Runs under an interpreter with Playwright (the same one
    ``capture_webui_renders.py`` uses), which is usually not the project venv;
    everything here after the screenshot is stdlib plus optional Pillow.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - operator tooling
        raise SystemExit(
            "Playwright is needed to rasterise the SVG:\n"
            "  python -m pip install playwright && python -m playwright install chromium"
        ) from exc

    match = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', RENDER.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"{_display(RENDER)} carries no viewBox to size the raster from")
    width, height = (int(float(group)) for group in match.groups())

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        page.goto(RENDER.resolve().as_uri())
        page.wait_for_timeout(1_000)
        page.screenshot(path=str(PNG_RENDER), omit_background=True)
        browser.close()

    try:
        from PIL import Image

        image = Image.open(PNG_RENDER)
        image.quantize(colors=256, dither=Image.Dither.NONE).save(PNG_RENDER, optimize=True)
    except ImportError:
        pass

    PNG_BINDING.write_text(
        json.dumps({PNG_RENDER.name: {"source": RENDER.name, "source_sha256": svg_digest()}}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {_display(PNG_RENDER)} ({PNG_RENDER.stat().st_size} bytes) bound to {_display(RENDER)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true", help="rewrite the committed render")
    parser.add_argument(
        "--rasterize",
        action="store_true",
        help="re-rasterise the committed SVG to the front page's PNG (needs Playwright)",
    )
    args = parser.parse_args(argv)

    if args.rasterize:
        return rasterize()

    current = render()
    if args.update:
        RENDER.write_text(current, encoding="utf-8")
        print(f"wrote {_display(RENDER)} ({len(current)} bytes)")
        return 0

    if not RENDER.exists():
        print(f"error: {_display(RENDER)} does not exist; run with --update", file=sys.stderr)
        return 1

    committed = RENDER.read_text(encoding="utf-8")
    problems = verify_png_binding()
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if committed == current:
        if problems:
            return 1
        print(f"{_display(RENDER)} matches what the dashboard draws")
        return 0

    print(
        f"error: {_display(RENDER)} no longer matches what the dashboard draws.\n"
        f"{drift_report(committed, current)}\n\n"
        "Regenerate with: uv run python scripts/render_tui_snapshot.py --update",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
