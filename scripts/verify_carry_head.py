#!/usr/bin/env python3
"""Validate the exact carry commit before publishing it to upstream."""

import argparse
import re
import subprocess
from pathlib import Path


def verify_carry_head(worktree: Path, main: str, original: str, head: str) -> None:
    """Require both histories and the trusted GitHub workflow tree."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "--no-replace-objects", "-C", str(worktree), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    for sha in (main, original, head):
        if re.fullmatch(r"[0-9a-f]{40}", sha) is None or git("cat-file", "-t", sha) != "commit":
            raise ValueError("expected an exact commit SHA")
    for sha, source in ((main, "trusted main"), (original, "original contributor head")):
        try:
            git("merge-base", "--is-ancestor", sha, head)
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"carry head does not preserve {source}") from exc
    trusted_tree = git("rev-parse", "--verify", f"{main}:.github")
    if git("cat-file", "-t", trusted_tree) != "tree" or git("rev-parse", "--verify", f"{head}:.github") != trusted_tree:
        raise ValueError("carry head does not have the trusted GitHub workflow tree")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worktree", type=Path)
    parser.add_argument("main", help="current upstream main SHA verified by the publisher")
    parser.add_argument("original", help="pinned head SHA of the original fork PR")
    parser.add_argument("head", help="exact reviewed commit to publish")
    args = parser.parse_args()
    try:
        verify_carry_head(args.worktree, args.main, args.original, args.head)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"carry head rejected: {exc}\n")


if __name__ == "__main__":
    main()
