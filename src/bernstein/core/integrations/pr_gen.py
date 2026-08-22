"""Pure-logic helpers for composing pull-request titles and bodies.

This module converts a completed Bernstein session into the title and
markdown body of a GitHub pull request.  It is deliberately free of
``click`` and ``subprocess`` imports so it can be unit-tested in
isolation; the CLI wrapper in :mod:`bernstein.cli.commands.pr_cmd`
handles I/O, git push and ``gh`` invocation.

The module reuses existing Bernstein state:

* :class:`bernstein.core.persistence.session.SessionState` - run-level
  goal, completed task ids and cumulative cost.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


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
    "EvidenceSummary",
    "GateResult",
    "MergedChange",
    "SessionSummary",
    "build_pr_body",
    "build_pr_title",
    "load_session_summary",
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
    """

    session_id: str
    goal: str
    branch: str
    base_branch: str = "main"
    primary_role: str | None = None
    diff_stat: str = ""
    merged_changes: tuple[MergedChange, ...] = ()
    gates: tuple[GateResult, ...] = ()
    cost: CostBreakdown = field(default_factory=CostBreakdown)
    evidence: EvidenceSummary | None = None


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

    return cleaned[0].lower() + cleaned[1:]


def build_pr_title(task_goal: str, role: str | None, labels: Iterable[str] = ()) -> str:
    """Compose a conventional-commit pull-request title.

    The result is truncated to :data:`_TITLE_MAX_CHARS` characters with a
    trailing ellipsis when the cleaned goal is longer.  The shape is
    always ``"<type>: <outcome>"``.

    Args:
        task_goal: Session goal or first-task title.
        role: Primary role for the session, used as a classification
            hint when the goal offers no other signal.
        labels: Labels on the linked issue. They outrank both the wording
            and the role, so a PR never announces a change type the issue
            it closes contradicts.

    Returns:
        A title at most :data:`_TITLE_MAX_CHARS` characters long.
    """
    prefix = _classify(task_goal, role, labels)
    outcome = _shape_outcome(task_goal)

    full = f"{prefix}: {outcome}"
    if len(full) <= _TITLE_MAX_CHARS:
        return full

    # Leave room for the ellipsis so the hard cap is honoured.
    budget = _TITLE_MAX_CHARS - len(prefix) - len(": ") - 1
    return f"{prefix}: {outcome[:budget].rstrip()}…"


# ---------------------------------------------------------------------------
# Body generation
# ---------------------------------------------------------------------------


def _summary_bullets(goal: str) -> list[str]:
    """Split a goal into up to three bullet points.

    Sentences separated by ``.``/``;`` become bullets; a single short
    goal is returned as one bullet verbatim.
    """
    parts = [p.strip() for p in re.split(r"[.;]\s+", goal.strip()) if p.strip()]
    if not parts:
        return ["Automated session completed with no explicit goal."]
    return parts[:3]


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


def _format_cost(cost: CostBreakdown) -> str:
    """Render the cost section as a markdown list."""
    lines: list[str] = [
        f"- **Total:** ${cost.total_usd:.2f}",
        f"- **Tokens:** {cost.total_tokens:,}",
    ]

    if cost.total_tokens > 0 and cost.total_usd > 0:
        rate = (cost.total_usd / cost.total_tokens) * 1_000_000
        lines.append(f"- **Effective rate:** ${rate:.2f} / 1M tokens")
    else:
        lines.append("- **Effective rate:** n/a")

    if cost.by_role:
        by_role_sorted = sorted(cost.by_role.items(), key=lambda kv: -kv[1])
        role_fragments = ", ".join(f"{role} ${usd:.2f}" for role, usd in by_role_sorted)
        lines.append(f"- **By role:** {role_fragments}")

    return "\n".join(lines)


def _format_diff_stat(diff_stat: str) -> str:
    """Render the diff-stat in a fenced code block, or a fallback line."""
    stripped = diff_stat.strip()
    if not stripped:
        return "_No changes recorded for this session._"
    return f"```\n{stripped}\n```"


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


def _format_changes(session: SessionSummary) -> str:
    """Render the Changes section from the diff-stat and the merged tasks.

    A run whose branch has already been folded into the base leaves ``git
    diff`` with nothing to report, which is how a PR full of merged work came
    to say no changes were recorded. The journal knows better, so both are
    shown and the fallback line is reached only when neither has anything.
    """
    blocks: list[str] = []
    if session.diff_stat.strip():
        blocks.append(_format_diff_stat(session.diff_stat))
    if session.merged_changes:
        blocks.append(_format_merged_changes(session.merged_changes))
    if not blocks:
        return _format_diff_stat("")
    return "\n\n".join(blocks)


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


def build_pr_body(session: SessionSummary) -> str:
    """Render the full markdown body for a pull request.

    The output is structured so downstream reviewers (and tooling) can
    reliably grep for section headers.  All four sections - Summary,
    Changes, Verification and Cost - are always present even when the
    underlying data is empty, so tests can rely on their presence.

    Args:
        session: The fully-populated session summary.

    Returns:
        A markdown string ready to pass to ``gh pr create --body``.
    """
    bullets = "\n".join(f"- {line}" for line in _summary_bullets(session.goal))

    # The ``bernstein-session-id`` trailer is consumed by the autofix
    # daemon to claim ownership of PRs Bernstein opened - keeping it
    # on its own line lets ``gh pr view --json body`` callers parse it
    # with a single regex.
    short_id = session.session_id[:12] if session.session_id else "unknown"
    parts: list[str] = [
        "## Summary",
        bullets,
        "",
        "## Changes",
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
    parts += [
        "## Cost",
        _format_cost(session.cost),
        "",
        "---",
        f"_Generated from Bernstein session `{short_id}`._",
        "",
        f"bernstein-session-id: {short_id}",
    ]
    return "\n".join(parts)


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
        total_tokens = int(raw.get("total_tokens", 0))  # type: ignore[arg-type]
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
        journal = contained_run_journal(runs_root, entry.name)
        if journal is None:
            continue
        run_dir = journal.parent
        if not (journal.exists() or (run_dir / _RUN_METADATA_FILENAME).exists()):
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
    branch = str(wrapup.get("branch") or live_session.get("branch") or run_metadata.get("git_branch") or "HEAD")
    diff_stat = str(wrapup.get("git_diff_stat") or wrapup.get("diff_stat") or "")
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
            total_tokens=int(live_session.get("total_tokens", 0) or 0),  # type: ignore[arg-type]
            by_role={},
        )

    # Link the sealed evidence bundle for the completed task the wrap-up names,
    # if one was sealed at completion (issue #2362, AC3). Absent a bundle the
    # field stays None and the PR body's Evidence block is omitted entirely.
    evidence = _evidence_summary_for_task(root, _candidate_task_ids(wrapup))

    merged_changes = _merged_changes_from_journal(run_dir) if run_dir else ()

    return SessionSummary(
        session_id=resolved_id,
        goal=goal,
        branch=branch,
        base_branch=base_branch,
        primary_role=primary_role,
        diff_stat=diff_stat,
        merged_changes=merged_changes,
        gates=gates,
        cost=cost,
        evidence=evidence,
    )
