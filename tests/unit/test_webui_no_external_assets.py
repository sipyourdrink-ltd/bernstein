"""The shipped dashboard must not fetch anything from a third party to render.

This guards a boot failure, not a typography one. ``web/src/index.css`` used to
open with an ``@import`` of the Google Fonts stylesheet. A CSS ``@import``
blocks its own stylesheet from finishing, and that stylesheet blocks the
``<script type="module">`` after it, so on a host with no route to the font
host the page commits, ``document.readyState`` stays ``interactive``,
``DOMContentLoaded`` never fires and ``#root`` stays empty. The operator gets a
blank dashboard; the ``system-ui`` fallback in the font stack never gets to
help, because nothing renders at all.

The same reasoning already strips the CDN webfont from
``docs/assets/tui-live.svg`` (docs/contributing/render-freshness.md): this
project ships an air-gap profile, and a published surface that fetches from a
third party on view tells that party who is looking at it.

The regression can arrive two ways, so both are checked: through the source
(a style refactor reintroducing the ``@import``) and through the build (a
dependency that injects a hosted stylesheet). The bundle is authoritative -
it is what the wheel ships - but the source is where a human would look, so a
failure there names the file they can actually edit.

The complement matters as much: deleting the ``@font-face`` blocks would also
make the "no external host" assertion pass while shipping a dashboard with no
typeface. So every asset the shipped CSS references must resolve to a file that
is really in the bundle.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_DIR = REPO_ROOT / "src" / "bernstein" / "gui" / "static"
WEB_SRC = REPO_ROOT / "web" / "src"

#: The Vite ``base``. Shipped CSS references assets absolutely (``/ui/assets/x``)
#: because that is where ``bernstein.gui`` mounts the bundle.
BASE = "/ui/"

#: A reference leaves this origin when it carries an authority component: an
#: explicit scheme (``https://host``) or a protocol-relative one (``//host``).
#: ``/ui/assets/x``, ``./fonts/x`` and ``data:`` URIs all stay put.
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:)?//", re.IGNORECASE)

_CSS_IMPORT = re.compile(r"""@import\s+(?:url\(\s*)?['"]?([^'")\s;]+)""", re.IGNORECASE)
_CSS_URL = re.compile(r"""url\(\s*['"]?([^'")]+?)['"]?\s*\)""", re.IGNORECASE)
_HTML_ATTR = re.compile(r"""(?:href|src)\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE)


def css_references(text: str) -> list[str]:
    """Every asset a stylesheet asks the browser to fetch."""
    return _CSS_IMPORT.findall(text) + _CSS_URL.findall(text)


def html_references(text: str) -> list[str]:
    """Every ``href``/``src`` in a document, including ``<link>`` and ``<script>``."""
    return _HTML_ATTR.findall(text)


def external(references: list[str]) -> list[str]:
    """References that leave this origin, deduplicated so a failure reads cleanly.

    An ``@import url(...)`` matches both reference patterns, and reporting the
    same offending URL twice invites the reader to look for a second one.
    """
    return list(dict.fromkeys(ref for ref in references if _EXTERNAL.match(ref.strip())))


def shipped_stylesheets() -> list[Path]:
    return sorted(BUNDLE_DIR.rglob("*.css"))


def test_the_bundle_ships_at_least_one_stylesheet() -> None:
    """Guard the guards: an empty glob would make every scan below vacuous."""
    assert shipped_stylesheets(), f"no stylesheet under {BUNDLE_DIR.relative_to(REPO_ROOT)}"


@pytest.mark.parametrize("stylesheet", shipped_stylesheets(), ids=lambda path: path.name)
def test_the_shipped_css_fetches_nothing_from_a_third_party(stylesheet: Path) -> None:
    """The failure this whole module exists for, checked on what actually ships."""
    offenders = external(css_references(stylesheet.read_text(encoding="utf-8")))

    assert not offenders, (
        f"{stylesheet.relative_to(REPO_ROOT)} fetches from another origin: {offenders}.\n"
        "A render-blocking external stylesheet or font leaves the dashboard blank when the "
        "host is unreachable, and tells that host who is looking. Vendor the asset under "
        "web/src/ and reference it relatively - see web/src/fonts/README.md."
    )


def test_the_source_css_fetches_nothing_from_a_third_party() -> None:
    """Same property one step upstream, where the fix would be made.

    The bundle is the authority, but it is generated; a reader who broke this
    needs to be pointed at the file they can edit.
    """
    sources = sorted(WEB_SRC.rglob("*.css"))
    assert sources, "no stylesheet under web/src - the scan would be vacuous"

    offenders = {
        str(path.relative_to(REPO_ROOT)): found
        for path in sources
        if (found := external(css_references(path.read_text(encoding="utf-8"))))
    }

    assert not offenders, f"stylesheet sources fetch from another origin: {offenders}"


@pytest.mark.parametrize(
    "document",
    [BUNDLE_DIR / "index.html", REPO_ROOT / "web" / "index.html"],
    ids=["shipped", "source"],
)
def test_the_dashboard_document_fetches_nothing_from_a_third_party(document: Path) -> None:
    """A hosted ``<link rel=stylesheet>`` is the same failure by another route.

    Moving the ``@import`` into the document would fix nothing: the stylesheet
    still blocks the module script that follows it.
    """
    offenders = external(html_references(document.read_text(encoding="utf-8")))

    assert not offenders, f"{document.relative_to(REPO_ROOT)} fetches from another origin: {offenders}"


@pytest.mark.parametrize("stylesheet", shipped_stylesheets(), ids=lambda path: path.name)
def test_every_asset_the_shipped_css_references_is_in_the_bundle(stylesheet: Path) -> None:
    """The complement: deleting the fonts must not be a way to pass.

    Without this, ripping out the ``@font-face`` blocks satisfies "no external
    host" while shipping a dashboard that has no typeface to load.
    """
    missing = []
    for ref in css_references(stylesheet.read_text(encoding="utf-8")):
        ref = ref.strip()
        if ref.startswith("data:") or _EXTERNAL.match(ref):
            continue
        relative = ref.split("?", 1)[0].split("#", 1)[0]
        relative = relative[len(BASE) :] if relative.startswith(BASE) else relative.lstrip("/")
        if not (BUNDLE_DIR / relative).is_file():
            missing.append(ref)

    assert not missing, (
        f"{stylesheet.relative_to(REPO_ROOT)} references assets that are not in the bundle: {missing}.\n"
        "Rebuild with: cd web && npm ci && npm run build"
    )


def test_both_dashboard_typefaces_are_declared_locally() -> None:
    """The families the UI names must be the ones the bundle carries.

    ``tailwind.config.js`` and ``index.css`` set ``Inter Tight`` and
    ``JetBrains Mono`` on ``body`` and on the numeric cells. If a refactor drops
    an ``@font-face`` the fallback stack silently takes over everywhere, which
    is a quiet visual regression rather than a loud one.
    """
    shipped = "\n".join(path.read_text(encoding="utf-8") for path in shipped_stylesheets())
    faces = re.findall(r"@font-face\s*\{(.*?)\}", shipped, re.DOTALL)
    declared = {
        match.group(1).strip().strip("'\"")
        for block in faces
        if (match := re.search(r"font-family\s*:\s*([^;}]+)", block))
    }

    assert {"Inter Tight", "JetBrains Mono"} <= declared, f"declared @font-face families: {sorted(declared)}"


def test_the_scan_catches_the_import_that_was_removed() -> None:
    """Prove the detector fires, rather than that today's files happen to be clean.

    A scanner that matches nothing passes every file in the repository. This
    feeds it the exact line that was removed from ``web/src/index.css``, plus
    the two shapes a reintroduction would most plausibly take.
    """
    removed = (
        "@import url('https://fonts.googleapis.com/css2?family=Inter+Tight:wght@300;400;500;600;700"
        "&family=JetBrains+Mono:wght@400;500;600&display=swap');"
    )
    bare_import = '@import "https://fonts.googleapis.com/css2?family=Inter+Tight";'
    hosted_font = "@font-face{font-family:x;src:url(//fonts.gstatic.com/s/x.woff2) format('woff2')}"

    for source in (removed, bare_import, hosted_font):
        assert external(css_references(source)), f"the scan missed: {source}"

    # ...and does not fire on what the bundle legitimately contains.
    kept = "@font-face{font-family:x;src:url(/ui/assets/x.woff2)}.i{background:url(data:image/png;base64,AA==)}"
    assert not external(css_references(kept))

    assert external(html_references('<link rel="stylesheet" href="https://fonts.googleapis.com/css2">'))
    assert not external(html_references('<link rel="icon" href="data:image/svg+xml;utf8,%3Csvg%3E" />'))
