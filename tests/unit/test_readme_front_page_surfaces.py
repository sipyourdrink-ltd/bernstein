"""The front page has to show the two surfaces an operator actually watches.

`bernstein live` and `bernstein gui serve` are the two places a run is visible
while it is running. Before this, the README named them in passing - one line
inside a code block, one row of a table below the fold - and showed neither,
while ten renders of exactly those surfaces sat unreferenced in `docs/assets/`.

These tests pin the three properties that make the rendered result readable
rather than decorative, each of which fails silently otherwise:

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
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
RAW_ASSET_PREFIX = "https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/"
MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
HTML_IMG = re.compile(r"<img\b[^>]*?\balt=\"([^\"]*)\"[^>]*?\bsrc=\"([^\"]+)\"", re.IGNORECASE)

#: The commands whose surfaces the front page has to show, in column order.
SURFACE_COMMANDS = ("bernstein live", "bernstein gui serve")

#: Identity marks, whose correct alt text is the name of the thing they mark.
#: Describing a logo in a sentence is worse for a screen reader, not better -
#: the reader wants "Bernstein", not a description of the artwork.
IDENTITY_MARKS = frozenset({"logo-light.svg", "logo-dark.svg", "banner-readme.png"})


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _surface_table() -> tuple[list[str], list[str]]:
    """Return the (image row cells, caption row cells) of the surface table.

    The table is located by its image row rather than by line number, so
    ordinary edits above it do not turn these tests into a line-count assertion.
    """
    lines = README.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("| !["):
            images = _cells(line)
            # index+1 is the alignment row that makes it a table at all.
            captions = _cells(lines[index + 2])
            return images, captions
    raise AssertionError("no image table found on the front page")


def _asset_for(target: str) -> Path:
    assert target.startswith(RAW_ASSET_PREFIX), (
        f"front-page image {target!r} must be an absolute raw URL: a repo-relative "
        "path renders as a broken image on PyPI, where this file is the long description"
    )
    return REPO_ROOT / target[len(RAW_ASSET_PREFIX) :]


def test_front_page_shows_one_terminal_surface_and_one_browser_surface() -> None:
    """Two images, side by side, and nothing else in the row."""
    images, captions = _surface_table()

    assert len(images) == 2, f"expected exactly two surface renders, found {len(images)}"
    assert len(captions) == 2, f"expected one caption per render, found {len(captions)}"
    for cell in images:
        assert MARKDOWN_IMAGE.fullmatch(cell), f"surface cell is not a bare image: {cell!r}"


def test_each_surface_render_is_captioned_with_the_command_that_opens_it() -> None:
    """A render whose caption names no command is a picture of an unreachable thing."""
    _, captions = _surface_table()

    for command, caption in zip(SURFACE_COMMANDS, captions, strict=True):
        assert f"`{command}`" in caption, (
            f"the caption {caption!r} does not name the command that opens that surface (expected `{command}`)"
        )


def test_alt_text_on_every_front_page_render_is_a_sentence() -> None:
    """Alt text is read aloud and shown when the image 404s - a filename is neither.

    Scoped to the renders this repository ships: an external status badge is
    labelled by convention ("CI", "PyPI") and an identity mark is labelled by
    name, so neither is improved by a sentence.
    """
    text = README.read_text(encoding="utf-8")
    pairs = [(alt, target) for alt, target in MARKDOWN_IMAGE.findall(text)]
    pairs += [(alt, src) for alt, src in HTML_IMG.findall(text)]
    renders = [
        (alt, target)
        for alt, target in pairs
        if target.startswith(RAW_ASSET_PREFIX) and Path(target).name not in IDENTITY_MARKS
    ]
    assert renders, "expected the README to reference at least one render of a surface"

    for alt, target in renders:
        assert len(alt.split()) >= 6, f"alt text for {target} is too short to describe it: {alt!r}"
        assert not re.search(r"\.(png|gif|svg|jpe?g)\b", alt, re.IGNORECASE), (
            f"alt text reads as a filename rather than a description: {alt!r}"
        )


def test_no_front_page_render_shows_the_page_through_it() -> None:
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
    text = README.read_text(encoding="utf-8")
    targets = [target for _, target in MARKDOWN_IMAGE.findall(text)] + [src for _, src in HTML_IMG.findall(text)]

    #: Antialiased corners are allowed; a see-through background is not.
    corner_allowance = 0.001

    see_through: list[str] = []
    for target in targets:
        if not target.startswith(RAW_ASSET_PREFIX):
            continue
        asset = _asset_for(target)
        if asset.name in IDENTITY_MARKS:
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
        "these front-page renders show the page through them, so they invert "
        f"between GitHub's light and dark themes: {see_through}"
    )
