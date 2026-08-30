"""Pure-logic helpers for composing pull-request titles and bodies.

This module converts a completed Bernstein session into the title and
markdown body of a GitHub pull request.  It is deliberately free of
``click`` and ``subprocess`` imports so it can be unit-tested in
isolation; the CLI wrapper in :mod:`bernstein.cli.commands.pr_cmd`
handles I/O, git push and ``gh`` invocation.

The description is composed from the *change*, not from the run that made
it. A run's last commit is often housekeeping - a lint repair, a formatting
pass, a regenerated context file - so titling the pull request after the
newest subject names the wrong change; :func:`rank_commits` orders the
branch's commits by how much they alter ``src/`` and drops the structurally
housekeeping ones, and the surviving dominant subject titles the pull
request. The body follows the same rule: the linked issue's problem
statement, the files the diff touches, and the gates that actually ran.
:func:`build_provenance` binds the result to the diff hash and the run's
journal head, and :func:`attest_pr_description` anchors that binding through
the existing review-receipt machinery so a reader can check offline that the
description belongs to this diff.

The module reuses existing Bernstein state:

* :class:`bernstein.core.persistence.session.SessionState` - run-level
  goal and completed task ids.
* :class:`bernstein.core.persistence.session.WrapUpBrief` - per-session
  diff-stat and changes summary written on graceful stop.
* :class:`bernstein.core.tasks.models.JanitorResult` - quality-gate
  signal results used for the Verification section.
* ``.sdd/runs/<run_id>/`` - the run's own directory: the replay metadata
  that names the run, and the Merkle-chained journal whose merge rows the
  Changes section is projected from. Every run writes it, including the
  ones that end without a wrap-up file, so it is what stops a session from
  resolving to ``unknown``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from bernstein.core.review.receipt import ReviewReceipt


_WRAPUP_GLOB = "*-wrapup.json"

#: Files a run leaves in ``.sdd/runs/<run_id>/``: the replay metadata that
#: names the run, and the Merkle-chained journal of what it did.
_RUN_METADATA_FILENAME = "metadata.json"
_RUN_JOURNAL_FILENAME = "journal.jsonl"

#: Journal events the Changes section is projected from. Duplicated as plain
#: strings rather than imported so this module stays free of orchestration
#: imports at module scope; the names are asserted against their source in
#: ``tests/unit/test_pr_goal_and_run_identity.py``.
_EVENT_TASK_MERGED = "task_merged"
_EVENT_TASK_DIFF_CAPTURED = "task_diff_captured"


__all__ = [
    "ChangeProvenance",
    "CommitRecord",
    "EvidenceSummary",
    "FileChange",
    "GateResult",
    "MergedChange",
    "SessionSummary",
    "attest_pr_description",
    "build_pr_body",
    "build_pr_title",
    "build_provenance",
    "describe_commit",
    "dominant_commit",
    "is_housekeeping_commit",
    "load_session_summary",
    "parse_commit_log",
    "rank_commits",
]


# Hard cap on a PR title - GitHub renders long titles awkwardly and most
# style guides recommend keeping headlines short.
_TITLE_MAX_CHARS = 70

# Conventional-commit prefixes, in priority order.  When the goal already
# starts with one of these we reuse it; otherwise we classify heuristically.
_CC_PREFIXES = (
    "feat",
    "fix",
    "refactor",
    "docs",
    "test",
    "chore",
    "perf",
    "build",
    "ci",
    "style",
)

_FIX_KEYWORDS = ("fix", "bug", "broken", "regression", "crash", "error")
_DOCS_KEYWORDS = ("docs", "documentation", "readme", "changelog")
_TEST_KEYWORDS = ("test", "tests", "coverage", "pytest")
_REFACTOR_KEYWORDS = ("refactor", "cleanup", "rename", "reorganise", "reorganize")

# Issue labels that state the change type outright. A tracker label is the
# repository's own classification of the work, so it settles what the wording
# of a title can only hint at - an issue labelled ``bug`` must never open a
# PR titled ``feat:``.
_LABEL_TYPES: Mapping[str, str] = {
    "bug": "fix",
    "bugfix": "fix",
    "defect": "fix",
    "regression": "fix",
    "documentation": "docs",
    "docs": "docs",
    "performance": "perf",
    "perf": "perf",
    "refactor": "refactor",
    "refactoring": "refactor",
    "test": "test",
    "tests": "test",
    "testing": "test",
    "build": "build",
    "ci": "ci",
    "chore": "chore",
    "maintenance": "chore",
    "enhancement": "feat",
    "feature": "feat",
}

# Which mapped type wins when an issue carries several type-bearing labels.
# Fixed order rather than label order, so the same issue always produces the
# same title however the tracker happens to list its labels.
_LABEL_TYPE_PRECEDENCE = ("fix", "docs", "perf", "refactor", "test", "build", "ci", "chore", "feat")

# Where behaviour lives. Commits are ranked by how much they change under this
# prefix, so a large test or docs commit never outranks the feature it covers.
_SRC_PREFIX = "src/"

# Conventional-commit types that state outright that a commit is upkeep.
_HOUSEKEEPING_TYPES = frozenset({"style", "chore"})

_CC_SUBJECT_RE = re.compile(r"^(?P<type>[a-z]+)(?:\([^)]*\))?!?:\s", re.IGNORECASE)

# Markers for a commit that was never meant to describe anything.
_REBASE_MARKER_RE = re.compile(r"^\s*(?:fixup!|squash!|amend!)", re.IGNORECASE)
_WIP_MARKER_RE = re.compile(r"^\s*(?:\[wip\]|wip\b)", re.IGNORECASE)

# Subjects that provably say nothing about the change, so the renderer
# describes the commit by what it touched instead of quoting them.
#
# The standing case is the fold-in commit an agent worktree writes,
# ``[WIP] <session-id> partial work`` (agent_lifecycle.py). Its subject names
# a session, not a change, and a squash merge copies the pull request body
# onto the default branch — so the session identifier became the permanent
# description of 71 commits on ``main`` in two weeks.
#
# Deliberately narrow. It matches that shape and a bare marker with nothing
# after it, and nothing else: a WIP commit whose author wrote a real subject
# still renders verbatim, because the subject is better than anything derived
# from a diff. This is the rendering half only — ``is_housekeeping_commit``
# still judges WIP commits by their churn (#4726), and this never consults it.
_UNINFORMATIVE_SUBJECT_RE = re.compile(
    r"^\s*(?:\[wip\]|wip)\s*(?::|-)?\s*(?:\S+\s+)?partial work\s*$|^\s*(?:\[wip\]|wip)\s*$",
    re.IGNORECASE,
)

# The wording a repair commit uses whatever conventional-commit type it
# claims. ``fix: resolve lint gate failures`` is typed ``fix`` and is still
# upkeep, so the type check alone cannot catch it.
_HOUSEKEEPING_PHRASE_RE = re.compile(
    r"\b(?:"
    r"lint|linter|linting|ruff|black|isort|prettier|eslint|gofmt|rustfmt"
    r"|formatter|formatting|(?:re|auto-?)format(?:s|ted|ting)?"
    r"|pre-commit|whitespace|typos?|regenerat(?:e|ed|es|ing|ion)"
    r")\b",
    re.IGNORECASE,
)

# Agent-context files a run regenerates. A commit that touches only these
# synced nothing but its own instructions.
_GENERATED_CONTEXT_NAMES = frozenset(
    {
        "AGENTS.md",
        "CLAUDE.md",
        "CONVENTIONS.md",
        "GEMINI.md",
        ".clinerules",
        ".cursorrules",
        ".windsurfrules",
        "copilot-instructions.md",
    }
)
_GENERATED_CONTEXT_DIRS = (".cursor/", ".github/instructions/")

#: Separators the commit-log format uses. ASCII record/unit separators cannot
#: occur in a commit subject, so parsing never has to guess.
_COMMIT_RECORD_SEP = "\x1e"
_COMMIT_FIELD_SEP = "\x1f"

#: Newline split for commit-log parsing. ``str.splitlines`` also breaks on the
#: ASCII record separator the format uses, which would consume every marker.
_LINE_SPLIT_RE = re.compile(r"\r?\n")

#: The ``git log --format`` string :func:`parse_commit_log` expects. Exported
#: so the CLI asks for exactly the shape the parser reads.
COMMIT_LOG_FORMAT = f"format:{_COMMIT_RECORD_SEP}%H{_COMMIT_FIELD_SEP}%P{_COMMIT_FIELD_SEP}%s"

# How many files the Change section names before it stops listing them.
_MAX_FILES_LISTED = 20

# How much of the linked issue's body the Problem section quotes.
_PROBLEM_MAX_CHARS = 600

# SHA-256 of zero bytes. `compute_diff_hash(b"")` returns it, and it is a
# well-formed digest of nothing: printed under "verify this" it is a receipt
# no verifier can honour. Recognised here so no caller can publish one.
_EMPTY_DIFF_HASH_SUFFIX = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_DIFF_STAT_SUMMARY_RE = re.compile(
    r"(?P<files>\d+)\s+files?\s+changed"
    r"(?:,\s*(?P<added>\d+)\s+insertions?\(\+\))?"
    r"(?:,\s*(?P<removed>\d+)\s+deletions?\(-\))?"
)

#: Verdict recorded on a receipt that attests a pull-request description
#: rather than a code review, so the two are told apart on read-back.
DESCRIPTION_VERDICT = "description"


@dataclass(frozen=True)
class GateResult:
    """A single quality-gate outcome as surfaced in the PR body.

    Attributes:
        name: Human-readable gate name (e.g. ``"lint"``, ``"types"``,
            ``"tests"``).
        passed: ``True`` when the gate reported success.
        detail: Optional extra context shown in parentheses next to the
            gate name (e.g. ``"ruff: 0 findings"``).  May be empty.
    """

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class CostBreakdown:
    """Aggregate cost figures for a session.

    Attributes:
        total_usd: Cumulative spend in US dollars.
        total_tokens: Sum of input + output tokens across every call.
        by_role: Mapping of role (``manager``, ``engineer``, ...) to USD.
    """

    total_usd: float = 0.0
    total_tokens: int = 0
    by_role: Mapping[str, float] = field(default_factory=dict[str, float])


@dataclass(frozen=True)
class EvidenceSummary:
    """A sealed evidence bundle surfaced in the PR body (issue #2362).

    The block links the bundle so review happens against sealed proof rather
    than a rerun-and-hope. It carries only the pointer and counts, never the
    evidence bytes.

    Attributes:
        task_id: The task the bundle was sealed for.
        anchor: The bundle's ``sha256:`` spine anchor (``journal_entry_hash``).
        passed: Number of producers that passed.
        failed: Number of producers that failed (advisory failures included).
        gate_passed: Whether every required producer passed.
    """

    task_id: str
    anchor: str
    passed: int
    failed: int
    gate_passed: bool


@dataclass(frozen=True)
class MergedChange:
    """One task whose work the run merged, as the run journal recorded it.

    Attributes:
        task_id: The task that was merged.
        files: Files its captured diff touched (0 when none was captured).
        added: Lines added by that diff.
        removed: Lines removed by that diff.
    """

    task_id: str
    files: int = 0
    added: int = 0
    removed: int = 0


@dataclass(frozen=True)
class FileChange:
    """One file a commit touched, with the line churn git reported.

    Attributes:
        path: Repository-relative path.
        added: Lines added; ``0`` for a binary change, which git reports as
            ``-`` rather than a count.
        removed: Lines removed; ``0`` for a binary change.
    """

    path: str
    added: int = 0
    removed: int = 0

    @property
    def churn(self) -> int:
        """Lines the file gained plus lines it lost."""
        return self.added + self.removed


@dataclass(frozen=True)
class CommitRecord:
    """One commit on the branch a pull request is opened from.

    Attributes:
        sha: Full commit hash.
        subject: The commit's first line.
        is_merge: Whether the commit has more than one parent.
        files: The files the commit touched, with their churn. Merge commits
            and empty commits carry none.
    """

    sha: str
    subject: str
    is_merge: bool = False
    files: tuple[FileChange, ...] = ()

    @property
    def short_sha(self) -> str:
        """The abbreviated hash shown next to the subject in the body."""
        return self.sha[:7]

    @property
    def src_churn(self) -> int:
        """Lines this commit changed under :data:`_SRC_PREFIX`."""
        return sum(f.churn for f in self.files if f.path.startswith(_SRC_PREFIX))

    @property
    def total_churn(self) -> int:
        """Lines this commit changed anywhere in the tree."""
        return sum(f.churn for f in self.files)


@dataclass(frozen=True)
class ChangeProvenance:
    """The binding between a pull-request description and what it describes.

    A description is prose, and prose can be written about any diff. These two
    values are what make it checkable: the diff the description was composed
    from, and the run journal head identifying every step that produced it.
    Both are recomputable, so a reader can tell a description that belongs to
    this diff from one that does not.

    Attributes:
        diff_hash: ``sha256:`` content hash of the diff bytes, computed by
            :func:`bernstein.core.review.receipt.compute_diff_hash` - the same
            function ``review-receipt verify`` recomputes with.
        journal_head: The run journal's Merkle head, or ``""`` when the run
            left no journal.
    """

    diff_hash: str
    journal_head: str = ""


@dataclass(frozen=True)
class SessionSummary:
    """Everything the PR generator needs from one completed session.

    Attributes:
        session_id: Stable identifier for the session (short form, first
            12 characters of the underlying id, is shown in the PR
            trailer).
        goal: The inline goal or first-task title that drove the run.
        primary_role: Role that performed the bulk of the work, used to
            seed the conventional-commit type when the goal does not
            already supply one.  May be ``None``.
        branch: Git branch containing the session's commits.
        base_branch: Intended PR base (usually ``main``).
        diff_stat: Output of ``git diff --stat <base>..<branch>``.
        merged_changes: Tasks the run merged, read off the run journal.
            Shown next to the diff-stat so a reviewer sees which tasks
            produced the diff even when the branch has already been folded
            into the base and ``git diff`` reports nothing.
        gates: Quality-gate outcomes from the janitor.
        cost: Aggregate cost figures for the session.
        evidence: Sealed evidence bundle for the task, or ``None`` when the
            task declared no evidence producers.
        issue_problem: The linked issue's body. Its first paragraph is the
            Problem section, so the pull request states the problem the issue
            states rather than the instructions the run was handed.
        commits: The branch's commits, newest first. When present they are
            what the title and the Change section are derived from.
        journal_head: The run journal's Merkle head, carried into
            :class:`ChangeProvenance`.
        provenance: The description's binding to the diff it describes, or
            ``None`` when no diff was available to hash.
        git_error: Why git could not describe the branch, when it could
            not.  A failed query and an empty answer look identical once
            they reach this dataclass, so the reason travels with them and
            the description reports it instead of claiming no changes.
    """

    session_id: str
    goal: str
    branch: str
    base_branch: str = "main"
    changes_summary: str = ""
    primary_role: str | None = None
    diff_stat: str = ""
    merged_changes: tuple[MergedChange, ...] = ()
    gates: tuple[GateResult, ...] = ()
    cost: CostBreakdown = field(default_factory=CostBreakdown)
    evidence: EvidenceSummary | None = None
    issue_problem: str = ""
    commits: tuple[CommitRecord, ...] = ()
    journal_head: str = ""
    provenance: ChangeProvenance | None = None
    git_error: str = ""


# ---------------------------------------------------------------------------
# Commit ranking - which commit the pull request is about
# ---------------------------------------------------------------------------


def _is_generated_context_path(path: str) -> bool:
    """Whether ``path`` is an agent-context file a run regenerates."""
    name = path.rsplit("/", 1)[-1]
    return name in _GENERATED_CONTEXT_NAMES or path.startswith(_GENERATED_CONTEXT_DIRS)


def is_housekeeping_commit(commit: CommitRecord) -> bool:
    """Whether ``commit`` is structural upkeep rather than the change itself.

    Housekeeping is decided from the commit's shape and its subject, never
    from its position in the branch - the failure this guards against is a run
    that *ends* with upkeep, and "last commit wins" is exactly what named the
    whole pull request after a lint repair.

    A commit is housekeeping when any of the following holds:

    * it is a merge commit, or it touches no files at all;
    * its subject is a rebase marker (``fixup!``, ``squash!``, ``amend!``);
    * its subject carries a work-in-progress marker (``[WIP]``, ``wip``) *and*
      it changes nothing under ``src/`` - the marker records when the commit
      was made, not that it is upkeep, and a worktree fold routinely lands
      real work behind it;
    * its conventional-commit type is ``style`` or ``chore``;
    * its subject names a formatter, a linter or a regeneration - the wording
      a repair commit uses whatever type it claims, which is how ``fix:
      resolve lint gate failures`` slipped through a type-only check;
    * every file it touches is a generated agent-context file.

    The wording rule can misread a genuine change to the lint setup itself.
    That misread is benign: a run whose *only* substantive commit is
    classified away falls back to the linked issue's title, which for such a
    run says the same thing.

    Args:
        commit: The commit to classify.

    Returns:
        ``True`` when the commit must not name the pull request.
    """
    if commit.is_merge or not commit.files:
        return True

    subject = commit.subject.strip()
    if not subject or _REBASE_MARKER_RE.match(subject):
        return True

    # A WIP marker records when the commit was made, not that it is upkeep.
    # Folding an agent worktree in lands substantive work behind that prefix
    # as a matter of course, so classifying on the marker alone drops the one
    # commit that touched src/ out of the ranking entirely and hands the pull
    # request's name to whatever small follow-up came after it. Judge it by
    # what it changed: a WIP commit that alters no source is a checkpoint.
    if _WIP_MARKER_RE.match(subject) and commit.src_churn == 0:
        return True

    conventional = _CC_SUBJECT_RE.match(subject)
    if conventional is not None and conventional.group("type").lower() in _HOUSEKEEPING_TYPES:
        return True

    if _HOUSEKEEPING_PHRASE_RE.search(subject):
        return True

    return all(_is_generated_context_path(f.path) for f in commit.files)


def rank_commits(commits: Iterable[CommitRecord]) -> tuple[CommitRecord, ...]:
    """Return the substantive commits, the one that changed the most first.

    Ranking is by ``src/`` churn, because that is where behaviour lives; ties
    break on whole-tree churn and then on input order, so the same branch
    always produces the same ranking however git happened to list it.
    Housekeeping commits are dropped rather than ranked last: they are not
    candidates to name the pull request at all.

    Args:
        commits: The branch's commits, in any order.

    Returns:
        The substantive commits, most-changed first. Empty when every commit
        is housekeeping.
    """
    substantive = [(index, commit) for index, commit in enumerate(commits) if not is_housekeeping_commit(commit)]
    substantive.sort(key=lambda pair: (-pair[1].src_churn, -pair[1].total_churn, pair[0]))
    return tuple(commit for _, commit in substantive)


def dominant_commit(commits: Iterable[CommitRecord]) -> CommitRecord | None:
    """Return the commit the pull request is about, or ``None``.

    Args:
        commits: The branch's commits, in any order.

    Returns:
        The highest-ranked substantive commit, or ``None`` when the run left
        nothing but housekeeping and the caller must fall back to the issue.
    """
    ranked = rank_commits(commits)
    return ranked[0] if ranked else None


def parse_commit_log(raw: str) -> tuple[CommitRecord, ...]:
    """Parse ``git log --format=<record> --numstat`` output into records.

    The expected format is the one :data:`COMMIT_LOG_FORMAT` asks for: each
    commit opens with an ASCII record separator followed by
    ``<sha>\x1f<parents>\x1f<subject>``, and its ``--numstat`` rows follow
    until the next separator. Anything that does not parse is skipped rather
    than raising, so a malformed row costs one commit and not the whole
    description.

    Args:
        raw: Raw stdout from the git invocation.

    Returns:
        One :class:`CommitRecord` per commit, in git's output order.
    """
    records: list[CommitRecord] = []
    sha = ""
    subject = ""
    is_merge = False
    files: list[FileChange] = []
    started = False

    def flush() -> None:
        if started and sha:
            records.append(CommitRecord(sha=sha, subject=subject, is_merge=is_merge, files=tuple(files)))

    # ``str.splitlines`` treats the ASCII record separator as a line boundary
    # and would eat the very marker the format uses, so split on newlines only.
    for line in _LINE_SPLIT_RE.split(raw):
        if line.startswith(_COMMIT_RECORD_SEP):
            flush()
            fields = line[len(_COMMIT_RECORD_SEP) :].split(_COMMIT_FIELD_SEP)
            sha = fields[0].strip() if fields else ""
            parents = fields[1].split() if len(fields) > 1 else []
            subject = fields[2].strip() if len(fields) > 2 else ""
            is_merge = len(parents) > 1
            files = []
            started = True
            continue
        if not started or not line.strip():
            continue
        parsed = _parse_numstat_row(line)
        if parsed is not None:
            files.append(parsed)
    flush()
    return tuple(records)


def _parse_numstat_row(line: str) -> FileChange | None:
    """Parse one ``<added>\\t<removed>\\t<path>`` row, or return ``None``."""
    parts = line.split("\t")
    if len(parts) < 3:
        return None
    added, removed, path = parts[0].strip(), parts[1].strip(), parts[2].strip()
    if not path:
        return None
    return FileChange(path=_normalise_rename(path), added=_numstat_count(added), removed=_numstat_count(removed))


def _numstat_count(value: str) -> int:
    """Return a numstat count, mapping git's ``-`` (binary) to ``0``."""
    try:
        return max(int(value), 0)
    except ValueError:
        return 0


def _normalise_rename(path: str) -> str:
    """Return the destination path of a git rename notation.

    ``git`` renders a rename as ``old => new`` or ``dir/{old => new}/file``;
    the destination is the path the pull request actually changed.
    """
    if "=>" not in path:
        return path
    if "{" in path and "}" in path:
        prefix, rest = path.split("{", 1)
        inner, suffix = rest.split("}", 1)
        return f"{prefix}{inner.split('=>', 1)[-1].strip()}{suffix}"
    return path.split("=>", 1)[-1].strip()


# ---------------------------------------------------------------------------
# Title generation
# ---------------------------------------------------------------------------


def _type_from_labels(labels: Iterable[str]) -> str | None:
    """Return the change type the issue's labels state, or ``None``.

    Args:
        labels: Tracker labels on the linked issue, in any order.

    Returns:
        A :data:`_CC_PREFIXES` member when a label maps to one, else
        ``None`` so the caller falls back to its own heuristics.
    """
    mapped = {_LABEL_TYPES[label.strip().lower()] for label in labels if label.strip().lower() in _LABEL_TYPES}
    for candidate in _LABEL_TYPE_PRECEDENCE:
        if candidate in mapped:
            return candidate
    return None


def _classify(goal: str, role: str | None, labels: Iterable[str] = ()) -> str:
    """Pick a conventional-commit type from the goal, labels and role.

    The order is strongest evidence first: a conventional-commit prefix the
    author typed, then the linked issue's labels, then keywords guessed out
    of the wording, then the role that did the work.

    Args:
        goal: Task goal / session description.
        role: Primary role, if known.
        labels: Labels on the linked issue, if one was named.

    Returns:
        One of :data:`_CC_PREFIXES`; defaults to ``"feat"``.
    """
    lowered = goal.lower()

    for prefix in _CC_PREFIXES:
        if lowered.startswith((f"{prefix}:", f"{prefix}(")):
            return prefix

    from_labels = _type_from_labels(labels)
    if from_labels is not None:
        return from_labels

    if any(kw in lowered for kw in _FIX_KEYWORDS):
        return "fix"
    if any(kw in lowered for kw in _DOCS_KEYWORDS):
        return "docs"
    if any(kw in lowered for kw in _TEST_KEYWORDS):
        return "test"
    if any(kw in lowered for kw in _REFACTOR_KEYWORDS):
        return "refactor"

    # Fall back on the role when the goal offers no signal.
    if role == "docs":
        return "docs"
    if role == "qa":
        return "test"

    return "feat"


def _shape_outcome(goal: str) -> str:
    """Normalise the goal into a short, imperative-mood phrase.

    Strips trailing punctuation, collapses internal whitespace and
    lower-cases the first character so it composes cleanly after a
    conventional-commit prefix.

    Args:
        goal: Raw goal string.

    Returns:
        A cleaned, verb-first summary.
    """
    cleaned = re.sub(r"\s+", " ", goal.strip())
    cleaned = cleaned.rstrip(".!?")

    # Drop any existing "feat: " / "fix(scope): " prefix so we don't
    # double-stamp the conventional-commit tag.
    cleaned = re.sub(r"^[a-z]+(?:\([^)]+\))?:\s*", "", cleaned, flags=re.IGNORECASE)

    if not cleaned:
        return "update project"

    # Preserve leading acronyms (MCP, CLI, HMAC, ...) so they don't become
    # mCP / cLI / hMAC; only lower-case a normal sentence-starting capital.
    if len(cleaned) >= 2 and cleaned[0].isupper() and cleaned[1].isupper():
        return cleaned

    return cleaned[0].lower() + cleaned[1:]


def _outcome_from_changes_summary(changes_summary: str) -> str | None:
    """Extract a short outcome from the first line of a changes summary.

    The changes summary is a newline-separated list of ``- <task title>:
    <result summary>`` lines.  The task title (the part before ``": "``) is
    the cleanest description of what landed; when no separator is present
    the whole line is used.

    Args:
        changes_summary: Multi-line string of formatted change bullets.

    Returns:
        The extracted outcome, or ``None`` when the summary is empty.
    """
    for line in changes_summary.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:]
        title = line.split(": ", 1)[0]
        return title.strip()
    return None


