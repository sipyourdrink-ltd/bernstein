#!/usr/bin/env python3
"""Rotate per-change release-notes fragments into a versioned notes page.

Every PR used to append its own line to the single
``docs/release-notes/unreleased.md`` file. Any two open PRs therefore
conflicted on that one shared anchor in the merge queue: the first merge
invalidated every other queued PR, and each needed a manual rebase and
re-enqueue.

A PR now has the option to add one file instead: ``docs/release-notes/
fragments/<issue-or-slug>.md``, holding the same ``## <title>`` heading and
prose paragraph an entry has always used. One entry, one file -- no shared
anchor, no conflicts.

The ``rotate`` command removes consumed inputs after manual curation.
The ``assemble`` command instead uses the checked-in cut description to
build the version page, update navigation and retire just the inputs it used.
Both commands leave fragments deferred by that version's cut in place.

Usage::

    python scripts/rotate_release_notes.py assemble v4.0.0
    python scripts/rotate_release_notes.py rotate docs/release-notes/v3.18.0.md
    python scripts/rotate_release_notes.py check-gate <changed-file> [<changed-file> ...]
"""

from __future__ import annotations

import argparse
import os.path
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAGMENTS_DIR = REPO_ROOT / "docs" / "release-notes" / "fragments"

# Repo-relative paths the notes gate recognises. Compared against
# forward-slash-normalized input so it works with paths from either a POSIX
# or a Windows checkout.
_UNRELEASED_REL = "docs/release-notes/unreleased.md"
_FRAGMENTS_REL_PREFIX = "docs/release-notes/fragments/"


@dataclass
class RotationResult:
    """Outcome of folding fragments into a version page."""

    consumed: list[Path] = field(default_factory=list)
    rendered: str = ""


def collect_fragments(fragments_dir: Path, *, hold: Iterable[str] = ()) -> list[Path]:
    """Return fragment files under ``fragments_dir`` in deterministic order.

    Ordered by filename, not by write time or directory-listing order: two
    PRs that each add one fragment merge in either order and still render
    the same combined section, so there is nothing for the merge queue to
    conflict on.

    Args:
        fragments_dir: Directory holding one ``.md`` file per entry.

    Returns:
        Matching files sorted by filename. Empty when the directory is
        missing, empty, or holds no ``.md`` files.
    """
    if not fragments_dir.is_dir():
        return []
    return sorted(p for p in fragments_dir.iterdir() if p.name not in hold and p.suffix == ".md" and p.is_file())


def render_fragments(fragments_dir: Path) -> str:
    """Concatenate fragment bodies into one rendered section.

    Each fragment holds one whole entry -- the same ``## <title>`` heading
    plus prose paragraph a contributor previously appended to
    ``unreleased.md`` by hand. Bodies are stripped of surrounding blank
    lines and joined with exactly one blank line between them, matching the
    spacing every tagged release page already uses between its ``## ``
    sections.

    Args:
        fragments_dir: Directory holding fragment files.

    Returns:
        The rendered section text, or ``""`` when there are no fragments.
    """
    bodies = [p.read_text(encoding="utf-8").strip() for p in collect_fragments(fragments_dir)]
    return "\n\n".join(bodies)


def rotate_into(version_page: Path, fragments_dir: Path) -> RotationResult:
    """Delete consumed fragment files without modifying any version page.

    The version page already contains the curated notes. This call only
    removes the fragment files that were consumed, leaving the version page
    untouched. This ensures the version page reflects curated content only,
    not raw fragments.

    Args:
        version_page: The ``vX.Y.Z.md`` page being cut.
            Created if it does not yet exist; untouched if it does.
        fragments_dir: Directory holding fragment files to consume.

    Returns:
        A ``RotationResult`` naming the consumed fragment paths and the
        rendered text. Both are empty when there was nothing to rotate.
    """
    cut = REPO_ROOT / "scripts" / "release_cuts" / f"{version_page.stem}.toml"
    hold = tomllib.loads(cut.read_text(encoding="utf-8"))["hold"] if cut.is_file() else ()
    fragments = collect_fragments(fragments_dir, hold=hold)
    if not fragments:
        return RotationResult()

    if not version_page.exists():
        return RotationResult()

    for fragment in fragments:
        fragment.unlink()

    return RotationResult(consumed=fragments, rendered="")


def _replace_once(body: str, old: str, new: str, fragment: Path) -> str:
    if not old or body.count(old) != 1:
        raise ValueError(f"{fragment.name}: expected exactly one occurrence of {old!r}")
    return body.replace(old, new)


