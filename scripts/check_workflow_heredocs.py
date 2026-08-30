#!/usr/bin/env python
"""Repo-hygiene gate: a Python heredoc in a workflow must use a quoted delimiter.

``volunteer-verify.yml`` fed its check-run script through ``python << EOF``.
An unquoted delimiter makes the shell expand the body before Python sees it,
so the step's source was assembled by string substitution: values landed
inside Python string literals, and one call argument was written as a bare
``CONCLUSION`` rather than ``'$CONCLUSION'``. The step died on ``NameError``
the first time it ran after merge, and every pull request opened afterwards
carried the red check until the workflow was corrected.

The quoted form (``python << 'PYEOF'``) passes the body through untouched and
forces the script to read its inputs from ``os.environ``, where a value
containing a quote or a newline cannot rewrite the program. Both surviving
heredocs in that workflow already did this; the broken step was the only one
that deviated.

Scans the workflow directory and fails on any heredoc that *is* a Python
program and whose delimiter is unquoted. A heredoc handed to a script as
stdin data (``python tool.py log <<JSON``) is left alone: there the shell
substitution is the point, and the body is never parsed as Python.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ``uv run python << 'PYEOF'``, ``PYTEST_EXIT_CODE="$rc" python3 <<-EOF`` and
# the like: an interpreter word carrying no script argument, so the heredoc
# body is the program itself, then the operator, then a delimiter that is
# quoted only when the body is meant to survive the shell verbatim.
HEREDOC = re.compile(r"(?:^|[\s;|&(])(?:python[0-9.]*)(?:\s+-\S+)*\s*<<-?\s*(?P<delim>\S+)")

WORKFLOW_SUFFIXES = (".yml", ".yaml")


def unquoted_heredocs(text: str) -> list[tuple[int, str]]:
    """Return ``(line number, delimiter)`` for each unquoted Python heredoc."""
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = HEREDOC.search(line)
        if match is None:
            continue
        delim = match.group("delim")
        if delim[:1] in {"'", '"'}:
            continue
        found.append((lineno, delim))
    return found


def workflow_files(root: Path) -> list[Path]:
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    return sorted(p for p in workflows.iterdir() if p.suffix in WORKFLOW_SUFFIXES)


def check(root: Path) -> int:
    violations: list[str] = []
    for path in workflow_files(root):
        for lineno, delim in unquoted_heredocs(path.read_text(encoding="utf-8")):
            rel = path.relative_to(root)
            violations.append(f"{rel}:{lineno}: python heredoc delimiter {delim} is not quoted")

    if not violations:
        return 0

    print("Unquoted Python heredoc in a workflow:", file=sys.stderr)
    for violation in violations:
        print(f"  {violation}", file=sys.stderr)
    print(
        "\nThe shell expands an unquoted heredoc body before Python parses it, so the "
        "step's source is built by substitution. Quote the delimiter (<< 'PYEOF') and "
        "read the values from os.environ instead.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="Repository to scan (default: the current directory).",
    )
    args = parser.parse_args(argv)
    return check(args.root)


if __name__ == "__main__":
    raise SystemExit(main())