def build_pr_title(
    task_goal: str,
    role: str | None,
    labels: Iterable[str] = (),
    *,
    changes_summary: str = "",
    commits: Iterable[CommitRecord] = (),
) -> str:
    """Compose a conventional-commit pull-request title.

    The result is truncated to :data:`_TITLE_MAX_CHARS` characters with a
    trailing ellipsis when the cleaned outcome is longer.  The shape is
    always ``"<type>: <outcome>"``.

    The outcome names the dominant change. When ``commits`` are supplied they
    settle it: :func:`dominant_commit` picks the commit that altered ``src/``
    most among the non-housekeeping ones, and its subject is the outcome. A
    run that left nothing but housekeeping has no substantive subject to
    offer, so the title falls back to ``task_goal`` - the linked issue's
    title, as the CLI passes it. Only when no commits are known at all does
    the wrap-up's ``changes_summary`` get a say.

    The conventional-commit type comes from the same string the outcome does,
    then labels, then role - so a ``bug``-labelled issue never opens a
    ``feat:`` PR on wording alone.

    Args:
        task_goal: Session goal or, when one is linked, the issue title.
        role: Primary role for the session, used as a classification
            hint when nothing stronger is available.
        labels: Labels on the linked issue. They outrank both the wording
            and the role, so a PR never announces a change type the issue
            it closes contradicts.
        changes_summary: Newline-separated change bullets from the wrap-up.
            Consulted only when ``commits`` is empty.
        commits: The branch's commits. When non-empty they decide the
            outcome.

    Returns:
        A title at most :data:`_TITLE_MAX_CHARS` characters long.
    """
    known = tuple(commits)
    dominant = dominant_commit(known) if known else None

    if dominant is not None:
        outcome_source = dominant.subject
    elif known:
        # Every commit was housekeeping: none of them may name the PR.
        outcome_source = task_goal
    else:
        outcome_source = _outcome_from_changes_summary(changes_summary) or task_goal

    prefix = _classify(outcome_source if dominant is not None else task_goal, role, labels)
    outcome = _shape_outcome(outcome_source)

    full = f"{prefix}: {outcome}"
    if len(full) <= _TITLE_MAX_CHARS:
        return full

    # Leave room for the ellipsis so the hard cap is honoured.
    budget = _TITLE_MAX_CHARS - len(prefix) - len(": ") - 1
    return f"{prefix}: {outcome[:budget].rstrip()}…"


