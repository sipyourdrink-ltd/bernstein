"""Both front pages have to show the two surfaces an operator actually watches.

`bernstein live` and `bernstein gui serve` are the two places a run is visible
while it is running. Before this, the README named them in passing - one line
inside a code block, one row of a table below the fold - and showed neither,
while ten renders of exactly those surfaces sat unreferenced in `docs/assets/`.

The project has two front pages: `README.md`, which is also the packaged long
description on PyPI, and `docs/index.md`, which is the MkDocs Home page. They
carry the same block and reference the same assets through different URL
forms, so every check here runs against both - nothing about the second page
is verified by the docs build, which does not run in CI.

These tests pin the properties that make the rendered result readable rather
than decorative, each of which fails silently otherwise:

* the page shows one terminal surface and one browser surface, each captioned
  with the command that opens it (a caption naming no command leaves the
  reader with a picture and no way to reach it);
* the alt text is a sentence (it is what a screen reader announces, and what
  GitHub shows when the image fails to load);
* nothing referenced from the page is transparent (a transparent asset takes
  the page background, so it inverts between GitHub's light and dark themes -
  legible in one, unreadable in the other, and never noticed by whoever
  committed it in their own theme).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_ASSET_PREFIX = "https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/"
MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
HTML_IMG = re.compile(r"<img\b[^>]*?\balt=\"([^\"]*)\"[^>]*?\bsrc=\"([^\"]+)\"", re.IGNORECASE)

#: The commands whose surfaces the front page has to show, in column order.
SURFACE_COMMANDS = ("bernstein live", "bernstein gui serve")

#: Identity marks, whose correct alt text is the name of the thing they mark.
#: Describing a logo in a sentence is worse for a screen reader, not better -
#: the reader wants "Bernstein", not a description of the artwork.
IDENTITY_MARKS = frozenset({"logo-light.svg", "logo-dark.svg", "banner-readme.png"})


@dataclass(frozen=True)
class FrontPage:
    """A front page plus the URL form its images are written in.

    The two pages resolve assets differently and each form is correct where it
    is used: the README is rendered off-repo by PyPI and needs absolute raw
    URLs, while MkDocs rewrites site-relative paths and would break on them.
    Sharing the assertions but not the resolver is what lets one set of
    properties cover both.
    """

    name: str
    path: Path
    #: Where a resolvable image target starts, relative to the repository root.
    asset_root: Path
    #: Prefix an image target must carry on this page, "" for site-relative.
    required_prefix: str

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def images(self) -> list[tuple[str, str]]:
        """Return (alt, target) for every image on the page, both syntaxes."""
        body = self.text()
        return [*MARKDOWN_IMAGE.findall(body), *HTML_IMG.findall(body)]

    def resolve(self, target: str) -> Path | None:
        """Return the repository file an image target names, or None if foreign.

        Foreign here means an external URL (a status badge), which no offline
        check can resolve. Whether those are acceptable is settled in
        ``test_packaged_readme_renders_off_repo``; this resolver only reports
        that there is no local file behind them.
        """
        if self.required_prefix:
            if not target.startswith(self.required_prefix):
                return None
            return self.asset_root / target[len(self.required_prefix) :]
        if target.startswith(("http://", "https://")):
            return None
        return self.asset_root / target


FRONT_PAGES = (
    FrontPage(
        name="README.md",
        path=REPO_ROOT / "README.md",
        asset_root=REPO_ROOT,
        required_prefix=RAW_ASSET_PREFIX,
    ),
    FrontPage(
        name="docs/index.md",
        path=REPO_ROOT / "docs" / "index.md",
        asset_root=REPO_ROOT / "docs",
        required_prefix="",
    ),
)
PAGE_IDS = [page.name for page in FRONT_PAGES]


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _surface_table(page: FrontPage) -> tuple[list[str], list[str]]:
    """Return the (image row cells, caption row cells) of the surface table.

    The table is located by its image row rather than by line number, so
    ordinary edits above it do not turn these tests into a line-count assertion.
    """
    lines = page.text().splitlines()
    for index, line in enumerate(lines):
        if line.startswith("| !["):
            images = _cells(line)
            # index+1 is the alignment row that makes it a table at all.
            captions = _cells(lines[index + 2])
            return images, captions
    raise AssertionError(f"no image table found on {page.name}")


@pytest.mark.parametrize("page", FRONT_PAGES, ids=PAGE_IDS)
def test_front_page_shows_one_terminal_surface_and_one_browser_surface(page: FrontPage) -> None:
    """Two images, side by side, and nothing else in the row."""
    images, captions = _surface_table(page)

    assert len(images) == 2, f"{page.name}: expected exactly two surface renders, found {len(images)}"
    assert len(captions) == 2, f"{page.name}: expected one caption per render, found {len(captions)}"
    for cell in images:
        assert MARKDOWN_IMAGE.fullmatch(cell), f"{page.name}: surface cell is not a bare image: {cell!r}"


@pytest.mark.parametrize("page", FRONT_PAGES, ids=PAGE_IDS)
def test_each_surface_render_is_captioned_with_the_command_that_opens_it(page: FrontPage) -> None:
    """A render whose caption names no command is a picture of an unreachable thing."""
    _, captions = _surface_table(page)

    for command, caption in zip(SURFACE_COMMANDS, captions, strict=True):
        assert f"`{command}`" in caption, (
            f"{page.name}: the caption {caption!r} does not name the command that opens "
            f"that surface (expected `{command}`)"
        )


@pytest.mark.parametrize("page", FRONT_PAGES, ids=PAGE_IDS)
def test_every_front_page_render_resolves_to_a_committed_asset(page: FrontPage) -> None:
    """A renamed asset breaks the page in the place nobody re-reads: rendered.

    The README half of this is also covered from the PyPI side in
    ``test_packaged_readme_renders_off_repo``; the value here is
    ``docs/index.md``, whose site-relative targets nothing else checks - the
    docs build does not run in CI.
    """
    missing = [
        target
        for _, target in page.images()
        if (resolved := page.resolve(target)) is not None and not resolved.exists()
    ]
    assert not missing, f"{page.name} references assets this repository does not contain: {missing}"


@pytest.mark.parametrize("page", FRONT_PAGES, ids=PAGE_IDS)
def test_alt_text_on_every_front_page_render_is_a_sentence(page: FrontPage) -> None:
    """Alt text is read aloud and shown when the image 404s - a filename is neither.

    Scoped to the renders this repository ships: an external status badge is
    labelled by convention ("CI", "PyPI") and an identity mark is labelled by
    name, so neither is improved by a sentence.
    """
    renders = [
        (alt, target)
        for alt, target in page.images()
        if (resolved := page.resolve(target)) is not None and resolved.name not in IDENTITY_MARKS
    ]
    assert renders, f"{page.name}: expected at least one render of a surface"

    for alt, target in renders:
        assert len(alt.split()) >= 6, f"{page.name}: alt text for {target} is too short: {alt!r}"
        assert not re.search(r"\.(png|gif|svg|jpe?g)\b", alt, re.IGNORECASE), (
            f"{page.name}: alt text reads as a filename rather than a description: {alt!r}"
        )


@pytest.mark.parametrize("page", FRONT_PAGES, ids=PAGE_IDS)
def test_no_front_page_render_shows_the_page_through_it(page: FrontPage) -> None:
    """A transparent render inherits the page background and inverts between themes.

    Checked on the pixels rather than by eye: whoever commits a render only
    ever sees it in their own theme, so "looks fine" is not evidence about the
    other one.

    Measured as a share of the image rather than as a yes/no on the alpha
    channel, because the two failure sizes are orders of magnitude apart. The
    demo GIF is antialiased into rounded corners and spends 8 pixels of 2.08M
    on them; a render that actually relies on the page background spends tens
    of thousands. The former is invisible in either theme, the latter is the
    bug this guards.

    The logo pair is exempt: it is theme-switched deliberately through
    ``<picture>``/``prefers-color-scheme``, which is the correct mechanism for
    an asset that should follow the theme rather than resist it.
    """
    #: Antialiased corners are allowed; a see-through background is not.
    corner_allowance = 0.001

    see_through: list[str] = []
    for _, target in page.images():
        asset = page.resolve(target)
        if asset is None or asset.name in IDENTITY_MARKS or not asset.exists():
            continue
        with Image.open(asset) as image:
            # The first frame is the one composited against the page; later
            # GIF frames are diffs whose transparency means "unchanged".
            histogram = image.convert("RGBA").getchannel("A").histogram()
            fully_transparent = histogram[0]
            total = sum(histogram)
        if total and fully_transparent / total > corner_allowance:
            see_through.append(f"{asset.name} ({100 * fully_transparent / total:.2f}% transparent)")

    assert not see_through, (
        f"these renders on {page.name} show the page through them, so they invert "
        f"between GitHub's light and dark themes: {see_through}"
    )
