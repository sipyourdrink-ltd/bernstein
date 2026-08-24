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

This script is the rotation step that used to be entirely by hand (see
``docs/operations/release.md``, "Release notes"): it concatenates the
fragments in deterministic filename order, appends the rendered section onto
the version page a release cuts, and deletes the consumed fragments so the
version-page edit and the cleanup land in the same commit. Editing
``docs/release-notes/unreleased.md`` directly still works during the
transition; ``notes_gate_ok`` accepts either form.

Usage::

    python scripts/rotate_release_notes.py rotate docs/release-notes/v3.18.0.md
    python scripts/rotate_release_notes.py check-gate <changed-file> [<changed-file> ...]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

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


def collect_fragments(fragments_dir: Path) -> list[Path]:
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
    return sorted(p for p in fragments_dir.iterdir() if p.is_file() and p.suffix == ".md")


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
    """Append the rendered fragments section to ``version_page`` and delete them.

    Both edits happen in this one call -- the version page gains the
    section and the consumed fragments are removed from disk -- so a
    release PR commits them together and never carries a page section with
    no matching fragment, or a fragment nothing rendered.

    Args:
        version_page: The ``vX.Y.Z.md`` page the release is cutting.
            Created if it does not yet exist; appended to (after a blank
            line) if it does.
        fragments_dir: Directory holding fragment files to consume.

    Returns:
        A ``RotationResult`` naming the consumed fragment paths and the
        rendered text. Both are empty when there was nothing to rotate.
    """
    fragments = collect_fragments(fragments_dir)
    if not fragments:
        return RotationResult()

    section = render_fragments(fragments_dir)
    existing = version_page.read_text(encoding="utf-8") if version_page.exists() else ""
    updated = existing.rstrip("\n") + "\n\n" + section + "\n" if existing.strip() else section + "\n"
    version_page.write_text(updated, encoding="utf-8")

    for fragment in fragments:
        fragment.unlink()

    return RotationResult(consumed=fragments, rendered=section)


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

    rotate_p = sub.add_parser("rotate", help="Concatenate fragments into a version page and delete them.")
    rotate_p.add_argument("version_page", type=Path, help="The vX.Y.Z.md page being cut.")
    rotate_p.add_argument("--fragments-dir", type=Path, default=FRAGMENTS_DIR)

    gate_p = sub.add_parser("check-gate", help="Exit 0 if the given changed files satisfy the release-notes gate.")
    gate_p.add_argument("changed_files", nargs="*", help="Repo-relative changed file paths.")

    args = parser.parse_args(argv)

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