# ---------------------------------------------------------------------------
# Body generation
# ---------------------------------------------------------------------------


def _problem_line(goal: str) -> str:
    """Reduce a goal to a single one-line problem statement.

    A run's goal is the brief it was handed, so everything after the first
    blank line is standing instructions ("Work only inside this repository")
    rather than the problem; it is dropped before the first sentence is taken.
    For a goal like ``Resolve GitHub issue #N: <issue title>`` the title
    portion after the colon is returned.

    Args:
        goal: The raw goal string.

    Returns:
        A single-line problem statement.
    """
    stripped = goal.strip()
    if not stripped:
        return "No linked issue and no recorded goal; the change is described below."
    lead = re.split(r"\n\s*\n", stripped, maxsplit=1)[0].strip()
    first = re.split(r"[.;]\s+", lead, maxsplit=1)[0].strip()
    # A ``Resolve GitHub issue #N: <title>`` goal: the title is the problem.
    if ": " in first:
        first = first.split(": ", 1)[1].strip()
    return first or lead


def _problem_statement(session: SessionSummary) -> str:
    """Return the Problem section's text.

    The linked issue states the problem; the run's goal only restates it,
    wrapped in the instructions the run was given. So the issue body's first
    paragraph wins whenever there is one, and the goal is the fallback for an
    unlinked run.

    Args:
        session: The session being described.

    Returns:
        The problem statement, never empty.
    """
    paragraph = _first_paragraph(session.issue_problem)
    return paragraph or _problem_line(session.goal)


