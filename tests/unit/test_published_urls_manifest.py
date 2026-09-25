"""Published identifiers must resolve.

Every ``https://bernstein.run/...`` literal the package emits (predicate
types, schema ``$id`` values, namespaces, catalog URLs) is a published
identifier: verifiers and tools dereference it. The site serves exactly the
URLs listed in ``docs/reference/published-urls.json`` and checks each one
resolves, so a new literal cannot land without a manifest entry.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "docs" / "reference" / "published-urls.json"
SCANNED_ROOTS = ("src", "schemas")
DOCS_PAGE = REPO_ROOT / "docs" / "reference" / "published-identifiers.md"

_URL_RE = re.compile(r"https://bernstein\.run/[A-Za-z0-9_./{}\-]*")
_PLACEHOLDER_RE = re.compile(r"\{[^}]*\}")
_KINDS = {"schema", "document", "redirect"}


def _normalise(url: str) -> str:
    """Collapse f-string placeholders so ``/spdx/{bom.run_id}`` reads ``/spdx/{id}``."""
    return _PLACEHOLDER_RE.sub("{id}", url)


def _literals_in_tree() -> set[str]:
    found: set[str] = set()
    for root in SCANNED_ROOTS:
        for path in (REPO_ROOT / root).rglob("*"):
            if path.suffix not in {".py", ".json"} or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            found.update(_normalise(m) for m in _URL_RE.findall(text))
    return found


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_lists_exactly_the_published_literals() -> None:
    listed = {entry["url"] for entry in _manifest()["entries"]}
    literals = _literals_in_tree()
    assert literals - listed == set(), "add these URLs to docs/reference/published-urls.json"
    assert listed - literals == set(), "these manifest URLs are no longer emitted; remove them"


def test_manifest_entries_are_well_formed() -> None:
    entries = _manifest()["entries"]
    urls = [entry["url"] for entry in entries]
    assert len(urls) == len(set(urls)), "duplicate manifest URL"
    assert urls == sorted(urls), "keep manifest entries sorted by url"
    for entry in entries:
        assert entry["kind"] in _KINDS, entry
        if entry["kind"] == "redirect":
            assert entry["target"].startswith("https://"), entry
        else:
            assert "target" not in entry, entry


def test_schema_entries_are_served_under_their_own_id() -> None:
    for entry in _manifest()["entries"]:
        if entry["kind"] != "schema":
            continue
        source = REPO_ROOT / entry["source"]
        assert source.is_file(), entry
        schema = json.loads(source.read_text(encoding="utf-8"))
        assert schema.get("$id") == entry["url"], f"{entry['source']} $id must equal its published URL"


def test_redirect_anchors_exist_in_the_docs() -> None:
    """A redirect into the docs must land on a heading that exists."""
    docs_root = "https://bernstein.readthedocs.io/en/latest/"
    for entry in _manifest()["entries"]:
        if entry["kind"] != "redirect" or not entry["target"].startswith(docs_root):
            continue
        page, _, anchor = entry["target"][len(docs_root) :].partition("#")
        md = REPO_ROOT / "docs" / (page.rstrip("/") + ".md")
        assert md.is_file(), entry
        if anchor:
            assert "{#" + anchor + "}" in md.read_text(encoding="utf-8"), entry


def test_docs_page_is_in_the_nav() -> None:
    assert DOCS_PAGE.is_file()
    assert "reference/published-identifiers.md" in (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
