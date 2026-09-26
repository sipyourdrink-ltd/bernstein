"""The brand kit under docs/assets/brand/ stays complete, named and wired.

docs/design/brand.md is the guideline; this file is the check. It guards the
things that have broken before with image assets: a file renamed or deleted
while a README, a listing or the docs site still hotlinked it, PNG exports
that drift from their SVG source, names with no convention, and a wordmark
that quietly moved. No renderer is needed: SVGs are parsed as XML and PNG
sizes are read from the IHDR chunk.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ASSETS = REPO / "docs" / "assets"
BRAND = ASSETS / "brand"
GUIDE = REPO / "docs" / "design" / "brand.md"

AMBER = "#F5A524"
DARK_BG = "#13130F"
PAPER_BG = "#F8F1E7"

# The wordmark paths are hotlinked from PyPI and the website repository -
# content may change, the paths may not (docs/design/brand.md § The wordmark).
HOTLINKED = ("logo-dark.svg", "logo-light.svg", "banner-readme.webp")

EXPECTED_PNG_SIZES = {
    **{f"bernstein-mark-{s}.png": (s, s) for s in (32, 64, 128, 256, 512, 1024)},
    "bernstein-logo-square-dark-512.png": (512, 512),
    "bernstein-logo-square-dark-1024.png": (1024, 1024),
    "bernstein-logo-square-light-512.png": (512, 512),
    "bernstein-social-1280x640.png": (1280, 640),
    "bernstein-og-1200x630.png": (1200, 630),
}
EXPECTED_SVGS = {
    "bernstein-mark.svg",
    "bernstein-mark-mono.svg",
    "bernstein-logo-square-dark.svg",
    "bernstein-logo-square-light.svg",
    "bernstein-social-1280x640.svg",
    "bernstein-og-1200x630.svg",
}
NAME_RE = re.compile(r"^bernstein(-[a-z0-9]+)+\.(svg|png)$")


def _png_size(path: Path) -> tuple[int, int]:
    head = path.read_bytes()[:24]
    assert head.startswith(b"\x89PNG\r\n\x1a\n"), f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def _svg_root(path: Path) -> ET.Element:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    assert root.tag == "{http://www.w3.org/2000/svg}svg", f"{path.name}: root is {root.tag}"
    return root


def test_brand_dir_holds_exactly_the_documented_files() -> None:
    assert set(p.name for p in BRAND.iterdir()) == EXPECTED_SVGS | set(EXPECTED_PNG_SIZES)


@pytest.mark.parametrize("name", sorted(EXPECTED_SVGS | set(EXPECTED_PNG_SIZES)))
def test_brand_file_names_follow_the_convention(name: str) -> None:
    assert NAME_RE.match(name), f"{name}: expected bernstein-<what>[-<variant>][-<size>].<ext>"


@pytest.mark.parametrize(("name", "size"), sorted(EXPECTED_PNG_SIZES.items()))
def test_png_exports_have_their_stated_size(name: str, size: tuple[int, int]) -> None:
    assert _png_size(BRAND / name) == size


@pytest.mark.parametrize("name", sorted(EXPECTED_SVGS))
def test_brand_svgs_are_well_formed_and_generated(name: str) -> None:
    root = _svg_root(BRAND / name)
    assert root.get("viewBox"), f"{name}: no viewBox"
    assert "scripts/gen_brand_svgs.py" in (BRAND / name).read_text(encoding="utf-8")


def test_mark_is_amber_and_cuts_the_prompt_out() -> None:
    text = (BRAND / "bernstein-mark.svg").read_text(encoding="utf-8")
    assert AMBER in text
    assert "<mask" in text, "the prompt is a cut-out (mask), not a drawing on top"


def test_mono_mark_uses_current_color_only() -> None:
    text = (BRAND / "bernstein-mark-mono.svg").read_text(encoding="utf-8")
    fills = set(re.findall(r'fill="([^"]+)"', text))
    assert fills == {"currentColor"}, fills
    assert 'fill-rule="evenodd"' in text


@pytest.mark.parametrize(
    ("name", "background"),
    [("bernstein-logo-square-dark.svg", DARK_BG), ("bernstein-logo-square-light.svg", PAPER_BG)],
)
def test_square_logos_sit_on_the_token_backgrounds(name: str, background: str) -> None:
    assert background in (BRAND / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", HOTLINKED)
def test_hotlinked_paths_still_exist(name: str) -> None:
    assert (ASSETS / name).is_file(), f"docs/assets/{name} is hotlinked from outside the repo; keep the path"


@pytest.mark.parametrize("name", ["logo-dark.svg", "logo-light.svg"])
def test_wordmarks_carry_the_mark_and_outlined_text(name: str) -> None:
    root = _svg_root(ASSETS / name)
    assert root.get("viewBox") == "0 0 400 80"
    text = (ASSETS / name).read_text(encoding="utf-8")
    assert AMBER in text
    assert "<text" not in text, "the wordmark is outlines, not a <text> element that needs a font"


def _names_listed_in_guide() -> set[str]:
    """Every `bernstein-...` file name the guide mentions, with `{a,b}` groups expanded."""
    names: set[str] = set()
    for token in re.findall(r"bernstein-[A-Za-z0-9{},.\-]+", GUIDE.read_text(encoding="utf-8")):
        expanded = [token]
        while (m := re.search(r"\{([^{}]*)\}", expanded[0])) is not None:
            expanded = [e[: m.start()] + alt + e[m.end() :] for e in expanded for alt in m.group(1).split(",")]
        names.update(expanded)
    return names


def test_guide_lists_every_brand_file() -> None:
    listed = _names_listed_in_guide()
    missing = sorted(p.name for p in BRAND.iterdir() if p.name not in listed)
    assert not missing, f"add to docs/design/brand.md § Files: {missing}"


def test_docs_site_logo_and_favicon_point_at_the_mark() -> None:
    mkdocs = (REPO / "mkdocs.yml").read_text(encoding="utf-8")
    assert "  logo: assets/brand/bernstein-mark.svg" in mkdocs
    assert "  favicon: assets/brand/bernstein-mark.svg" in mkdocs
    assert "design/brand.md" in mkdocs


def test_docs_og_image_is_the_brand_export() -> None:
    og = "assets/brand/bernstein-og-1200x630.png"
    for rel in ("docs/overrides/main.html", "docs/benchmarks/leaderboard.html"):
        assert og in (REPO / rel).read_text(encoding="utf-8"), rel


def test_operator_gui_favicon_is_the_amber_mark() -> None:
    html = (REPO / "web" / "index.html").read_text(encoding="utf-8")
    assert "%23F5A524" in html and "fill-rule='evenodd'" in html


def test_vscode_icons_are_the_mark() -> None:
    media = REPO / "packages" / "vscode" / "media"
    svg = (media / "bernstein-icon.svg").read_text(encoding="utf-8")
    assert "currentColor" in svg and 'fill-rule="evenodd"' in svg
    assert _png_size(media / "bernstein-icon.png") == (128, 128)