def _first_paragraph(text: str) -> str:
    """Return the first prose paragraph of ``text``, capped in length.

    Leading markdown headings ("## Problem") and blockquote markers are
    skipped so an issue that opens with a heading still yields prose.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    blocks = re.split(r"\n\s*\n", stripped)
    for index, block in enumerate(blocks):
        paragraph = _flatten_block(block)
        if not paragraph:
            continue
        # "Two release tracks, going forward:" is not a problem statement, it
        # is the sentence before one. A paragraph that ends on a colon
        # introduces the block beneath it, so take that block too rather than
        # publishing the lead-in alone.
        if paragraph.endswith(":") and index + 1 < len(blocks):
            continuation = _flatten_block(blocks[index + 1])
            if continuation:
                paragraph = f"{paragraph} {continuation}".strip()
        return _cap(paragraph)
    return ""


def _flatten_block(block: str) -> str:
    """Flatten one markdown block to a single line of prose.

    Headings are dropped; list markers and blockquote markers are stripped so
    a bulleted block reads as a sentence rather than as broken markdown.
    """
    kept: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^>\s*", "", line)
        line = re.sub(r"^(?:[-*+]|\d+\.)\s+", "", line)
        if line:
            kept.append(line)
    return " ".join(kept).strip()


def _cap(text: str) -> str:
    """Truncate ``text`` to the problem-statement budget, with an ellipsis."""
    if len(text) > _PROBLEM_MAX_CHARS:
        return text[:_PROBLEM_MAX_CHARS].rstrip() + "…"
    return text


def _format_gates(gates: tuple[GateResult, ...]) -> str:
    """Render gate outcomes as a checklist with ✅/❌ markers."""
    if not gates:
        return "- _No quality gates were configured for this session._"
    lines: list[str] = []
    for gate in gates:
        mark = "✅" if gate.passed else "❌"
        detail = f" - {gate.detail}" if gate.detail else ""
        lines.append(f"- {mark} **{gate.name}**{detail}")
    return "\n".join(lines)


def _format_diff_stat(diff_stat: str) -> str:
    """Render the diff-stat, folded away, or a fallback line."""
    stripped = diff_stat.strip()
    if not stripped:
        return "_No changes recorded for this session._"
    return "\n".join(
        [
            "<details>",
            "<summary>Full diff-stat</summary>",
            "",
            "```",
            stripped,
            "```",
            "",
            "</details>",
        ]
    )


def _format_merged_changes(merged: tuple[MergedChange, ...]) -> str:
    """Render the tasks the run merged, one line each."""
    lines = ["Merged in this run:"]
    for change in merged:
        if change.files:
            plural = "" if change.files == 1 else "s"
            counts = f"{change.files} file{plural}, +{change.added}/-{change.removed}"
        else:
            counts = "no diff captured"
        lines.append(f"- `{change.task_id}` - {counts}")
    return "\n".join(lines)


def _format_file_lines(files: Iterable[FileChange]) -> list[str]:
    """Render the files a commit touched, largest change first."""
    ordered = sorted(files, key=lambda f: (-f.churn, f.path))
    if not ordered:
        return []
    shown = ordered[:_MAX_FILES_LISTED]
    lines = ["| File | Change |", "| --- | --- |"]
    lines += [f"| `{change.path}` | +{change.added} / -{change.removed} |" for change in shown]
    remaining = len(ordered) - len(shown)
    if remaining > 0:
        lines.append(f"| _…and {remaining} more file(s)_ | |")
    return lines


def _scope_of(paths: Sequence[str]) -> str:
    """The deepest directory every one of ``paths`` sits under.

    ``""`` when they share nothing but the repository root, which reads
    better as "across the tree" than as an empty backtick pair.
    """
    if not paths:
        return ""
    split = [p.split("/")[:-1] for p in paths]
    common: list[str] = []
    # strict=False is the point: paths sit at different depths, and the common
    # scope ends at the shallowest one.
    for parts in zip(*split, strict=False):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return "/".join(common)


def describe_commit(commit: CommitRecord) -> str:
    """How a commit is named in the rendered body.

    Its subject, unless the subject provably says nothing about the change —
    then what it actually touched: scope plus churn. A reader of ``git log``
    on the default branch gets "the agents package, 3 files, +120 / -8"
    instead of a session identifier and the word "partial".

    Ranking is untouched: which commit names the pull request is
    ``rank_commits``' decision and stays exactly as #4726 left it. This only
    changes how the chosen commits are written down.
    """
    subject = commit.subject.strip()
    if not _UNINFORMATIVE_SUBJECT_RE.match(subject):
        return subject
    if not commit.files:
        # Nothing to describe it by. Say so rather than fall back to the
        # subject, which is what leaked the session identifier.
        return "checkpoint, no file changes"
    added = sum(f.added for f in commit.files)
    removed = sum(f.removed for f in commit.files)
    if len(commit.files) == 1:
        only = commit.files[0]
        return f"work in `{only.path}` (+{only.added} / -{only.removed})"
    scope = _scope_of([f.path for f in commit.files])
    where = f"in `{scope}`" if scope else "across the tree"
    return f"work {where} ({len(commit.files)} files, +{added} / -{removed})"


def _format_commit_changes(commits: tuple[CommitRecord, ...]) -> str:
    """Render the Change section from the branch's commits.

    The dominant commit leads with the files it altered, so a reader sees the
    change the pull request is about before anything else. Remaining
    substantive commits follow as one line each, and housekeeping commits are
    listed last and labelled, so they are visible without being mistaken for
    the point of the branch.

    Args:
        commits: The branch's commits, in git's order.

    Returns:
        The rendered section, or ``""`` when there are no commits to render.
    """
    if not commits:
        return ""

    ranked = rank_commits(commits)
    housekeeping = [commit for commit in commits if is_housekeeping_commit(commit) and not commit.is_merge]

    lines: list[str] = []
    if ranked:
        lead, *rest = ranked
        lines.append(f"{describe_commit(lead)} (`{lead.short_sha}`)")
        lines.append("")
        lines.extend(_format_file_lines(lead.files))
        if rest:
            lines.append("")
            lines.append("Also in this branch:")
            lines.extend(f"- {describe_commit(commit)} (`{commit.short_sha}`)" for commit in rest)

    if housekeeping:
        if lines:
            lines.append("")
        lines.append("Housekeeping, not what this pull request is about:")
        lines.extend(f"- {describe_commit(commit)} (`{commit.short_sha}`)" for commit in housekeeping)

    return "\n".join(lines)


def _format_changes(session: SessionSummary) -> str:
    """Render the Changes section from the commits, diff-stat and merged tasks.

    The commits are the primary source: they say what the diff does and which
    files it alters. The diff-stat and the run journal's merge rows follow as
    corroboration - a run whose branch has already been folded into the base
    leaves ``git diff`` with nothing to report, which is how a PR full of
    merged work came to say no changes were recorded. The fallback line is
    reached only when none of the three has anything.
    """
    blocks: list[str] = []
    commit_block = _format_commit_changes(session.commits)
    if commit_block:
        blocks.append(commit_block)
    if session.diff_stat.strip():
        blocks.append(_format_diff_stat(session.diff_stat))
    if session.merged_changes:
        blocks.append(_format_merged_changes(session.merged_changes))
    if not blocks:
        if session.git_error:
            return (
                "> ⚠️ **The diff could not be read.** This description was composed without it.\n"
                f"> git said: `{session.git_error}`"
            )
        return _format_diff_stat("")
    return "\n\n".join(blocks)


def _format_provenance(provenance: ChangeProvenance) -> str:
    """Render the Provenance block binding the description to its diff."""
    lines = [f"- **Diff:** `{provenance.diff_hash}`"]
    # A run that left no journal head has nothing to say here. The word
    # "unrecorded" beside a verify command reads as a missing recording
    # rather than as a run that never anchored one, and it is the line
    # readers ask about first.
    if provenance.journal_head:
        lines.append(f"- **Journal head:** `{provenance.journal_head}`")
    lines.append("- **Verify:** `bernstein review-receipt verify --pr <this PR> --issue <issue.md> --diff <pr.diff>`")
    return "\n".join(lines)


def _format_evidence(evidence: EvidenceSummary) -> str:
    """Render the sealed-evidence block linking the bundle (issue #2362).

    The block surfaces the gate verdict, the pass/fail counts, the spine
    anchor prefix, and the offline ``bernstein evidence show`` command, so a
    reviewer verifies against sealed proof rather than rerunning the checks.
    """
    verdict = "✅ pass" if evidence.gate_passed else "❌ fail"
    anchor = evidence.anchor.split(":", 1)[-1][:16] if evidence.anchor else "unanchored"
    return "\n".join(
        [
            f"- **Gate:** {verdict}",
            f"- **Producers:** {evidence.passed} passed / {evidence.failed} failed",
            f"- **Bundle anchor:** `{anchor}`",
            f"- **Inspect:** `bernstein evidence show {evidence.task_id}`",
            f"- **Verify offline:** `bernstein evidence verify {evidence.task_id}`",
        ]
    )


def _churn(session: SessionSummary) -> tuple[int, int, int] | None:
    """Return ``(files, added, removed)`` for the branch, or ``None``.

    The commits are the first source because they carry per-file numbers.
    The diff-stat's summary line is the fallback for a session whose commits
    were not parsed.  ``None`` means neither could say, which is the case a
    headline must not invent a number for.
    """
    paths: dict[str, tuple[int, int]] = {}
    for commit in session.commits:
        if commit.is_merge:
            continue
        for change in commit.files:
            added, removed = paths.get(change.path, (0, 0))
            paths[change.path] = (added + change.added, removed + change.removed)
    if paths:
        return len(paths), sum(a for a, _ in paths.values()), sum(r for _, r in paths.values())

    match = _DIFF_STAT_SUMMARY_RE.search(session.diff_stat)
    if match:
        return (
            int(match.group("files")),
            int(match.group("added") or 0),
            int(match.group("removed") or 0),
        )
    return None


def _format_headline(session: SessionSummary) -> str:
    """Render the one-line summary that opens the body.

    A reviewer opening a pull request asks three questions before any other:
    how big is it and did the checks pass.  The line answers
    all three above the fold so the rest of the description is optional
    reading rather than a search.
    """
    segments: list[str] = []

    churn = _churn(session)
    if churn is not None:
        files, added, removed = churn
        plural = "" if files == 1 else "s"
        segments.append(f"**{files} file{plural}** · +{added} / -{removed}")

    failed = [gate for gate in session.gates if not gate.passed]
    if session.gates:
        passed = len(session.gates) - len(failed)
        segments.append(f"**{passed}/{len(session.gates)} gates passed**")

    if session.git_error:
        marker = "⚠️"
        segments.append("**the diff could not be read**")
    elif failed:
        marker = "❌"
    elif session.gates:
        marker = "✅"
    else:
        marker = "📝"

    if not segments:
        return ""
    return f"> {marker} " + " · ".join(segments)


def build_pr_body(session: SessionSummary) -> str:
    """Render the full markdown body for a pull request.

    The output is structured so downstream reviewers (and tooling) can
    reliably grep for section headers.  All core sections - Problem,
    Change and Verification - are always present even when the
    underlying data is empty, so tests can rely on their presence.

    What the run spent is deliberately absent, for the same reason the
    status text is: it describes the run, and the page is public.

    Every section is projected from the change: the linked issue states the
    problem, the commits and their files state what the diff does, and the
    gates state what was actually run. The session's own status text - the
    wrap-up's task-completion lines - is deliberately not a source: it
    describes the run, and a reader of the pull request is asking about the
    diff.

    Args:
        session: The fully-populated session summary.

    Returns:
        A markdown string ready to pass to ``gh pr create --body``.
    """
    # The ``bernstein-session-id`` trailer is consumed by the autofix
    # daemon to claim ownership of PRs Bernstein opened - keeping it
    # on its own line lets ``gh pr view --json body`` callers parse it
    # with a single regex.
    short_id = session.session_id[:12] if session.session_id else "unknown"

    parts: list[str] = []
    headline = _format_headline(session)
    if headline:
        parts += [headline, ""]

    parts += [
        "## Problem",
        _problem_statement(session),
        "",
        "## Change",
        _format_changes(session),
        "",
        "## Verification",
        _format_gates(session.gates),
        "",
    ]
    # The evidence block links the sealed bundle so review happens against
    # sealed proof (issue #2362, AC3). Omitted entirely when the task declared
    # no evidence producers, so existing PRs are unchanged.
    if session.evidence is not None:
        parts += [
            "## Evidence",
            _format_evidence(session.evidence),
            "",
        ]
    # The description is prose about a diff, and prose can be written about
    # any diff. The block below is what makes this one checkable: the diff it
    # was composed from and the journal head of the run that produced it, both
    # recomputable by ``review-receipt verify``.
    if session.provenance is not None and not session.provenance.diff_hash.endswith(_EMPTY_DIFF_HASH_SUFFIX):
        parts += [
            "## Provenance",
            _format_provenance(session.provenance),
            "",
        ]
    parts += [
        "---",
        f"_Generated from Bernstein session `{short_id}`._",
        "",
        f"bernstein-session-id: {short_id}",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Provenance - binding the description to the diff it describes
# ---------------------------------------------------------------------------


def build_provenance(*, diff: bytes, journal_head: str = "") -> ChangeProvenance:
    """Bind a description to its diff and to the run that produced it.

    The diff hash comes from
    :func:`bernstein.core.review.receipt.compute_diff_hash` - the same
    function ``review-receipt verify`` recomputes with, so there is one
    hashing path and a description cannot be bound by a rule the verifier
    does not apply.

    Args:
        diff: The pull request's diff bytes.
        journal_head: The run journal's Merkle head, when the run left one.

    Returns:
        The :class:`ChangeProvenance` the body renders.
    """
    from bernstein.core.review.receipt import compute_diff_hash

    return ChangeProvenance(diff_hash=compute_diff_hash(diff), journal_head=journal_head)


def attest_pr_description(
    *,
    workdir: Path,
    pr_url: str,
    repo: str,
    issue_body: str,
    description: str,
    diff: bytes,
    journal_head: str = "",
    task_id: str = "",
    timestamp: int | None = None,
    hmac_key: bytes | None = None,
) -> ReviewReceipt:
    """Anchor a pull-request description against the diff it describes.

    The description takes the receipt's ``plan`` slot: the receipt then binds
    ``{issue, description, journal head, diff}`` in one signed, spine-anchored
    record, which is exactly the question a reader of the description has -
    does this text belong to this diff. ``bernstein review-receipt verify``
    answers it offline and rejects a diff that has since changed.

    Args:
        workdir: Project root; the receipt lands under ``.sdd/reviews/``.
        pr_url: The pull request the description was posted to.
        repo: ``owner/repo`` slug.
        issue_body: The linked issue's body, or ``""`` when unlinked.
        description: The posted description (title and body).
        diff: The diff the description describes.
        journal_head: The run journal's Merkle head.
        task_id: Task the run is attributed to.
        timestamp: Receipt timestamp; defaults to now. Passing one makes the
            receipt byte-identical across runs of the same inputs.
        hmac_key: Audit-chain key; defaults to the install's own.

    Returns:
        The signed, anchored receipt.
    """
    from bernstein.core.review.receipt import emit_review_receipt, load_or_create_review_identity
    from bernstein.core.security.audit import load_or_create_audit_key

    private_pem, public_pem = load_or_create_review_identity(workdir / ".sdd" / "identity")
    return emit_review_receipt(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=hmac_key if hmac_key is not None else load_or_create_audit_key(),
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        pr_url=pr_url,
        repo=repo,
        issue_body=issue_body,
        plan=description,
        journal_head=journal_head,
        diff=diff,
        findings=(),
        verdict=DESCRIPTION_VERDICT,
        task_id=task_id,
        timestamp=timestamp if timestamp is not None else int(time.time()),
        resolution_hash="sha256:912abcebddc909bb61712cad73e12236d0128a53e9e7fcac0ac33c58df0ea804",
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _sessions_dir(workdir: Path) -> Path:
    """Return the directory holding per-session artefacts."""
    return workdir / ".sdd" / "sessions"


def _pick_latest_wrapup(sessions_dir: Path) -> Path | None:
    """Return the newest ``*-wrapup.json`` file, or ``None`` if absent."""
    if not sessions_dir.exists():
        return None
    candidates = sorted(
        sessions_dir.glob(_WRAPUP_GLOB),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _read_json(path: Path) -> dict[str, object]:
    """Read a JSON file, returning an empty dict on any error."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # Normalise to ``dict[str, object]`` - json.loads never produces
    # non-string keys at the top level, but pyright wants us to say so.
    return {str(key): value for key, value in raw.items()}  # type: ignore[reportUnknownVariableType]


def _load_by_session_id(sessions_dir: Path, session_id: str) -> Path | None:
    """Locate a wrap-up file whose name or content matches ``session_id``."""
    if not sessions_dir.exists():
        return None

    # Fast path: filename prefix match (e.g. ``<timestamp>-<id>-wrapup.json``
    # or ``<id>-wrapup.json``).
    for candidate in sessions_dir.glob(_WRAPUP_GLOB):
        if session_id in candidate.name:
            return candidate

    # Slow path: scan contents for the session id.
    for candidate in sessions_dir.glob(_WRAPUP_GLOB):
        payload = _read_json(candidate)
        if payload.get("session_id") == session_id:
            return candidate

    return None


def _gates_from_dict(raw: object) -> tuple[GateResult, ...]:
    """Parse a loosely-typed list of gate dicts into :class:`GateResult`."""
    if not isinstance(raw, list):
        return ()
    gates: list[GateResult] = []
    for item in raw:  # type: ignore[reportUnknownVariableType]
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "gate"))  # type: ignore[reportUnknownArgumentType]
        passed = bool(item.get("passed", False))  # type: ignore[reportUnknownArgumentType]
        detail = str(item.get("detail", ""))  # type: ignore[reportUnknownArgumentType]
        gates.append(GateResult(name=name, passed=passed, detail=detail))
    return tuple(gates)


