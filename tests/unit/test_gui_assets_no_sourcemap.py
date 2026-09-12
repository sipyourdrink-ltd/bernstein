"""Packaging guard: no source map ships inside the wheel (#4878).

A 4.1 MB ``index-*.js.map`` was committed under ``src/bernstein/gui/static/``,
which IS the packaged tree - so every install downloaded it. It was 4.5x the
size of the bundle it described and 82% of the entire GUI asset payload, for a
debugging aid a browser fetches only when devtools is open.

Deleting the file was never the fix on its own. ``web/vite.config.ts`` had
``sourcemap: true``, so the next ``npm run build`` would have written it back,
and the spa-bundle-freshness lane would then have demanded the 4.1 MB blob be
committed again to make the tree clean.

So the guard is in two halves, matching the two ways it can regress: the build
must not be configured to emit maps, and the artifact must not contain one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The tree hatchling ships verbatim - anything here is in the wheel.
PACKAGED_GUI_DIR = REPO_ROOT / "src" / "bernstein" / "gui" / "static"

VITE_CONFIG = REPO_ROOT / "web" / "vite.config.ts"

#: Scans the source tree rather than importing it, so no diff produces an
#: import edge to this file. The marker puts it in every pull request's
#: affected slice instead of only the merge group (#5428).
pytestmark = [
    pytest.mark.whole_tree_guard,
    pytest.mark.skipif(
        not (REPO_ROOT / "pyproject.toml").is_file(),
        reason="packaging guards only run inside a bernstein source checkout",
    ),
]


def test_no_source_map_under_the_packaged_gui_tree() -> None:
    """No ``*.map`` anywhere under the shipped GUI assets."""
    maps = sorted(p.relative_to(REPO_ROOT) for p in PACKAGED_GUI_DIR.rglob("*.map"))
    assert not maps, (
        "source map(s) under the packaged GUI tree would ship in the wheel to every "
        f"install: {[str(m) for m in maps]}. Set `sourcemap: false` in web/vite.config.ts "
        "and rebuild rather than deleting the file, or the next build restores it."
    )


def test_vite_does_not_emit_source_maps_into_the_packaged_tree() -> None:
    """The build config is the half that decides whether the file comes back.

    Asserted on the config text rather than by running a build: the point is to
    fail the moment someone flips the flag, which is cheaper and earlier than
    catching the 4 MB artifact it produces.
    """
    config = VITE_CONFIG.read_text(encoding="utf-8")
    assert re.search(r"^\s*sourcemap:\s*false\s*,", config, re.MULTILINE), (
        "web/vite.config.ts must keep `sourcemap: false`: its outDir IS the packaged "
        "tree, so a map emitted there ships to every install."
    )


def test_no_bundle_references_a_source_map_that_is_not_there() -> None:
    """A dangling ``sourceMappingURL`` 404s for anyone who opens devtools.

    The two belong in one change: dropping the map while leaving the comment
    trades 4 MB of payload for a broken devtools experience.
    """
    dangling: list[str] = []
    for script in PACKAGED_GUI_DIR.rglob("*.js"):
        text = script.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"sourceMappingURL=(\S+)", text):
            target = match.group(1)
            if target.startswith("data:"):
                continue  # inlined, nothing to resolve
            if not (script.parent / target).exists():
                dangling.append(f"{script.relative_to(REPO_ROOT)} -> {target}")
    assert not dangling, f"bundle(s) point at a source map that does not ship: {dangling}"
