#!/usr/bin/env python3
"""Fail when a touched test module loses collected cases vs the merge base (#4873).

The zero-collection guard (#4834) catches a module emptied to nothing. This
check catches the residual shape: a module that still collects, but fewer
items than at the merge base.

Counts come from ``pytest --collect-only``, not AST ``def test_*`` counts, so
folding N one-off cases into one ``@pytest.mark.parametrize`` with N values
keeps the collected count stable and stays green without an override.

Override (PR body, reviewable)::

    test-count-drop: tests/unit/foo/test_bar.py -3

Allow that path's collected count to fall by **at most** 3. Overrides that
name a module with no drop are stale and fail the check. There is no
path-less global budget.

A deleted test module whose matching subject under ``src/`` was also deleted
in the same diff is carved out. Needs ``--base`` (CI merge base); without a
base the check skips with a clear message rather than a fake green.

Usage::

    python scripts/check_test_count_drop.py --base origin/main
    python scripts/check_test_count_drop.py --base "$BASE" --pr-body "$PR_BODY"
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

OVERRIDE_RE = re.compile(
    r"(?m)^[ \t]*test-count-drop:[ \t]+(\S+)[ \t]+-(\d+)[ \t]*$",
)
_TEST_NAME_RE = re.compile(r"^(?:test_(.+)|(.+)_test)$")


@dataclass
class DropReport:
    """Result of comparing collected counts for touched test modules."""

    drops: list[tuple[str, int, int]] = field(default_factory=list)  # path, base, head
    excused: list[tuple[str, int, int, int]] = field(default_factory=list)  # path, base, head, allowed
    carved: list[str] = field(default_factory=list)
    stale_overrides: list[str] = field(default_factory=list)
    skipped: str | None = None
    checked: int = 0


def is_test_module(path: str) -> bool:
    """Return True when *path* matches pytest's default ``python_files`` under tests/."""
    posix = path.replace("\\", "/")
    if not posix.startswith("tests/"):
        return False
    name = Path(posix).name
    if name in {"conftest.py", "__init__.py"}:
        return False
    if name.startswith("test_") and name.endswith(".py"):
        return True
    return name.endswith("_test.py")


def parse_overrides(pr_body: str) -> dict[str, int]:
    """Parse per-module ``test-count-drop: <path> -<N>`` lines from a PR body."""
    found: dict[str, int] = {}
    for match in OVERRIDE_RE.finditer(pr_body or ""):
        rel = match.group(1).replace("\\", "/")
        found[rel] = int(match.group(2))
    return found


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def changed_paths(repo: Path, base: str, head: str = "HEAD") -> tuple[set[str], set[str], set[str]]:
    """Return (modified, deleted, added) repo-relative paths for ``base...head``."""
    out = _git(repo, "diff", "--name-status", f"{base}...{head}")
    modified: set[str] = set()
    deleted: set[str] = set()
    added: set[str] = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R") and len(parts) >= 3:
            deleted.add(parts[1].replace("\\", "/"))
            added.add(parts[2].replace("\\", "/"))
            continue
        if status.startswith("C") and len(parts) >= 3:
            added.add(parts[2].replace("\\", "/"))
            continue
        path = parts[-1].replace("\\", "/")
        if status.startswith("D"):
            deleted.add(path)
        elif status.startswith("A"):
            added.add(path)
        else:
            modified.add(path)
    return modified, deleted, added


def subject_stem(test_path: str) -> str | None:
    """Return the production-module stem implied by a test path, if any."""
    match = _TEST_NAME_RE.match(Path(test_path).stem)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def subject_deleted(test_path: str, deleted_paths: set[str]) -> bool:
    """True when a matching ``src/**/<stem>.py`` was deleted in the same diff."""
    stem = subject_stem(test_path)
    if not stem:
        return False
    needle = f"/{stem}.py"
    return any(
        path.startswith("src/") and (path.endswith(needle) or path == f"src/{stem}.py") for path in deleted_paths
    )


def pytest_collect_count(path: Path, *, python: str = sys.executable, cwd: Path | None = None) -> int:
    """Return how many items pytest collects from *path*."""
    result = subprocess.run(
        [
            python,
            "-m",
            "pytest",
            str(path),
            "--collect-only",
            "-q",
            "--no-header",
            "--no-cov",
        ],
        cwd=cwd or path.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    blob = f"{result.stdout}\n{result.stderr}"
    if re.search(r"\bno tests collected\b", blob, re.IGNORECASE):
        return 0
    match = re.search(r"(\d+)\s+tests?\s+collected", blob, re.IGNORECASE)
    if match:
        return int(match.group(1))
    # pytest -q lists one node id per line when collection succeeds without a
    # summary line (or with an older summary format).
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("=") and "collected" not in line.lower()
    ]
    return len(lines)