def _cost_from_dict(raw: dict[str, object]) -> CostBreakdown:
    """Parse a cost dict into :class:`CostBreakdown`, tolerating partials."""
    by_role_raw = raw.get("by_role", {})
    by_role: dict[str, float] = {}
    if isinstance(by_role_raw, dict):
        for key, value in by_role_raw.items():  # type: ignore[reportUnknownVariableType]
            try:
                by_role[str(key)] = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue

    try:
        total_usd = float(raw.get("total_usd", 0.0))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        total_usd = 0.0
    try:
        total_tokens = int(cast(int, raw.get("total_tokens", 0)))
    except (TypeError, ValueError):
        total_tokens = 0

    return CostBreakdown(
        total_usd=total_usd,
        total_tokens=total_tokens,
        by_role=by_role,
    )


def _candidate_task_ids(wrapup: dict[str, object]) -> list[str]:
    """Resolve the completed-task ids a wrap-up records, in preference order.

    A singular ``task_id`` wins; otherwise the ``completed_task_ids`` list (the
    shape the wrap-up writer emits) is used. Absent both, no task is named and
    no evidence is surfaced.
    """
    explicit = wrapup.get("task_id")
    if isinstance(explicit, str) and explicit:
        return [explicit]
    raw = wrapup.get("completed_task_ids")
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str) and t]  # type: ignore[reportUnknownVariableType]
    return []


