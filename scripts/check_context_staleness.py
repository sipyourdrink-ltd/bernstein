#!/usr/bin/env python3
"""Context-file staleness checker.

The repo's context-file gates check internal consistency (``bernstein
agents-md verify``) and referential integrity (``check_docs_drift.py``).
Neither notices the third failure mode: a subtree churns for weeks while
the curated context file that describes it stays byte-identical. The
generator keeps module tables fresh mechanically, but curated content
(nested ``AGENTS.md`` files, committed ``.sdd/agents-md`` overlays)
asserts invariants the generator cannot derive, and those age silently.

This script derives a staleness report from git history alone. For each
curated context file it computes:

1. The last commit that touched the file.
2. The net diff under the file's scope since that commit (files
   changed, insertions + deletions, ``*.py`` modules added or removed).
3. The commits in that range, ranked by churn, so every flag names the
   exact commits that aged the file.

A nested ``AGENTS.md`` is flagged when the net line churn under its
directory reaches ``SUBTREE_LINE_THRESHOLD``, or when any ``*.py``
module was added or removed under it (a public-surface event the prose
almost certainly describes). A committed ``.sdd/agents-md`` overlay is
repo-scoped prose, so it is flagged on line churn only, at the larger
``REPO_SCOPE_LINE_THRESHOLD``.

The computation is a pure function of the repository state: two runs on
the same repo state produce byte-identical output on any machine. Rename
detection is disabled explicitly (``--no-renames``) so the report cannot
vary with git version or local diff configuration; a rename therefore
surfaces as one module-removed plus one module-added event, which is the
staleness signal we want (the prose likely names the old path). Touching
a context file in a commit — updating it, or a reconfirmation-only edit
— resets its clock, so the review of "is this still true?" always
leaves a commit.

Exits non-zero on flags only with ``--strict`` (the scheduled sweep uses
it to decide whether to update the tracking issue); without ``--strict``
the report is advisory and the exit code is always 0. Exit code 2 means
the repository cannot be assessed (shallow clone: truncated history
would silently corrupt every number).

Usage:

    uv run python scripts/check_context_staleness.py \
        [--strict] [--json] [--ref REF] [--baseline REF] [--repo PATH]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Top-level roots searched for nested per-directory ``AGENTS.md`` files.
# Mirrors ``_DIRECTORY_CONTEXT_ROOTS`` in
# ``src/bernstein/core/knowledge/agents_md_generator.py``: a fixed
# allowlist keeps the report byte-stable across environments.
DIRECTORY_CONTEXT_ROOTS: tuple[str, ...] = ("src", "tests")

# Curated overlay directory (repo-scoped context, when committed).
OVERLAY_DIR = ".sdd/agents-md"

# Net insertions + deletions under a nested AGENTS.md's directory since
# the file's last commit at which the file is flagged.
SUBTREE_LINE_THRESHOLD = 200

# Committed overlays describe the whole repository, so they age slower
# per line of churn; the flag threshold is correspondingly larger.
REPO_SCOPE_LINE_THRESHOLD = 2000

# How many top-churn commits each flagged entry names.
TOP_COMMITS = 5

# Short-sha display length. Fixed here (not ``%h``) because git's
# abbreviation length varies with object count and ``core.abbrev``.
SHORT_SHA_LEN = 12

# Explicit config overrides so local git configuration cannot change the
# output. ``--no-renames`` is passed per diff/log call below; ``log.follow``
# is forced off because it silently rewrites single-path ``git log`` queries
# (the last-touch lookup) on machines that enable it.
_GIT_OVERRIDES = (
    "-c",
    "core.quotePath=false",
    "-c",
    "log.showSignature=false",
    "-c",
    "log.follow=false",
)

_COMMIT_HEADER_RE = re.compile(r"^[0-9a-f]{40}\t")
_NUMSTAT_RE = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")


class GitError(RuntimeError):
    """A git invocation failed."""


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` with pinned config overrides; return stdout."""
    result = subprocess.run(
        ["git", *_GIT_OVERRIDES, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


@dataclass(frozen=True)
class ContextFile:
    """A curated context file and the subtree it describes."""

    path: str
    scope: str  # directory prefix, or "" for repo scope
    repo_scoped: bool


@dataclass(frozen=True)
class CommitChurn:
    sha: str
    subject: str
    lines: int


@dataclass
class StalenessEntry:
    context: ContextFile
    last_commit: str
    last_commit_date: str
    last_commit_subject: str
    commits_since: int = 0
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    modules_added: list[str] = field(default_factory=list)
    modules_removed: list[str] = field(default_factory=list)
    top_commits: list[CommitChurn] = field(default_factory=list)
    newly_flagged: bool = False

    @property
    def total_lines(self) -> int:
        return self.insertions + self.deletions

    @property
    def line_threshold(self) -> int:
        return REPO_SCOPE_LINE_THRESHOLD if self.context.repo_scoped else SUBTREE_LINE_THRESHOLD

    @property
    def flagged(self) -> bool:
        if self.total_lines >= self.line_threshold:
            return True
        if self.context.repo_scoped:
            # Repo-scoped overlays would flag on every added module
            # anywhere in the tree; only line churn is meaningful.
            return False
        return bool(self.modules_added or self.modules_removed)


@dataclass
class StalenessReport:
    ref: str
    baseline: str | None
    entries: list[StalenessEntry] = field(default_factory=list)

    @property
    def flagged(self) -> list[StalenessEntry]:
        return [entry for entry in self.entries if entry.flagged]

    @property
    def newly_flagged(self) -> list[StalenessEntry]:
        return [entry for entry in self.flagged if entry.newly_flagged]

    @property
    def is_clean(self) -> bool:
        return not self.flagged


def _is_excluded(path: str) -> bool:
    """Context files themselves never count as code churn."""
    return path.rsplit("/", 1)[-1] == "AGENTS.md" or path.startswith(f"{OVERLAY_DIR}/")


def list_context_files(repo: Path, ref: str) -> list[ContextFile]:
    """Enumerate curated context files tracked at ``ref``.

    Derived from ``git ls-tree`` so untracked scratch files can never
    enter the report (the same rule the agents-md generator applies).
    """
    tracked = _git(repo, "ls-tree", "-r", "--name-only", "-z", ref).split("\0")
    out: list[ContextFile] = []
    for path in sorted(p for p in tracked if p):
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in DIRECTORY_CONTEXT_ROOTS and parts[-1] == "AGENTS.md":
            out.append(ContextFile(path=path, scope="/".join(parts[:-1]), repo_scoped=False))
        elif path.startswith(f"{OVERLAY_DIR}/") and path.endswith(".md"):
            out.append(ContextFile(path=path, scope="", repo_scoped=True))
    return out


def _last_touch(repo: Path, ref: str, path: str) -> tuple[str, str, str] | None:
    """Return (sha, committer-date, subject) of the last commit touching ``path``."""
    out = _git(repo, "log", "-1", "--no-renames", "--format=%H%x09%cI%x09%s", ref, "--", path)
    line = out.strip("\n")
    if not line:
        return None
    parts = line.split("\t", 2)
    sha = parts[0]
    date = parts[1] if len(parts) > 1 else ""
    subject = parts[2] if len(parts) > 2 else ""
    return sha, date, subject


def _net_diff(repo: Path, last: str, ref: str, scope: str) -> tuple[int, int, int, list[str], list[str]]:
    """Net (files, insertions, deletions, modules added, modules removed)."""
    pathspec = [f"{scope}/"] if scope else []
    numstat = _git(repo, "diff", "--numstat", "--no-renames", last, ref, "--", *pathspec)
    files = insertions = deletions = 0
    for line in numstat.splitlines():
        match = _NUMSTAT_RE.match(line)
        if not match or _is_excluded(match.group(3)):
            continue
        files += 1
        if match.group(1) != "-":
            insertions += int(match.group(1))
            deletions += int(match.group(2))

    name_status = _git(repo, "diff", "--name-status", "--no-renames", last, ref, "--", *pathspec)
    added: list[str] = []
    removed: list[str] = []
    for line in name_status.splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        status, path = fields[0], fields[1]
        if _is_excluded(path) or not path.endswith(".py"):
            continue
        if status.startswith("A"):
            added.append(path)
        elif status.startswith("D"):
            removed.append(path)
    return files, insertions, deletions, sorted(added), sorted(removed)


def _commit_churn(repo: Path, last: str, ref: str, scope: str) -> tuple[int, list[CommitChurn]]:
    """Per-commit churn in ``last..ref`` under ``scope``.

    Returns the number of commits with at least one non-excluded change
    and the top ``TOP_COMMITS`` commits by lines changed (ties broken by
    sha so the ranking is total and deterministic).
    """
    pathspec = [f"{scope}/"] if scope else []
    out = _git(
        repo,
        "log",
        "--no-renames",
        "--format=%H%x09%s",
        "--numstat",
        f"{last}..{ref}",
        "--",
        *pathspec,
    )
    commits: list[CommitChurn] = []
    sha = subject = ""
    lines = 0
    touched = False

    def _close() -> None:
        if sha and touched:
            commits.append(CommitChurn(sha=sha, subject=subject, lines=lines))

    for raw in out.splitlines():
        if _COMMIT_HEADER_RE.match(raw):
            _close()
            parts = raw.split("\t", 1)
            sha = parts[0]
            subject = parts[1] if len(parts) > 1 else ""
            lines = 0
            touched = False
            continue
        match = _NUMSTAT_RE.match(raw)
        if not match or _is_excluded(match.group(3)):
            continue
        touched = True
        if match.group(1) != "-":
            lines += int(match.group(1)) + int(match.group(2))
    _close()

    top = sorted(
        (c for c in commits if c.lines > 0),
        key=lambda c: (-c.lines, c.sha),
    )[:TOP_COMMITS]
    return len(commits), top


def compute_report(repo: Path, ref: str, baseline: str | None = None) -> StalenessReport:
    """Compute the staleness report at ``ref``.

    With ``baseline``, each flagged entry is additionally marked
    ``newly_flagged`` when the same context file was not flagged at the
    baseline commit — the signal the PR surface keys on.
    """
    ref_sha = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    baseline_sha: str | None = None
    baseline_flagged: set[str] = set()
    if baseline is not None:
        baseline_sha = _git(repo, "rev-parse", "--verify", f"{baseline}^{{commit}}").strip()
        baseline_flagged = {entry.context.path for entry in _compute_entries(repo, baseline_sha) if entry.flagged}

    report = StalenessReport(ref=ref_sha, baseline=baseline_sha)
    report.entries = _compute_entries(repo, ref_sha)
    if baseline_sha is not None:
        for entry in report.entries:
            entry.newly_flagged = entry.flagged and entry.context.path not in baseline_flagged
    return report


def _compute_entries(repo: Path, ref_sha: str) -> list[StalenessEntry]:
    entries: list[StalenessEntry] = []
    for context in list_context_files(repo, ref_sha):
        last = _last_touch(repo, ref_sha, context.path)
        if last is None:  # pragma: no cover - tracked files always have a commit
            continue
        last_sha, last_date, last_subject = last
        entry = StalenessEntry(
            context=context,
            last_commit=last_sha,
            last_commit_date=last_date,
            last_commit_subject=last_subject,
        )
        (
            entry.files_changed,
            entry.insertions,
            entry.deletions,
            entry.modules_added,
            entry.modules_removed,
        ) = _net_diff(repo, last_sha, ref_sha, context.scope)
        entry.commits_since, entry.top_commits = _commit_churn(repo, last_sha, ref_sha, context.scope)
        entries.append(entry)
    return entries


def _short(sha: str) -> str:
    return sha[:SHORT_SHA_LEN]


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|")


def _fmt_modules(paths: list[str], limit: int = 10) -> str:
    shown = ", ".join(f"`{p}`" for p in paths[:limit])
    extra = len(paths) - limit
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def render_markdown(report: StalenessReport) -> str:
    parts: list[str] = ["# Context-file staleness report\n\n"]
    parts.append(
        f"Checked {len(report.entries)} curated context file(s) at `{_short(report.ref)}`. "
        f"{len(report.flagged)} flagged.\n"
    )
    if report.baseline is not None:
        parts.append(f"Baseline `{_short(report.baseline)}`: {len(report.newly_flagged)} newly flagged since.\n")
    parts.append("\n")

    if report.is_clean:
        parts.append("All curated context files are fresh relative to the code they describe.\n")
        return "".join(parts)

    for entry in report.flagged:
        scope_label = f"`{entry.context.scope}/`" if entry.context.scope else "the whole repository"
        marker = " (newly flagged in this range)" if entry.newly_flagged else ""
        parts.append(f"## `{entry.context.path}`{marker}\n\n")
        parts.append(f"- Scope: {scope_label}\n")
        parts.append(
            f"- Last touched: `{_short(entry.last_commit)}` ({entry.last_commit_date}) — {entry.last_commit_subject}\n"
        )
        parts.append(
            f"- Since then: {entry.commits_since} commit(s) in scope; net diff "
            f"{entry.files_changed} file(s), +{entry.insertions}/-{entry.deletions} "
            f"({entry.total_lines} lines, threshold {entry.line_threshold})\n"
        )
        if entry.modules_added:
            parts.append(f"- Modules added: {_fmt_modules(entry.modules_added)}\n")
        if entry.modules_removed:
            parts.append(f"- Modules removed: {_fmt_modules(entry.modules_removed)}\n")
        if entry.top_commits:
            parts.append("- Top commits by churn:\n\n")
            parts.append("| Commit | Lines | Subject |\n|--------|-------|---------|\n")
            for commit in entry.top_commits:
                parts.append(f"| `{_short(commit.sha)}` | {commit.lines} | {_md_escape(commit.subject)} |\n")
        parts.append("\n")

    parts.append(
        "To clear a flag, review the context file against its scope and touch it in a "
        "commit — update it, or make a reconfirmation-only edit. Either way the "
        '"is this still true?" review leaves a commit that resets the clock.\n'
    )
    return "".join(parts)


def render_json(report: StalenessReport) -> str:
    payload = {
        "ref": report.ref,
        "baseline": report.baseline,
        "files_checked": len(report.entries),
        "clean": report.is_clean,
        "flagged": [
            {
                "path": entry.context.path,
                "scope": entry.context.scope,
                "repo_scoped": entry.context.repo_scoped,
                "last_commit": entry.last_commit,
                "last_commit_date": entry.last_commit_date,
                "commits_since": entry.commits_since,
                "files_changed": entry.files_changed,
                "insertions": entry.insertions,
                "deletions": entry.deletions,
                "total_lines": entry.total_lines,
                "line_threshold": entry.line_threshold,
                "modules_added": entry.modules_added,
                "modules_removed": entry.modules_removed,
                "top_commits": [
                    {"sha": commit.sha, "lines": commit.lines, "subject": commit.subject}
                    for commit in entry.top_commits
                ],
                "newly_flagged": entry.newly_flagged,
            }
            for entry in report.flagged
        ],
        "newly_flagged": [entry.context.path for entry in report.newly_flagged],
    }
    return json.dumps(payload, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic context-file staleness report from git history.")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help="Repository root (default: this repo).")
    parser.add_argument("--ref", default="HEAD", help="Commit to assess (default: HEAD).")
    parser.add_argument(
        "--baseline",
        default=None,
        help="Also compute flags at this commit and mark entries newly flagged since (PR surface).",
    )
    parser.add_argument("--strict", action="store_true", help="Exit 1 when any context file is flagged.")
    parser.add_argument("--json", action="store_true", help="Emit a JSON summary instead of the Markdown report.")
    args = parser.parse_args()

    repo = args.repo.resolve()
    try:
        if _git(repo, "rev-parse", "--is-shallow-repository").strip() == "true":
            print(
                "ERROR: shallow clone; history-derived churn would be silently wrong. "
                "Fetch full history (fetch-depth: 0) and re-run.",
                file=sys.stderr,
            )
            return 2
        report = compute_report(repo, args.ref, args.baseline)
    except GitError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(render_json(report) if args.json else render_markdown(report), end="")
    if args.json:
        print()

    if args.strict and not report.is_clean:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