def assemble(repo: Path, version: str) -> None:
    """Cut a page from clean inputs and the corresponding checked-in TOML description."""
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError(f"expected a release name such as v4.0.0, got {version!r}")
    cut = tomllib.loads((repo / "scripts" / "release_cuts" / f"{version}.toml").read_text(encoding="utf-8"))
    notes = repo / "docs" / "release-notes"
    page = notes / f"{version}.md"
    if page.exists():
        raise ValueError(f"{page.name} already exists; start from the pre-cut tree")
    fragments_dir = notes / "fragments"
    hold = set(cut["hold"])
    missing = hold - {p.name for p in fragments_dir.iterdir()}
    if missing:
        raise ValueError(f"missing deferred fragments: {sorted(missing)}")
    fragments = collect_fragments(fragments_dir, hold=hold)
    wording = cut.get("wording", {})
    links = cut.get("relative_links", {})
    missing = (wording.keys() | links.keys()) - {p.name for p in fragments}
    if missing:
        raise ValueError(f"curation names unavailable fragments: {sorted(missing)}")

    unreleased = notes / "unreleased.md"
    header, separator, entries = unreleased.read_text(encoding="utf-8").partition("\n## ")
    parts = [f"# {version}"]
    if separator:
        parts.append("## " + entries.strip())
    for fragment in fragments:
        body = fragment.read_text(encoding="utf-8").strip()
        if fragment.name in wording:
            old, new = wording[fragment.name]
            body = _replace_once(body, old, new, fragment)
        for original in links.get(fragment.name, []):
            url = urlsplit(original)
            if not url.path or url.scheme or url.netloc or url.path.startswith("/"):
                raise ValueError(f"{fragment.name}: expected a relative document link, got {original!r}")
            moved_path = Path(os.path.relpath(fragment.parent / url.path, page.parent)).as_posix()
            moved_url = urlunsplit(url._replace(path=moved_path))
            body = _replace_once(body, f"]({original})", f"]({moved_url})", fragment)
        parts.append(body)
    if cut["limitations"]:
        parts.append("## Known limitations\n\n" + "\n".join(f"- {item}" for item in cut["limitations"]))

    nav = repo / "mkdocs.yml"
    nav_text = nav.read_text(encoding="utf-8")
    anchor = re.compile(r"^([ \t]*)- Unreleased: release-notes/unreleased\.md$", re.MULTILINE)
    target = f"release-notes/{page.name}"
    if target in nav_text or len(anchor.findall(nav_text)) != 1:
        raise ValueError(f"expected a single Unreleased navigation entry and no existing {target}")
    new_nav = anchor.sub(rf"\g<0>\n\1- {version}: {target}", nav_text)

    updates = {nav: new_nav, unreleased: header.rstrip("\n") + "\n"}
    _write_cut(page, "\n\n".join(parts) + "\n", updates, fragments)


def _write_cut(page: Path, body: str, updates: dict[Path, str], fragments: list[Path]) -> None:
    """Restore the pre-cut files if writing the page or retiring a fragment fails."""
    originals = {path: path.read_bytes() for path in [*updates, *fragments]}
    output = page.open("x", encoding="utf-8", newline="\n")
    touched: list[Path] = []
    try:
        with output:
            output.write(body)
        for path, text in updates.items():
            touched.append(path)
            path.write_text(text, encoding="utf-8", newline="\n")
        for fragment in fragments:
            fragment.unlink()
            touched.append(fragment)
    except OSError:
        for path in reversed(touched):
            if not path.exists() or path.read_bytes() != originals[path]:
                path.write_bytes(originals[path])
        page.unlink()
        raise


def notes_gate_ok(changed_files: Iterable[str]) -> bool:
    """Return whether a change set satisfies the release-notes gate.

    Accepts either form during the fragments transition window: an edit to
    ``docs/release-notes/unreleased.md``, or a new or changed fragment
    under ``docs/release-notes/fragments/``. Editing a tagged version page
    (``docs/release-notes/vX.Y.Z.md``) does not count -- that documents
    what already shipped, not the change this PR makes.

    Args:
        changed_files: Repo-relative paths, e.g. from
            ``git diff --name-only``. Windows-style separators are
            normalized before matching.

    Returns:
        True if at least one path satisfies the gate.
    """
    for raw in changed_files:
        path = raw.replace("\\", "/")
        if path == _UNRELEASED_REL:
            return True
        if path.startswith(_FRAGMENTS_REL_PREFIX) and path.endswith(".md"):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    assemble_p = sub.add_parser("assemble", help="Generate notes and navigation from the version's cut description.")
    assemble_p.add_argument("version", help="Release name such as v4.0.0.")

    rotate_p = sub.add_parser("rotate", help="Delete consumed fragments (version page already curated).")
    rotate_p.add_argument("version_page", type=Path, help="The vX.Y.Z.md page being cut.")
    rotate_p.add_argument("--fragments-dir", type=Path, default=FRAGMENTS_DIR)

    gate_p = sub.add_parser("check-gate", help="Exit 0 if the given changed files satisfy the release-notes gate.")
    gate_p.add_argument("changed_files", nargs="*", help="Repo-relative changed file paths.")

    args = parser.parse_args(argv)

    if args.command == "assemble":
        try:
            assemble(REPO_ROOT, args.version)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"error: {exc}\n")
        print(f"Assembled {args.version}")
        return 0

    if args.command == "rotate":
        result = rotate_into(args.version_page, args.fragments_dir)
        if result.consumed:
            names = ", ".join(p.name for p in result.consumed)
            print(f"Rotated {len(result.consumed)} fragment(s) into {args.version_page}: {names}")
        else:
            print(f"No fragments in {args.fragments_dir}; nothing to rotate.")
        return 0

    if notes_gate_ok(args.changed_files):
        print("Release-notes gate: OK")
        return 0
    print(
        "Release-notes gate: FAIL - no edit to docs/release-notes/unreleased.md "
        "and no fragment under docs/release-notes/fragments/. Add one of the two.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