def _run_dir(root: Path, session_id: str | None) -> Path | None:
    """Locate the run directory a PR should be described from.

    A run leaves its durable record in ``.sdd/runs/<run_id>/`` - the replay
    metadata that names it and the Merkle-chained journal of what it did.
    That directory outlives the runtime state and is written by every run,
    including the ones that never got as far as a wrap-up file, so it is what
    keeps a session from resolving to ``unknown``.

    Paths are derived through the journal module's containment barrier rather
    than by joining strings, so an operator-supplied ``--session-id`` cannot
    address a directory outside the runs root.

    Args:
        root: Project root.
        session_id: Run to look up, or ``None`` for the most recent one.

    Returns:
        The run directory, or ``None`` when there is none to read.
    """
    from bernstein.core.replay.journal import (
        JournalPathError,
        contained_run_journal,
        run_journal_path,
    )

    runs_root = root / ".sdd" / "runs"
    if not runs_root.is_dir():
        return None

    if session_id:
        try:
            journal = run_journal_path(root / ".sdd", session_id)
        except JournalPathError:
            return None
        return journal.parent if journal.parent.is_dir() else None

    newest: tuple[float, Path] | None = None
    for entry in runs_root.iterdir():
        if not entry.is_dir():
            continue
        found_journal = contained_run_journal(runs_root, entry.name)
        if found_journal is None:
            continue
        run_dir = found_journal.parent
        if not (found_journal.exists() or (run_dir / _RUN_METADATA_FILENAME).exists()):
            continue
        try:
            mtime = run_dir.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, run_dir)
    return newest[1] if newest else None


