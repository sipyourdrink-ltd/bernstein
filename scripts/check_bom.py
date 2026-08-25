#!/usr/bin/env python
"""Repo-hygiene sweep: reject committed text files starting with a UTF-8 BOM.

Three pull requests in a row (#4429, #4430, #4493) arrived with ``EF BB BF``
at the head of a committed release-notes fragment, mangled by a contributor's
shell. ``Repo hygiene`` passed all three; the BOM was caught only in review,
and one of those fragments would otherwise have rendered a stray ``\\ufeff``
onto a release page.

A BOM is invisible in most editors and in a GitHub diff, so this is exactly
the class of fault that has to be machine-checked - reading the file cannot
find it.

Scope
-----

Only paths git tracks, and only the text extensions listed in
:data:`TEXT_SUFFIXES`. Anything ``.gitattributes`` marks ``-text`` is exempt:
those are byte-exact fixtures (the demo receipt, the committed receipt
vectors) whose signatures are over the bytes, so their leading bytes are not
this check's business.

Exit status is 1 when any file is flagged, 0 otherwise. Findings are printed
as GitHub Actions ``::error file=…::`` annotations.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

#: The UTF-8 encoding of U+FEFF.
BOM = b"\xef\xbb\xbf"

#: Extensions swept. Deliberately a list rather than "everything not binary":
#: a new binary format should not start failing this check by default.
TEXT_SUFFIXES = frozenset({".md", ".py", ".yaml", ".yml", ".toml", ".json", ".txt", ".cfg", ".ini"})


def tracked_text_files(repo_root: Path) -> list[Path]:
    """Every git-tracked file whose suffix is in :data:`TEXT_SUFFIXES`."""
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=True,
        text=False,
    ).stdout
    names = [n.decode("utf-8", "surrogateescape") for n in out.split(b"\0") if n]
    return [repo_root / n for n in names if Path(n).suffix.lower() in TEXT_SUFFIXES]


def binary_marked_paths(repo_root: Path, paths: list[Path]) -> set[Path]:
    """Paths ``.gitattributes`` marks ``-text`` (or binary).

    Asks git rather than parsing ``.gitattributes``, so pattern semantics -
    precedence, negation, directory scoping - stay git's to define.
    """
    if not paths:
        return set()
    rel = [str(p.relative_to(repo_root)) for p in paths]
    proc = subprocess.run(
        ["git", "check-attr", "--stdin", "-z", "text"],
        cwd=repo_root,
        input="\0".join(rel).encode("utf-8"),
        capture_output=True,
        check=True,
    )
    fields = proc.stdout.split(b"\0")
    marked: set[Path] = set()
    # git emits <path>\0<attr>\0<value>\0 triples under -z.
    for i in range(0, len(fields) - 2, 3):
        path, _attr, value = fields[i], fields[i + 1], fields[i + 2]
        if value in (b"unset", b"set-binary"):
            marked.add(repo_root / path.decode("utf-8", "surrogateescape"))
    return marked


def find_bom_files(repo_root: Path) -> list[Path]:
    """Tracked, non-exempt text files whose first bytes are a UTF-8 BOM."""
    candidates = tracked_text_files(repo_root)
    exempt = binary_marked_paths(repo_root, candidates)
    flagged: list[Path] = []
    for path in candidates:
        if path in exempt or not path.is_file():
            continue
        try:
            with path.open("rb") as handle:
                if handle.read(len(BOM)) == BOM:
                    flagged.append(path)
        except OSError:
            # An unreadable tracked path is a different fault; hygiene is not
            # the place to fail the build over it.
            continue
    return sorted(flagged)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    flagged = find_bom_files(args.repo_root.resolve())
    for path in flagged:
        rel = path.relative_to(args.repo_root.resolve())
        print(f"::error file={rel}::{rel} starts with a UTF-8 byte-order mark (EF BB BF); strip it")
    if flagged:
        print(f"{len(flagged)} file(s) start with a UTF-8 BOM", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
