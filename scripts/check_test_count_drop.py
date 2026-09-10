#!/usr/bin/env python3
"""Fail when a touched test module loses collected cases vs the merge base (#4873).

The zero-collection guard (#4834) catches a module emptied to nothing. This
check catches the residual shape: a module that still collects, but fewer
items than at the merge base.

Counts come from ``pytest --collect-only``, not AST ``def test_*`` counts, so
folding N one-off cases into one ``@pytest.mark.parametrize`` with N values
keeps the collected count stable and stays green without an override.

Base and head are collected under the **same** isolated temp environment
(``git show`` bytes into sibling files, one pytest invocation style) so the
delta measures the diff, not the ambient tree. ``PYTHONPATH`` is pointed at
the real repo checkout so a module that imports a sibling ``tests/`` helper
by its repo-relative dotted path still resolves; the tempdir itself stays a
single-file island so ambient conftest / fixture drift cannot leak in.

Outcome words (never collapse these)::

    OK       compared touched modules; no unexplained drop
    NOT_RUN  no merge base — guard did not compare (not a clean bill of health)
    FAIL     unexplained drop and/or stale override

A module that fails to import collects zero; the message names
``import_error`` so it is not read as a silent false-positive count drop.

Override (reviewable), in the PR body **or** in any commit message in the
compared range::

    test-count-drop: tests/unit/foo/test_bar.py -3

Allow that path's collected count to fall by **at most** 3. Overrides that
name a module with no drop are stale and fail the check. There is no
path-less global budget.

Declare it in a commit message when the drop must survive the merge queue.
A ``merge_group`` build carries no ``pull_request`` payload, so the PR body
is empty in the one lane that gates the merge; a body-only override goes
green on the PR lane and then fails the queue, taking every entry stacked
behind it down with it. Commits are present in both lanes and cannot be
edited after the merge, so the reason the cases went away stays on the
record.

A deleted test module whose matching subject under ``src/`` was also deleted
in the same diff is carved out.

Policy choice: drops fail outright against the merge base (no committed
ratchet baseline). Stricter on refactors; override is the escape hatch.

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
from typing import Literal

REPO_ROOT = Path(__file__).resolve().parent.parent

OVERRIDE_RE = re.compile(
    r"(?m)^[ \t]*test-count-drop:[ \t]+(\S+)[ \t]+-(\d+)[ \t]*$",
)
_TEST_NAME_RE = re.compile(r"^(?:test_(.+)|(.+)_test)$")
_IMPORT_ERROR_RE = re.compile(
    r"(ImportError|ModuleNotFoundError|ERROR collecting|Interrupted:)",
    re.IGNORECASE,
)

CollectStatus = Literal["ok", "missing", "import_error"]


@dataclass(frozen=True)
class CollectResult:
    """One collect-only attempt under the shared isolation environment."""

    status: CollectStatus
    count: int = 0
    detail: str = ""


@dataclass
class DropReport:
    """Result of comparing collected counts for touched test modules."""

    # path, base_count, head_count, head_status
    drops: list[tuple[str, int, int, str]] = field(default_factory=list)
    excused: list[tuple[str, int, int, int]] = field(default_factory=list)
    carved: list[str] = field(default_factory=list)
    stale_overrides: list[str] = field(default_factory=list)
    not_run: str | None = None
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


def commit_message_text(repo: Path, base: str, head: str = "HEAD") -> str:
    """Return the concatenated commit messages of ``base..head``.

    Second override channel, and the only one that survives the merge queue:
    a ``merge_group`` build has no ``pull_request`` payload, so ``PR_BODY``
    is empty there. Reading the commits keeps one declaration readable in
    both lanes without a token or an API call, and keeps it immutable — a PR
    body edited after the merge erases the reason the cases went away.
    """
    try:
        return _git(repo, "log", "--format=%B", f"{base}..{head}")
    except subprocess.CalledProcessError:
        return ""


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


def _parse_collect_output(result: subprocess.CompletedProcess[str]) -> CollectResult:
    """Interpret pytest --collect-only stdout/stderr into a :class:`CollectResult`."""
    blob = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 and _IMPORT_ERROR_RE.search(blob):
        snippet = next(
            (line.strip() for line in blob.splitlines() if line.strip()),
            "collection failed",
        )
        return CollectResult(status="import_error", count=0, detail=snippet[:200])
    if re.search(r"\bno tests collected\b", blob, re.IGNORECASE):
        return CollectResult(status="ok", count=0)
    match = re.search(r"(\d+)\s+tests?\s+collected", blob, re.IGNORECASE)
    if match:
        return CollectResult(status="ok", count=int(match.group(1)))
    if result.returncode != 0:
        snippet = next(
            (line.strip() for line in blob.splitlines() if line.strip()),
            "collection failed",
        )
        return CollectResult(status="import_error", count=0, detail=snippet[:200])
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("=") and "collected" not in line.lower()
    ]
    return CollectResult(status="ok", count=len(lines))


def pytest_collect_file(path: Path, *, python: str, cwd: Path, import_root: Path) -> CollectResult:
    """Run collect-only on *path* with cwd=*cwd* (shared isolation root).

    ``import_root`` (the real repo checkout) goes on ``PYTHONPATH`` so a module
    that imports a sibling test helper by its repo-relative dotted path (e.g.
    ``from tests.unit._adapter_test_helpers import ...``, ~20 files do this
    today) still resolves. The isolation tempdir holds only this one
    materialised file with no ``tests`` package around it, so without this the
    import fails and a stable module reads as every case having disappeared.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(import_root), existing]))
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
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return _parse_collect_output(result)