def _merged_changes_from_journal(run_dir: Path) -> tuple[MergedChange, ...]:
    """Project the run journal's merge rows into per-task change records.

    ``task_merged`` says which tasks landed; ``task_diff_captured`` carries
    the size of the diff each one produced. Reading them here means the
    Changes section describes what the run actually merged rather than
    whatever ``git diff`` happens to still show.

    Args:
        run_dir: The run's directory under ``.sdd/runs/``.

    Returns:
        One :class:`MergedChange` per merged task, in merge order. Empty when
        the journal is missing, unreadable, or records no merges.
    """
    journal = run_dir / _RUN_JOURNAL_FILENAME
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()

    merged_order: list[str] = []
    diffs: dict[str, tuple[int, int, int]] = {}
    for line in lines:
        if not line.strip():
            continue
        try:
            row: object = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        typed = {str(k): v for k, v in row.items()}  # type: ignore[reportUnknownVariableType]
        task_id = typed.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        event = typed.get("event")
        if event == _EVENT_TASK_MERGED and task_id not in merged_order:
            merged_order.append(task_id)
        elif event == _EVENT_TASK_DIFF_CAPTURED:
            diffs[task_id] = (
                _as_count(typed.get("diff_files")),
                _as_count(typed.get("diff_added")),
                _as_count(typed.get("diff_removed")),
            )

    changes: list[MergedChange] = []
    for task_id in merged_order:
        files, added, removed = diffs.get(task_id, (0, 0, 0))
        changes.append(MergedChange(task_id=task_id, files=files, added=added, removed=removed))
    return tuple(changes)


