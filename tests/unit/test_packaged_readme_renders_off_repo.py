"""The README ships inside the wheel, so it has to work away from the repo.

`pyproject.toml` sets `readme = "README.md"`, which makes this file the
package's long description. PyPI renders that description standalone: there is
no repository around it, so a link written as `docs/getting-started/install.md`
resolves against `https://pypi.org/project/bernstein/` and 404s.

Nothing caught this when the README was rebuilt as a front page. The previous
README was one self-contained 70KB document that mostly linked outward; the
front-page rewrite replaced it with a short page whose whole job is pointing at
files elsewhere in the repository, and every one of those pointers was written
in the form that only works on github.com.

These tests fix the packaged artefact against the surface it is published on,
rather than against the surface it is authored on.
"""

from __future__ import annotations

import re
import tomllib
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
README = REPO_ROOT / "README.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"

LINK = re.compile(r"\]\(([^)]+)\)")
OFF_REPO_SAFE = ("http://", "https://", "#", "mailto:")

#: ``![alt](target)`` - the form used where the surrounding cell sizes the image.
#: The captured group is the whole destination *and* any optional title, which
#: :func:`markdown_destination` separates.
MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
#: Raw-content prefix an absolute image URL has to carry to render off-repo.
RAW_ASSET_PREFIX = "https://raw.githubusercontent.com/sipyourdrink-ltd/bernstein/main/"
#: Hosts the front page is allowed to load an image from. Everything here is a
#: status badge whose URL cannot be checked offline; the point of the list is
#: that the set of third parties the front page depends on is a decision, not
#: something that grows by accident. A new host has to be added here on
#: purpose, in a diff someone reads.
ALLOWED_IMAGE_HOSTS = frozenset(
    {
        "raw.githubusercontent.com",
        "github.com",
        "img.shields.io",
        "api.securityscorecards.dev",
        "mcptoplist.com",
        "deepwiki.com",
    }
)


def markdown_destination(raw: str) -> str:
    """Return just the URL from a markdown link destination.

    ``![alt](url "title")`` and ``![alt](<url>)`` are both valid and both
    render; a check that treats the title as part of the path reports a working
    image as broken. A gate that fails on correct input is worse than a missing
    one - it gets an exception added, and the exception is where the next real
    break hides.
    """
    stripped = raw.strip()
    if stripped.startswith("<"):
        end = stripped.find(">")
        return stripped[1:end] if end != -1 else stripped[1:]
    # A destination cannot contain unescaped whitespace unless it is wrapped in
    # angle brackets, so the first token is the URL and the rest is the title.
    parts = stripped.split()
    return parts[0] if parts else ""