def collect_count_at_revision(
    repo: Path,
    rev: str,
    rel_path: str,
    *,
    python: str = sys.executable,
) -> int | None:
    """Collect-count for ``rel_path`` as it existed at *rev*, or ``None`` if absent."""
    show = subprocess.run(
        ["git", "show", f"{rev}:{rel_path}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if show.returncode != 0:
        return None
    with tempfile.TemporaryDirectory(prefix="bernstein-test-count-") as tmp:
        target = Path(tmp) / Path(rel_path).name
        target.write_bytes(show.stdout)
        # Isolate from the live tree's conftest by collecting in the temp dir.
        return pytest_collect_count(target, python=python, cwd=Path(tmp))


def collect_count_at_head(repo: Path, rel_path: str, *, python: str = sys.executable) -> int | None:
    """Collect-count for ``rel_path`` in the working tree, or ``None`` if missing."""
    path = repo / rel_path
    if not path.is_file():
        return None
    return pytest_collect_count(path, python=python, cwd=repo)


def build_report(
    repo: Path,
    *,
    base: str | None,
    head: str = "HEAD",
    pr_body: str = "",
    python: str = sys.executable,
) -> DropReport:
    """Compare collected counts for test modules touched between *base* and *head*."""
    report = DropReport()
    if not base:
        report.skipped = "no merge base (--base); skip rather than fake-green"
        return report

    overrides = parse_overrides(pr_body)
    modified, deleted, added = changed_paths(repo, base, head)
    touched = {p for p in (modified | deleted | added) if is_test_module(p)}
    report.checked = len(touched)

    consumed: set[str] = set()
    for rel in sorted(touched):
        base_count = collect_count_at_revision(repo, base, rel, python=python)
        head_count = collect_count_at_head(repo, rel, python=python)
        if base_count is None:
            continue  # new test module at head — not a drop
        head_n = 0 if head_count is None else head_count
        if head_n >= base_count:
            continue
        drop = base_count - head_n
        if head_count is None and subject_deleted(rel, deleted):
            report.carved.append(rel)
            continue
        allowed = overrides.get(rel)
        if allowed is not None:
            consumed.add(rel)
            if drop <= allowed:
                report.excused.append((rel, base_count, head_n, allowed))
                continue
            # Override too small for the actual drop — still a failure.
            report.drops.append((rel, base_count, head_n))
            continue
        report.drops.append((rel, base_count, head_n))

    for rel, allowed in sorted(overrides.items()):
        if rel not in consumed:
            # Stale: named a module with no drop (or not in the touched set).
            report.stale_overrides.append(f"{rel} -{allowed}")

    return report


def format_report(report: DropReport) -> str:
    """Human-readable report for CI logs."""
    lines: list[str] = []
    if report.skipped:
        lines.append(f"SKIP: {report.skipped}")
        return "\n".join(lines)
    if report.drops:
        lines.append("Test modules lost collected cases vs merge base (issue #4873):")
        for rel, base_n, head_n in report.drops:
            lines.append(f"  {rel}: {base_n} -> {head_n} (drop {base_n - head_n})")
        lines.append(
            "Restore the cases, consolidate via parametrize (collected count must stay "
            "stable), carve out a deleted subject, or add a PR-body override: "
            "`test-count-drop: <path> -<N>`."
        )
    if report.stale_overrides:
        lines.append("Stale test-count-drop overrides (no drop for that path):")
        for entry in report.stale_overrides:
            lines.append(f"  {entry}")
    if report.excused:
        lines.append("Overrides consumed:")
        for rel, base_n, head_n, allowed in report.excused:
            lines.append(f"  {rel}: {base_n} -> {head_n} (allowed -{allowed})")
    if report.carved:
        lines.append("Deleted test modules carved out (subject also deleted):")
        for rel in report.carved:
            lines.append(f"  {rel}")
    if not report.drops and not report.stale_overrides:
        lines.append(f"OK: {report.checked} touched test modules; no unexplained collection drops.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("TEST_COUNT_DROP_BASE", ""), help="Merge-base SHA or ref")
    parser.add_argument("--head", default="HEAD", help="Head ref (default HEAD)")
    parser.add_argument(
        "--pr-body",
        default=os.environ.get("PR_BODY", ""),
        help="PR body text (or set PR_BODY); used for test-count-drop overrides",
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="Repository root")
    args = parser.parse_args(argv)

    base = args.base.strip() or None
    report = build_report(args.root, base=base, head=args.head, pr_body=args.pr_body)
    print(format_report(report))
    if report.skipped:
        return 0
    return 1 if report.drops or report.stale_overrides else 0


if __name__ == "__main__":
    raise SystemExit(main())
