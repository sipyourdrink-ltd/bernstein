#!/usr/bin/env python
"""Repo-hygiene gate for issue #4500: a UTF-8 BOM in a committed text file.

Three pull requests in a row (#4429, #4430, #4493) arrived with ``EF BB BF``
at the start of a release-notes fragment. ``Repo hygiene`` passed all three:
the BOM was caught in review, and one such fragment would otherwise have
rendered into a release page with a stray glyph ahead of its first heading.

Scans the tracked files whose suffix we treat as text and fails if any begins
with the mark. Paths whose ``text`` attribute git reports as unset are exempt,
which is how ``.gitattributes`` spells "these bytes are not ours to police":
the demo receipt and its vectors are byte-exact fixtures whose signature dies
on any rewrite. The ``binary`` macro expands to ``-diff -merge -text``, so the
one query covers both spellings.

Run locally::

    uv run python scripts/check_bom.py

Exit codes:
  0 = no tracked text file starts with a BOM.
  1 = at least one does.

Root and path overrides for tests::

    uv run python scripts/check_bom.py --root path/to/repo
    uv run python scripts/check_bom.py --paths fragment.md pyproject.toml
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

BOM = b"\xef\xbb\xbf"

# Deliberately a suffix allowlist rather than a binary sniff. The gate exists
# for hand-written text that renders somewhere, and a sniff would have to
# guess about the fixtures ``.gitattributes`` already answers for.
TEXT_SUFFIXES = frozenset({".md", ".py", ".yaml", ".yml", ".toml", ".json"})

REMEDY = (
    "Strip the mark before committing. UTF-8 needs no byte-order mark, and a\n"
    "file that carries one renders it as a stray glyph ahead of its first\n"
    "line. Most editors offer 'UTF-8' against 'UTF-8 with BOM'; from a shell::\n"
    "\n"
    "    sed -i '1s/^\\xef\\xbb\\xbf//' <path>\n"
    "\n"
    "If the file is a byte-exact fixture whose leading bytes are load-bearing,\n"
    "mark it '-text' in .gitattributes instead, next to the demo-run vectors."
)


def starts_with_bom(path: Path) -> bool:
    """Return True if *path* begins with the UTF-8 byte-order mark."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(BOM)) == BOM
    except OSError as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
        return False


def tracked_text_files(root: Path) -> list[Path]:
    """Tracked paths under *root* whose suffix is in :data:`TEXT_SUFFIXES`.

    NUL-separated so a path containing a newline cannot split one entry into
    two, which would silently drop it from the sweep.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    names = [chunk for chunk in result.stdout.split(b"\0") if chunk]
    paths = [Path(name.decode("utf-8", "surrogateescape")) for name in names]
    return [path for path in paths if path.suffix in TEXT_SUFFIXES]


def exempt_paths(root: Path, paths: list[Path]) -> set[Path]:
    """Of *paths*, those whose ``text`` attribute git reports as unset."""
    if not paths:
        return set()
    stdin = b"\0".join(str(path).encode("utf-8", "surrogateescape") for path in paths)
    result = subprocess.run(
        ["git", "-C", str(root), "check-attr", "-z", "--stdin", "text"],
        input=stdin,
        capture_output=True,
        check=True,
    )
    # ``-z`` emits a flat NUL-separated stream of (path, attribute, value)
    # triples, so step through it three fields at a time.
    fields = result.stdout.split(b"\0")
    exempt: set[Path] = set()
    for index in range(0, len(fields) - 2, 3):
        name, _attribute, value = fields[index : index + 3]
        if value == b"unset":
            exempt.add(Path(name.decode("utf-8", "surrogateescape")))
    return exempt


def check(root: Path, paths: list[Path] | None = None) -> int:
    """Report tracked text files under *root* that begin with a BOM."""
    candidates = tracked_text_files(root) if paths is None else paths
    exempt = exempt_paths(root, candidates)
    findings = [path for path in candidates if path not in exempt and starts_with_bom(root / path)]

    if not findings:
        print("check_bom: OK (no tracked text file starts with a UTF-8 BOM)")
        return 0

    print(
        "check_bom: UTF-8 byte-order mark at the start of a committed text file:",
        file=sys.stderr,
    )
    for path in findings:
        print(f"  {path}", file=sys.stderr)
    print(f"\n{REMEDY}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Repository to scan (default: the current directory).",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        type=Path,
        default=None,
        help="Paths relative to --root to scan instead of every tracked text file.",
    )
    args = parser.parse_args(argv)
    return check(args.root, args.paths)


if __name__ == "__main__":
    raise SystemExit(main())