class _ImageSourceCollector(HTMLParser):
    """Collect every image URL the HTML in a markdown file points at.

    Parsed rather than pattern-matched because ``src='…'`` and bare ``src=…``
    are as valid as ``src="…"``, and a policy that silently skips the forms it
    did not anticipate is worse than no policy: it reports success over the
    markup it could not see.

    ``srcset`` carries a comma-separated candidate list where each entry is a
    URL followed by an optional descriptor, so entries are split before the URL
    is taken.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "img" and values.get("src"):
            self.sources.append(str(values["src"]).strip())
        srcset = values.get("srcset") if tag in {"source", "img"} else None
        if srcset:
            for candidate in str(srcset).split(","):
                url = candidate.strip().split(" ", 1)[0]
                if url:
                    self.sources.append(url)


def _html_image_sources(text: str) -> list[str]:
    collector = _ImageSourceCollector()
    collector.feed(text)
    collector.close()
    return collector.sources


def _links() -> list[str]:
    return LINK.findall(README.read_text(encoding="utf-8"))


def _project() -> dict[str, object]:
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    project = data.get("project")
    assert isinstance(project, dict)
    return project


def test_readme_is_the_packaged_long_description() -> None:
    """The premise of every other test here. If this changes, they stop mattering."""
    assert _project().get("readme") == "README.md"


def test_no_readme_link_depends_on_the_repository_around_it() -> None:
    relative = [link for link in _links() if not link.startswith(OFF_REPO_SAFE)]
    assert not relative, (
        "these README links resolve only inside the repository, so they 404 on "
        f"PyPI where this file is the project description: {sorted(set(relative))}"
    )


def test_readme_absolute_links_point_at_paths_this_repo_actually_has() -> None:
    """An absolute link is not automatically a working one.

    Rewriting a relative path to a blob or tree URL is a mechanical edit, and a
    typo in it fails in exactly the place nobody looks: rendered on PyPI,
    months later. Checking the path component against the working tree catches
    that here instead, without needing the network. Both GitHub path forms are
    validated - files (``/blob/main/``) and directories (``/tree/main/``) - so
    neither form can carry a destination this repository does not contain.
    """
    prefixes = (
        "https://github.com/sipyourdrink-ltd/bernstein/blob/main/",
        "https://github.com/sipyourdrink-ltd/bernstein/tree/main/",
    )
    broken: list[str] = []
    for link in _links():
        prefix = next((p for p in prefixes if link.startswith(p)), None)
        if prefix is None:
            continue
        path = link[len(prefix) :].split("#", 1)[0]
        # A traversal component is itself a broken GitHub URL, and letting it
        # through would validate against whatever happens to exist OUTSIDE the
        # repository on the machine running the test.
        if ".." in PurePosixPath(path).parts or not (REPO_ROOT / path).exists():
            broken.append(link)
    assert not broken, f"README links to paths this repository does not contain: {broken}"


def test_every_readme_image_resolves_to_an_asset_this_repo_actually_ships() -> None:
    """Images have the link problem too, and one renderer further to fall through.

    A repo-relative ``src`` fails on PyPI exactly like a repo-relative link,
    except it fails silently as a broken-image icon rather than a dead click.
    An absolute ``raw.githubusercontent.com`` URL survives that render but
    still 404s when the path is wrong or the asset is later renamed - and the
    only place that shows is the published page, which nobody re-reads.

    Both the HTML ``<img>`` form (used for the demo GIF, which needs a width)
    and the markdown form (used inside the surface table, where the cell sizes
    the image) are checked, because the README uses both.

    ``<source srcset>`` inside the logo's ``<picture>`` is checked too. PyPI's
    sanitiser keeps only the ``<img>`` fallback, which is what a fallback is
    for - the theme switch is a GitHub enhancement, and dropping it there costs
    the reader nothing. On GitHub it is the element that actually renders in
    dark mode, so a broken URL in it is invisible to whoever committed it and
    visible to half the readers.

    An image on a host this repository does not ship from cannot be verified
    offline at all, so the rule there is different in kind: the host has to be
    one the front page already depends on deliberately. That does not prove a
    badge URL is spelled correctly - nothing offline can - but it does stop the
    dependency set from growing silently.
    """
    text = README.read_text(encoding="utf-8")
    sources = _html_image_sources(text) + [markdown_destination(target) for _, target in MARKDOWN_IMAGE.findall(text)]
    assert sources, "expected the README to reference at least one image"

    broken: list[str] = []
    foreign: list[str] = []
    for src in sources:
        if src.startswith(RAW_ASSET_PREFIX):
            # Take the path component from the parsed URL rather than trimming
            # suffixes by hand: `?raw=true` and GitHub's `#gh-dark-mode-only`
            # are both idiomatic on an image URL, and either one left attached
            # turns a working asset into a reported break.
            path = urlsplit(src[len(RAW_ASSET_PREFIX) :]).path
            if ".." in PurePosixPath(path).parts or not (REPO_ROOT / path).exists():
                broken.append(src)
        elif src.startswith(("http://", "https://")):
            host = urlsplit(src).hostname or ""
            if host not in ALLOWED_IMAGE_HOSTS:
                foreign.append(src)
        else:
            # Resolves against pypi.org/project/bernstein/ when PyPI renders
            # the long description, so it can only ever be a broken image.
            broken.append(src)
    assert not broken, (
        "these README image sources do not resolve to a committed asset, so they "
        f"render as a broken image on PyPI, on GitHub, or on both: {sorted(set(broken))}"
    )
    assert not foreign, (
        "the front page would load these images from hosts it does not already "
        "depend on; add the host to ALLOWED_IMAGE_HOSTS if that dependency is "
        f"intended: {sorted(set(foreign))}"
    )


def test_the_image_scan_sees_every_html_attribute_form() -> None:
    """The scan is only a policy over what it can see.

    ``src='…'`` and bare ``src=…`` render exactly like ``src="…"``, so a scan
    that reads only the third form passes a README full of images it never
    looked at - the failure mode where a gate is worse than no gate, because
    it reports success.
    """
    markup = (
        '<img src="https://one.example/a.png">'
        "<img src='https://two.example/b.png'>"
        "<img src=https://three.example/c.png>"
        "<picture><source srcset='https://four.example/d.png 2x, https://five.example/e.png'></picture>"
    )

    assert _html_image_sources(markup) == [
        "https://one.example/a.png",
        "https://two.example/b.png",
        "https://three.example/c.png",
        "https://four.example/d.png",
        "https://five.example/e.png",
    ]


def test_a_query_or_fragment_is_not_read_as_part_of_the_asset_path() -> None:
    """`?raw=true` and `#gh-dark-mode-only` are both idiomatic on an image URL.

    Neither changes which file is fetched, so neither may change whether the
    gate finds it - a gate that fails on correct input gets switched off.
    """
    asset = "docs/assets/tui-agents.png"
    assert (REPO_ROOT / asset).exists(), "the probe asset has moved"
    for suffix in ("", "?raw=true", "#gh-dark-mode-only", "?raw=true#gh-dark-mode-only"):
        path = urlsplit(f"{RAW_ASSET_PREFIX}{asset}{suffix}"[len(RAW_ASSET_PREFIX) :]).path
        assert (REPO_ROOT / path).exists(), suffix


def test_a_markdown_title_is_not_read_as_part_of_the_destination() -> None:
    """``![alt](url "title")`` is valid markdown that renders.

    Reading the title as part of the path turns a working image into a
    reported break, which is the failure mode that gets a gate switched off.
    """
    assert markdown_destination('docs/a.png "a title"') == "docs/a.png"
    assert markdown_destination("<https://example.com/a.png>") == "https://example.com/a.png"
    assert markdown_destination("  https://example.com/a.png  ") == "https://example.com/a.png"
    assert markdown_destination('<docs/a.png> "titled and bracketed"') == "docs/a.png"


def test_python_classifiers_match_the_versions_the_project_supports() -> None:
    """A classifier is what package indexes filter on, so a stale one hides the project.

    `requires-python` is the enforced floor and the CI matrix is the tested set;
    the classifier list is the advertised one, and it drifts silently because
    nothing fails when it lags.
    """
    classifiers = _project().get("classifiers")
    assert isinstance(classifiers, list)
    advertised = {
        item.rsplit(" :: ", 1)[-1]
        for item in classifiers
        if isinstance(item, str) and item.startswith("Programming Language :: Python :: 3.")
    }

    # Only `python-version:` assignments count as the tested set. Scanning the
    # whole file for version-shaped text reads prose as configuration: ci.yml
    # carries the comment "3.11 is intentionally excluded", and a loose scan
    # turns that sentence into a demand to advertise 3.11, which
    # `requires-python = ">=3.12"` refuses to install on.
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    tested: set[str] = set()
    for line in ci.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped.startswith("python-version:"):
            continue
        tested.update(re.findall(r"3\.\d+", stripped))

    assert tested, "expected to find python-version assignments in ci.yml"
    assert tested <= advertised, (
        f"CI tests Python {sorted(tested - advertised)} but the classifiers do not "
        "advertise it, so index filters exclude the project from those searches"
    )
