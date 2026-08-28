#!/usr/bin/env python3
"""Docs drift checker.

Walks the doc inventory recorded in ``docs/playbooks/docs-drift.md`` and
emits a report describing which docs are likely stale relative to their
source-of-truth files.

Checks performed:

1. Every ``Source of truth`` path referenced in the playbook must exist
   under the repository root. If it does not, the doc that names it has
   drifted: either the doc references a moved/renamed module or the
   playbook itself needs updating.
2. The canonical AGENTS.md / CLAUDE.md / CONVENTIONS.md / `.goosehints`
   / `.cursor/rules/*.mdc` outputs must agree with
   ``bernstein agents-md verify``.
3. Every doc enumerated in the playbook must exist on disk.

Outputs a Markdown report on stdout. Exits non-zero on drift only when
called with ``--strict`` (used by the CI gate on pushes to main); for
pull requests the workflow runs without ``--strict`` so the comment
posts as advisory.

``--deleted-paths-file`` is a separate, narrower gate that does run on
pull requests. It takes the list of paths a change deletes and fails when
one of them is a source of truth the playbook names. Accumulated
staleness is advisory because the doc merely fell behind; a change that
deletes the file a doc is documented against introduces the error in that
same change, and the doc and the playbook row can be fixed alongside it.

Usage:

    uv run python scripts/check_docs_drift.py [--strict] [--json]
    uv run python scripts/check_docs_drift.py --deleted-paths-file deleted.txt
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = REPO_ROOT / "docs" / "playbooks" / "docs-drift.md"

# A markdown table row matches when it has at least 4 pipe-separated
# columns: doc / source / drift signal / remediation.
_ROW_RE = re.compile(r"^\|\s*`([^`|]+?)`\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*`([a-z-]+)`\s*\|\s*$")
# Within the "source of truth" column, repo-relative paths are written as
# ``backticked`` tokens. Any token that starts with src/, scripts/,
# templates/, web/, pyproject.toml, .sdd/, .github/, .importlinter,
# release-please-*, .cursor/, or a docs/ subdir is treated as a path.
_PATH_RE = re.compile(r"`([^`\s]+)`")


@dataclass
class DocRow:
    doc: str
    sources_raw: str
    drift_signal: str
    remediation: str
    section: str = ""

    @property
    def source_paths(self) -> list[str]:
        out: list[str] = []
        for match in _PATH_RE.finditer(self.sources_raw):
            tok = match.group(1)
            if tok.endswith(".py") or "/" in tok or tok.endswith(".toml"):
                out.append(tok.rstrip("/"))
        return out


@dataclass
class DriftReport:
    missing_sources: list[tuple[DocRow, str]] = field(default_factory=list)
    missing_docs: list[DocRow] = field(default_factory=list)
    agents_md_drift: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.missing_sources and not self.missing_docs and not self.agents_md_drift


def parse_playbook(path: Path) -> list[DocRow]:
    rows: list[DocRow] = []
    section = ""
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("###"):
            section = line.lstrip("# ").strip()
            continue
        match = _ROW_RE.match(line)
        if not match:
            continue
        doc, src, drift, remediation = match.groups()
        # Skip the legend table at the top of the playbook (its first
        # column lists remediation tokens, not real doc paths). The
        # legend has no real source path, only descriptive prose, so the
        # row is filtered out by checking the second column for an
        # explicit "What it runs" header on the surrounding section.
        if section.lower().startswith("how agents pick"):
            continue
        rows.append(
            DocRow(
                doc=doc.strip(),
                sources_raw=src.strip(),
                drift_signal=drift.strip(),
                remediation=remediation.strip(),
                section=section,
            )
        )
    return rows


def _doc_path(row: DocRow) -> Path:
    # Root-level docs are written as bare filenames (README.md, ...).
    # Everything else is repo-relative (docs/concepts/foo.md).
    if "/" in row.doc:
        return REPO_ROOT / row.doc
    if row.section.lower().startswith("root"):
        return REPO_ROOT / row.doc
    # Heuristic fallback: section names like "docs/concepts/" map a
    # bare filename row to that subdir.
    if row.section.startswith("`docs/") and row.section.endswith("`"):
        subdir = row.section.strip("`")
        return REPO_ROOT / subdir / row.doc
    return REPO_ROOT / row.doc


# Path prefixes that are runtime state (gitignored) or output artefacts;
# their absence in the working tree is expected and does not indicate drift.
_RUNTIME_PREFIXES = (".sdd/", ".bernstein/", "dist/", "build/", "node_modules/")


def check_sources(rows: list[DocRow]) -> list[tuple[DocRow, str]]:
    missing: list[tuple[DocRow, str]] = []
    for row in rows:
        if row.remediation == "static":
            continue
        for src in row.source_paths:
            if any(src.startswith(prefix) for prefix in _RUNTIME_PREFIXES):
                continue
            full = REPO_ROOT / src
            if not full.exists():
                missing.append((row, src))
    return missing


def sources_deleted_by(deleted_paths: Iterable[str], rows: list[DocRow]) -> list[tuple[DocRow, str]]:
    """Return the (row, source) pairs whose source is in ``deleted_paths``.

    ``deleted_paths`` are repo-relative, as ``git diff --diff-filter=D
    --name-only`` prints them. ``static`` rows are skipped for the same
    reason :func:`check_sources` skips them.
    """
    deleted = {p.strip().rstrip("/") for p in deleted_paths if p.strip()}
    hits: list[tuple[DocRow, str]] = []
    for row in rows:
        if row.remediation == "static":
            continue
        for src in row.source_paths:
            if src in deleted:
                hits.append((row, src))
    return hits


def check_docs(rows: list[DocRow]) -> list[DocRow]:
    missing: list[DocRow] = []
    for row in rows:
        path = _doc_path(row)
        if not path.exists():
            missing.append(row)
    return missing


def check_agents_md() -> list[str]:
    """Run ``bernstein agents-md verify`` and report drift lines.

    The check is best-effort. If the CLI is not installed (CI runs
    without the bernstein package in some matrices) it returns an empty
    list rather than failing the gate.
    """
    try:
        result = subprocess.run(
            ["uv", "run", "--quiet", "bernstein", "agents-md", "verify"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode == 0:
        return []
    lines = [
        line
        for line in (result.stdout + result.stderr).splitlines()
        if "drift" in line.lower() or "out of sync" in line.lower()
    ]
    return lines or ["bernstein agents-md verify exited non-zero"]


def render_markdown(report: DriftReport) -> str:
    parts: list[str] = ["# Docs drift report\n"]
    if report.is_clean:
        parts.append("No drift detected. Docs are in sync with code.\n")
        return "".join(parts)

    if report.agents_md_drift:
        parts.append("## AGENTS.md / CLAUDE.md / CONVENTIONS.md sync drift\n\n")
        for line in report.agents_md_drift:
            parts.append(f"- {line}\n")
        parts.append("\nRun `uv run bernstein agents-md sync` to refresh.\n\n")

    if report.missing_sources:
        parts.append("## Doc references a missing source-of-truth path\n\n")
        parts.append("| Doc | Missing source | Remediation |\n")
        parts.append("|-----|----------------|-------------|\n")
        for row, src in report.missing_sources:
            parts.append(f"| `{row.doc}` | `{src}` | `{row.remediation}` |\n")
        parts.append(
            "\nEither the doc references a moved or renamed module, or the "
            "drift playbook needs a row update. Open the doc, check the "
            "source-of-truth column in `docs/playbooks/docs-drift.md`, and "
            "fix the stale path.\n\n"
        )

    if report.missing_docs:
        parts.append("## Doc enumerated in the playbook is missing on disk\n\n")
        for row in report.missing_docs:
            parts.append(f"- `{row.doc}` (section: {row.section})\n")
        parts.append("\nEither restore the doc or remove the row from `docs/playbooks/docs-drift.md`.\n\n")

    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when drift is detected (used by the main-branch gate).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON summary instead of the Markdown report.",
    )
    parser.add_argument(
        "--deleted-paths-file",
        type=Path,
        help=(
            "File of repo-relative paths a change deletes, one per line. "
            "Exit 1 when any of them is a source of truth the playbook names."
        ),
    )
    args = parser.parse_args()

    rows = parse_playbook(PLAYBOOK)
    if not rows:
        print("ERROR: could not parse any doc rows from", PLAYBOOK, file=sys.stderr)
        return 1

    if args.deleted_paths_file is not None:
        deleted = args.deleted_paths_file.read_text(encoding="utf-8").splitlines()
        hits = sources_deleted_by(deleted, rows)
        if not hits:
            print("No deleted path is a documented source of truth.")
            return 0
        for row, src in hits:
            print(
                f"ERROR: `{src}` is deleted here and is the source of truth for `{row.doc}`. "
                f"Update the doc and its row in {PLAYBOOK.relative_to(REPO_ROOT)} in this change.",
                file=sys.stderr,
            )
        return 1

    report = DriftReport(
        missing_sources=check_sources(rows),
        missing_docs=check_docs(rows),
        agents_md_drift=check_agents_md(),
    )

    if args.json:
        payload = {
            "clean": report.is_clean,
            "missing_sources": [{"doc": row.doc, "source": src} for row, src in report.missing_sources],
            "missing_docs": [row.doc for row in report.missing_docs],
            "agents_md_drift": report.agents_md_drift,
            "rows_checked": len(rows),
        }
        print(json.dumps(payload, indent=2))
    else:
        print(render_markdown(report))

    if args.strict and not report.is_clean:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