def _git_show_bytes(repo: Path, rev: str, rel_path: str) -> bytes | None:
    show = subprocess.run(
        ["git", "show", f"{rev}:{rel_path}"],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    if show.returncode != 0:
        return None
    return show.stdout


def collect_pair(
    repo: Path,
    *,
    base: str,
    head: str,
    rel_path: str,
    python: str = sys.executable,
) -> tuple[CollectResult, CollectResult]:
    """Collect base and head under one shared temp environment.

    Both revisions are materialised as sibling files and collected with the
    same interpreter and cwd, so the delta is the file content, not ambient
    conftest / import-path drift between worktree and isolation.
    """
    base_bytes = _git_show_bytes(repo, base, rel_path)
    head_bytes = _git_show_bytes(repo, head, rel_path)
    with tempfile.TemporaryDirectory(prefix="bernstein-test-count-") as tmp:
        root = Path(tmp)
        base_path = root / f"base_{Path(rel_path).name}"
        head_path = root / f"head_{Path(rel_path).name}"
        if base_bytes is None:
            base_result = CollectResult(status="missing")
        else:
            base_path.write_bytes(base_bytes)
            base_result = pytest_collect_file(base_path, python=python, cwd=root, import_root=repo)
        if head_bytes is None:
            head_result = CollectResult(status="missing")
        else:
            head_path.write_bytes(head_bytes)
            head_result = pytest_collect_file(head_path, python=python, cwd=root, import_root=repo)
        return base_result, head_result


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
        report.not_run = "no merge base (--base); guard did not compare"
        return report

    overrides = parse_overrides(f"{pr_body}\n{commit_message_text(repo, base, head)}")
    modified, deleted, added = changed_paths(repo, base, head)
    touched = {p for p in (modified | deleted | added) if is_test_module(p)}
    report.checked = len(touched)

    consumed: set[str] = set()
    for rel in sorted(touched):
        base_result, head_result = collect_pair(repo, base=base, head=head, rel_path=rel, python=python)
        if base_result.status == "missing":
            continue  # new at head — not a drop
        base_n = base_result.count
        if head_result.status == "missing":
            head_n = 0
            head_status = "missing"
        else:
            head_n = head_result.count
            head_status = head_result.status
        if head_n >= base_n and head_status == "ok":
            continue
        if head_n >= base_n:
            continue
        drop = base_n - head_n
        if head_result.status == "missing" and subject_deleted(rel, deleted):
            report.carved.append(rel)
            continue
        cause = head_status if head_status != "ok" else "count_drop"
        allowed = overrides.get(rel)
        if allowed is not None:
            consumed.add(rel)
            if drop <= allowed:
                report.excused.append((rel, base_n, head_n, allowed))
                continue
            report.drops.append((rel, base_n, head_n, cause))
            continue
        report.drops.append((rel, base_n, head_n, cause))

    for rel, allowed in sorted(overrides.items()):
        if rel not in consumed:
            report.stale_overrides.append(f"{rel} -{allowed}")

    return report


def format_report(report: DropReport) -> str:
    """Human-readable report for CI logs."""
    lines: list[str] = []
    if report.not_run is not None:
        # Distinct from OK: an unrun guard must not read as a clean bill of health.
        lines.append(f"NOT_RUN: {report.not_run}")
        return "\n".join(lines)
    if report.drops:
        lines.append("FAIL: test modules lost collected cases vs merge base (issue #4873):")
        for rel, base_n, head_n, cause in report.drops:
            if cause == "import_error":
                lines.append(
                    f"  {rel}: {base_n} -> {head_n} (drop {base_n - head_n}; "
                    "cause=import_error — module failed to import under collect-only, "
                    "not necessarily deleted tests)"
                )
            elif cause == "missing":
                lines.append(f"  {rel}: {base_n} -> 0 (file deleted at head)")
            else:
                lines.append(f"  {rel}: {base_n} -> {head_n} (drop {base_n - head_n}; cause=count_drop)")
        lines.append(
            "Restore the cases, consolidate via parametrize (collected count must stay "
            "stable), carve out a deleted subject, or declare an override: "
            "`test-count-drop: <path> -<N>`. Put it in a commit message, not only in "
            "the PR body: the merge-queue build has no PR body to read, so a body-only "
            "override passes the PR lane and then fails here."
        )
    if report.stale_overrides:
        lines.append("FAIL: stale test-count-drop overrides (no drop for that path):")
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
    if report.not_run is not None:
        return 0
    return 1 if report.drops or report.stale_overrides else 0


if __name__ == "__main__":
    raise SystemExit(main())