def _journal_head(run_dir: Path) -> str:
    """Return the run journal's Merkle head, or ``""`` when unreadable.

    The head is the ``event_hash`` of the journal's last row - the same value
    :meth:`bernstein.core.replay.journal.EventJournal.head` reports, read off
    disk here because the run has already finished by the time a PR is opened.

    Args:
        run_dir: The run's directory under ``.sdd/runs/``.

    Returns:
        The head hash, or ``""`` when the journal is missing or empty.
    """
    journal = run_dir / _RUN_JOURNAL_FILENAME
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row: object = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            head = {str(k): v for k, v in row.items()}.get("event_hash")  # type: ignore[reportUnknownVariableType]
            if isinstance(head, str) and head:
                return head
    return ""


def _as_count(value: object) -> int:
    """Coerce a journal count field to a non-negative int."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(int(value), 0)


def _evidence_summary_for_task(root: Path, task_ids: list[str]) -> EvidenceSummary | None:
    """Project the first sealed bundle among ``task_ids`` into an EvidenceSummary.

    Reads the sealed bundle off disk (the pointer and counts, never the evidence
    bytes) so a PR opened for a task that sealed proof-of-done links the bundle.
    Returns ``None`` when no candidate task has a bundle, so a session without
    evidence renders a body identical to before (issue #2362, AC3).
    """
    from bernstein.core.evidence.bundle import read_evidence_bundle

    for task_id in task_ids:
        try:
            bundle = read_evidence_bundle(root, task_id)
        except OSError:
            bundle = None
        if bundle is None:
            continue
        return EvidenceSummary(
            task_id=bundle.task_id,
            anchor=bundle.journal_entry_hash,
            passed=bundle.passed_count,
            failed=bundle.failed_count,
            gate_passed=bundle.gate_passed,
        )
    return None


def load_session_summary(
    session_id: str | None,
    *,
    workdir: Path | None = None,
    base_branch: str = "main",
) -> SessionSummary:
    """Load a :class:`SessionSummary` from on-disk session state.

    When ``session_id`` is ``None`` the newest wrap-up file wins.  When
    no wrap-up files exist, the session-level ``session.json`` is used as
    a best-effort fallback so the command still has something to say.

    Args:
        session_id: Specific session to load, or ``None`` for the most
            recent one.
        workdir: Project root.  Defaults to the current working dir.
        base_branch: PR base branch; recorded on the summary so callers
            can keep it next to the rest of the data.

    Returns:
        A populated :class:`SessionSummary`.  Missing fields are filled
        with sensible defaults (empty strings, zeroes) rather than
        raising, so the CLI can still open a PR when state is sparse.
    """
    root = workdir or Path.cwd()
    sessions_dir = _sessions_dir(root)

    wrapup_path: Path | None
    if session_id is None:
        wrapup_path = _pick_latest_wrapup(sessions_dir)
    else:
        wrapup_path = _load_by_session_id(sessions_dir, session_id)

    wrapup = _read_json(wrapup_path) if wrapup_path else {}

    # Fall back to the live session.json for the goal/cost when the
    # wrap-up file is missing or sparse.
    live_session = _read_json(root / ".sdd" / "runtime" / "session.json")

    # The run directory is the last resort for identity, and the only one
    # every run writes: a run that ended without a wrap-up file still left
    # its metadata and journal there.
    run_dir = _run_dir(root, session_id)
    run_metadata = _read_json(run_dir / _RUN_METADATA_FILENAME) if run_dir else {}
    run_id_on_disk = str(run_metadata.get("run_id") or (run_dir.name if run_dir else ""))

    resolved_id = str(
        wrapup.get("session_id") or session_id or live_session.get("run_id") or run_id_on_disk or "unknown"
    )
    goal = str(wrapup.get("goal") or live_session.get("goal") or "")
    changes_summary = str(wrapup.get("changes_summary") or "")
    branch = str(wrapup.get("branch") or live_session.get("branch") or run_metadata.get("git_branch") or "HEAD")
    diff_stat = str(wrapup.get("git_diff_stat") or wrapup.get("diff_stat") or "")
    if diff_stat == "(no uncommitted changes)":
        # Wrap-up's old fallback answered a different question -- "any
        # uncommitted changes right now?" -- and recorded that answer where
        # the branch diff-stat belongs. Ten pull requests shipped it as
        # their whole diff-stat block. Treat it as no answer so the git
        # enrichment recomputes the real one; wrap-up files that predate
        # the fix are still on disk and get replayed by bundle rescues.
        diff_stat = ""
    primary_role_raw = wrapup.get("primary_role") or live_session.get("primary_role")
    primary_role = str(primary_role_raw) if primary_role_raw else None

    gates = _gates_from_dict(wrapup.get("gates"))

    cost_raw = wrapup.get("cost")
    if isinstance(cost_raw, dict):
        # Re-key to satisfy strict typing: JSON never produces non-string keys.
        cost_typed: dict[str, object] = {str(k): v for k, v in cost_raw.items()}  # type: ignore[reportUnknownVariableType]
        cost = _cost_from_dict(cost_typed)
    else:
        # Derive a minimal cost object from the session file when no
        # wrap-up cost block was written.
        cost = CostBreakdown(
            total_usd=float(live_session.get("cost_spent", 0.0) or 0.0),  # type: ignore[arg-type]
            total_tokens=int(cast(int, live_session.get("total_tokens", 0) or 0)),
            by_role={},
        )

    # Link the sealed evidence bundle for the completed task the wrap-up names,
    # if one was sealed at completion (issue #2362, AC3). Absent a bundle the
    # field stays None and the PR body's Evidence block is omitted entirely.
    evidence = _evidence_summary_for_task(root, _candidate_task_ids(wrapup))

    merged_changes = _merged_changes_from_journal(run_dir) if run_dir else ()
    journal_head = _journal_head(run_dir) if run_dir else ""

    return SessionSummary(
        session_id=resolved_id,
        goal=goal,
        changes_summary=changes_summary,
        branch=branch,
        base_branch=base_branch,
        primary_role=primary_role,
        diff_stat=diff_stat,
        merged_changes=merged_changes,
        gates=gates,
        cost=cost,
        evidence=evidence,
        journal_head=journal_head,
    )
