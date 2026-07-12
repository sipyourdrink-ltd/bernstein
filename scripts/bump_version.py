#!/usr/bin/env python3
"""Single supported version-bump path for a Bernstein release.

A release bump is not just a ``pyproject.toml`` edit: the lockfile and the
distribution manifests (``server.json``, ``.plugin/plugin.json``) must move in
lockstep, or CI's drift gates fail after the fact and a version-bump PR can
merge green having run no tests. This script performs the bump deterministically
so every operator produces the byte-identical tree:

  1. rewrite ``project.version`` in ``pyproject.toml``
  2. ``uv lock`` so ``uv.lock`` pins the new version
  3. regenerate ``server.json`` and ``.plugin/plugin.json`` via
     ``scripts/gen_distribution_manifests.py``

Usage::

    python scripts/bump_version.py 3.4.5

Never hand-edit the manifests or the OCI ``packages[].version`` field: the
registry schema forbids a top-level version on the OCI package (the version
rides in the image tag instead), the generator owns that shape, and the unit
suite guards it. This script therefore delegates all manifest edits to the
generator rather than touching those files itself.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
GEN_MANIFESTS = REPO / "scripts" / "gen_distribution_manifests.py"

# Matches only the top-level ``version = "..."`` line (column 0). Table-scoped
# version keys inside pyproject are indented and are intentionally not touched.
_VERSION_RE = re.compile(r'^version = "[^"]*"$', re.MULTILINE)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.-]+)?$")


def set_pyproject_version(text: str, new_version: str) -> str:
    """Return *text* with the top-level ``version = "..."`` line set to *new_version*.

    Only the first ``^version = "..."`` line (the ``[project]`` version) is
    rewritten. Raises ``ValueError`` when no such line exists so a malformed
    pyproject fails loud instead of silently shipping the old version.
    """
    replacement = f'version = "{new_version}"'
    new_text, count = _VERSION_RE.subn(replacement, text, count=1)
    if count != 1:
        msg = 'no top-level `version = "..."` line found in pyproject.toml'
        raise ValueError(msg)
    return new_text


def _validate_version(version: str) -> str:
    """Return *version* when it is a plausible semver, else raise ``ValueError``."""
    if not _SEMVER_RE.match(version):
        msg = f"version must be semver (e.g. 3.4.5), got {version!r}"
        raise ValueError(msg)
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bump the Bernstein release version.")
    parser.add_argument("version", help="new semver version, e.g. 3.4.5")
    args = parser.parse_args(argv)

    try:
        version = _validate_version(args.version)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    original = PYPROJECT.read_text(encoding="utf-8")
    updated = set_pyproject_version(original, version)
    if updated != original:
        PYPROJECT.write_text(updated, encoding="utf-8")
        print(f"pyproject.toml -> version {version}")
    else:
        print(f"pyproject.toml already at version {version}")

    subprocess.run(["uv", "lock"], cwd=REPO, check=True)
    print("uv.lock regenerated")

    subprocess.run([sys.executable, str(GEN_MANIFESTS)], cwd=REPO, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
