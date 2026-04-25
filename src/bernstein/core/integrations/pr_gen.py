"""Pure-logic helpers for composing pull-request titles and bodies.

This module converts a completed Bernstein session into the title and
markdown body of a GitHub pull request.  It is deliberately free of
``click`` and ``subprocess`` imports so it can be unit-tested in
isolation; the CLI wrapper in :mod:`bernstein.cli.commands.pr_cmd`
handles I/O, git push and ``gh`` invocation.

The module reuses existing Bernstein state:

* :class:`bernstein.core.persistence.session.SessionState` — run-level
  goal, completed task ids and cumulative cost.
* :class:`bernstein.core.persistence.session.WrapUpBrief` — per-session
  diff-stat and changes summary written on graceful stop.
* :class:`bernstein.core.tasks.models.JanitorResult` — quality-gate
  signal results used for the Verification section.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


_WRAPUP_GLOB = "*-wrapup.json"


__all__ = [
    "GateResult",
    "SessionSummary",
    "build_pr_body",
    "build_pr_title",
    "load_session_summary",
]


# Hard cap on a PR title — GitHub renders long titles awkwardly and most
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
        gates: Quality-gate outcomes from the janitor.
        cost: Aggregate cost figures for the session.
    """

    session_id: str
    goal: str
    branch: str
    base_branch: str = "main"
    primary_role: str | None = None
    diff_stat: str = ""
    gates: tuple[GateResult, ...] = ()
    cost: CostBreakdown = field(default_factory=CostBreakdown)


# ---------------------------------------------------------------------------
# Title generation
# ---------------------------------------------------------------------------


def _classify(goal: str, role: str | None) -> str:
    """Pick a conventional-commit type from the goal + role.

    Args:
        goal: Task goal / session description.
        role: Primary role, if known.

    Returns:
        One of :data:`_CC_PREFIXES`; defaults to ``"feat"``.
    """
    lowered = goal.lower()

    for prefix in _CC_PREFIXES:
        if lowered.startswith(f"{prefix}:") or lowered.startswith(f"{prefix}("):
            return prefix

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


def build_pr_title(task_goal: str, role: str | None) -> str:
    """Compose a conventional-commit pull-request title.

    The result is truncated to :data:`_TITLE_MAX_CHARS` characters with a
    trailing ellipsis when the cleaned goal is longer.  The shape is
    always ``"<type>: <outcome>"``.

    Args:
        task_goal: Session goal or first-task title.
        role: Primary role for the session, used as a classification
            hint when the goal offers no other signal.

    Returns:
        A title at most :data:`_TITLE_MAX_CHARS` characters long.
    """
    prefix = _classify(task_goal, role)
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
        detail = f" — {gate.detail}" if gate.detail else ""
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


def build_pr_body(session: SessionSummary) -> str:
    """Render the full markdown body for a pull request.

    The output is structured so downstream reviewers (and tooling) can
    reliably grep for section headers.  All four sections — Summary,
    Changes, Verification and Cost — are always present even when the
    underlying data is empty, so tests can rely on their presence.

    Args:
        session: The fully-populated session summary.

    Returns:
        A markdown string ready to pass to ``gh pr create --body``.
    """
    bullets = "\n".join(f"- {line}" for line in _summary_bullets(session.goal))

    # The ``bernstein-session-id`` trailer is consumed by the autofix
    # daemon to claim ownership of PRs Bernstein opened — keeping it
    # on its own line lets ``gh pr view --json body`` callers parse it
    # with a single regex.
    short_id = session.session_id[:12] if session.session_id else "unknown"
    parts: list[str] = [
        "## Summary",
        bullets,
        "",
        "## Changes",
        _format_diff_stat(session.diff_stat),
        "",
        "## Verification",
        _format_gates(session.gates),
        "",
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
    # Normalise to ``dict[str, object]`` — json.loads never produces
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

    resolved_id = str(wrapup.get("session_id") or session_id or live_session.get("run_id") or "unknown")
    goal = str(wrapup.get("goal") or live_session.get("goal") or "")
    branch = str(wrapup.get("branch") or live_session.get("branch") or "HEAD")
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

    return SessionSummary(
        session_id=resolved_id,
        goal=goal,
        branch=branch,
        base_branch=base_branch,
        primary_role=primary_role,
        diff_stat=diff_stat,
        gates=gates,
        cost=cost,
    )
