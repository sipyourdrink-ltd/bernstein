#!/usr/bin/env python
"""Lint gate for issue #1723: bare ``except Exception:`` in route handlers.

Scans ``src/bernstein/core/routes/**.py`` and fails if any
``except Exception:`` clause lacks a justification marker on the line itself
or in the three preceding comment lines. Two markers are recognised:

  - ``intentional-broad-except``: legitimate best-effort path (telemetry,
    optional analytics, lineage append, etc.). The body should route any
    sensitive message through ``bernstein.core.security.sanitize.sanitize_log``.
  - ``bot-ack:`` followed by a short tag (e.g. ``bot-ack: legacy-shim``):
    used when a bot review has already acked the broad clause and a
    follow-up ticket exists.

Run locally::

    uv run python scripts/check_routes_broad_except.py

Exit codes:
  0 = all broad-except sites carry a marker, or none exist.
  1 = at least one unmarked broad-except clause was found.

Path overrides for tests::

    uv run python scripts/check_routes_broad_except.py --paths path1 path2
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_DEFAULT_ROOT = Path("src/bernstein/core/routes")
_BROAD_EXCEPT = re.compile(r"^\s*except\s+Exception\s*(?:as\s+\w+)?\s*:")
_MARKER = re.compile(r"intentional-broad-except|bot-ack:")
_LOOKBACK = 3


def _has_marker(lines: list[str], idx: int) -> bool:
    """Return True if ``lines[idx]`` (the ``except`` line) or any of the
    previous ``_LOOKBACK`` lines contain a justification marker."""
    start = max(0, idx - _LOOKBACK)
    return any(_MARKER.search(line) for line in lines[start : idx + 1])


def _scan_file(path: Path) -> list[tuple[Path, int, str]]:
    """Return a list of ``(path, lineno, source)`` for unmarked broad-except
    clauses in *path*."""
    findings: list[tuple[Path, int, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"warning: could not read {path}: {exc}", file=sys.stderr)
        return findings
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if _BROAD_EXCEPT.match(line) and not _has_marker(lines, idx):
            findings.append((path, idx + 1, line.rstrip()))
    return findings


def _iter_paths(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--paths",
        nargs="*",
        type=Path,
        default=[_DEFAULT_ROOT],
        help="Files or directories to scan (default: src/bernstein/core/routes).",
    )
    args = parser.parse_args(argv)

    findings: list[tuple[Path, int, str]] = []
    for path in _iter_paths(args.paths):
        findings.extend(_scan_file(path))

    if not findings:
        print("check_routes_broad_except: OK (no unmarked broad-except clauses)")
        return 0

    print("check_routes_broad_except: unmarked `except Exception:` clauses found:", file=sys.stderr)
    for path, lineno, source in findings:
        print(f"  {path}:{lineno}: {source}", file=sys.stderr)
    print(
        "\nEither narrow the clause to the realistic exceptions, or annotate the\n"
        "intentional breadth with a comment line within 3 lines containing\n"
        "'intentional-broad-except' (preferred) or 'bot-ack: <tag>'.\n"
        "See CONTRIBUTING.md (Broad-except policy) for the full convention.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
