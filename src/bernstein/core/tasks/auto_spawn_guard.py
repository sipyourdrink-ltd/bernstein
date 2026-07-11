"""Recursion/dedupe/cap guard for auto-spawned meta-tasks.

Guards against the "recursive junk task" production incident: with no limits,
the evolution loop's "Upgrade: ..." proposal tasks and the watchdog's
"Watchdog triage: ..." tasks can spawn unboundedly, including meta-tasks
*about* other meta-tasks (e.g. "Watchdog triage of watchdog triage" - a triage
task created because a previous triage task itself stalled). A single real
run degenerated into ~19 "Upgrade: Improve task success rate" duplicates plus
recursive watchdog-triage-of-watchdog-triage chains with zero forward
progress. See work/agent-reports/2026-07-02-run9-attempt9-audit.md.

This module is shared by both known auto-spawn sites:
- ``bernstein.core.orchestration.orchestrator_evolve._create_upgrade_tasks``
- ``bernstein.core.observability.watchdog.WatchdogManager._create_triage_task``

Guard order (first hit wins, cheapest checks first):
1. Ancestry depth: refuse spawning a meta-task ABOUT a task that is itself an
   auto-spawned meta-task (its title already carries a known meta-task
   prefix). This caps recursion at depth 1: normal-task -> meta-task is
   allowed, meta-task -> meta-task-about-a-meta-task is refused.
2. Dedupe: refuse spawning a meta-task whose (normalized) title matches an
   already-open auto-spawned task. Normalization: lowercase, collapse
   whitespace, strip a trailing id-like suffix (e.g. "(abc123)"); a match is
   either an exact normalized match or one title containing the other (both
   at least 8 characters) - good enough to catch cosmetic drift like an
   appended session id without conflating unrelated short titles.
3. Cap: refuse once the number of ALLOWED auto-spawns for this run has
   reached ``max_auto_spawns_per_run``. State is a small JSON counter
   persisted under the run's ``.sdd/runtime/`` directory so multiple call
   sites (and process restarts within the same run) share one cap.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Title prefixes produced by known auto-spawn sites. A source task whose title
# starts with one of these is itself an auto-spawned meta-task.
META_TASK_PREFIXES: tuple[str, ...] = ("Upgrade:", "Watchdog triage:")

DEFAULT_MAX_ANCESTRY_DEPTH = 1
DEFAULT_MAX_AUTO_SPAWNS_PER_RUN = 3

_STATE_RELATIVE_PATH = Path(".sdd") / "runtime" / "auto_spawn_guard.json"

_TRAILING_ID_RE = re.compile(r"[\[(][0-9a-f-]{6,}[\])]\s*$")
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class AutoSpawnDecision:
    """Result of an :meth:`AutoSpawnGuard.evaluate` call."""

    allowed: bool
    reason: str  # "allowed" | "depth" | "dedupe" | "cap"
    ancestry_depth: int
    current_count: int
    cap: int


def meta_task_kind(title: str | None) -> str | None:
    """Return the matched ``META_TASK_PREFIXES`` category for ``title``, or ``None``.

    This is the "title-CLASS" used for category-based dedupe: two meta-task
    titles in the same category (e.g. both ``"Upgrade: ..."``) are treated as
    the same class of auto-spawn even when their specific wording differs
    (e.g. "Upgrade: Improve task success rate" vs. "Upgrade: Improve token
    budget") -- see :func:`_titles_match`.
    """
    if not title:
        return None
    for prefix in META_TASK_PREFIXES:
        if prefix in title:
            return prefix
    return None


def compute_ancestry_depth(source_title: str | None) -> int:
    """Ancestry depth of a meta-task that would be created ABOUT ``source_title``.

    A meta-task about an ordinary task has depth 1. If the source task's own
    title already carries a known meta-task prefix (i.e. the source is itself
    an auto-spawned meta-task), the new meta-task would be depth 2 -- exactly
    the "triage of triage" / "upgrade of upgrade" shape observed in
    production.
    """
    depth = 1
    if not source_title:
        return depth
    for prefix in META_TASK_PREFIXES:
        if prefix in source_title:
            depth += 1
            break
    return depth


def _normalize_title(title: str) -> str:
    normalized = title.strip().lower()
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    normalized = _TRAILING_ID_RE.sub("", normalized).strip()
    return normalized


def _titles_match(candidate: str, existing: str) -> bool:
    """Dedupe match: exact (normalized) match, or one title containing the other.

    History: an earlier version of this function additionally treated ANY
    two titles sharing the same known meta-task prefix (e.g. both starting
    with "Upgrade:") as duplicates regardless of content, to catch a real
    incident where "Upgrade: Improve task success rate" survived as three
    sequential, differently-worded task rows. That title-CLASS rule was too
    coarse: it also refused genuinely distinct, unrelated proposals that
    merely shared a prefix (e.g. "Upgrade: Proposal One" vs "Upgrade:
    Proposal Two" from two independent evolution findings in the same
    cycle) -- see tests/unit/test_orchestrator.py::
    test_happy_path_creates_http_task_per_proposal, which regressed under
    the coarse rule (2026-07-02).

    The dedupe key is now purely content-based: normalize both titles
    (lowercase, collapse whitespace, strip a trailing id-like suffix such as
    "(abc123)"), then match on exact equality or containment (gated on both
    normalized titles being at least 8 characters so short generic titles
    like "Fix bug" don't spuriously collide). This still catches true
    resurrection-duplicates -- the retry/resurrection path always copies the
    title verbatim or with only a cosmetic id-suffix change -- while letting
    two differently-worded, independently-generated proposals coexist even
    when they share a category prefix.
    """
    normalized_candidate = _normalize_title(candidate)
    normalized_existing = _normalize_title(existing)
    if not normalized_candidate or not normalized_existing:
        return False
    if normalized_candidate == normalized_existing:
        return True
    if len(normalized_candidate) >= 8 and len(normalized_existing) >= 8:
        return normalized_candidate in normalized_existing or normalized_existing in normalized_candidate
    return False


class AutoSpawnGuard:
    """Shared cap/dedupe/depth guard for auto-spawned meta-tasks.

    The allowed-spawn counter is persisted to
    ``<workdir>/.sdd/runtime/auto_spawn_guard.json`` so every call site in the
    same run (and across process restarts within that run) shares a single
    cap, rather than each site independently allowing up to its own cap.
    """

    def __init__(
        self,
        workdir: Path,
        *,
        max_ancestry_depth: int = DEFAULT_MAX_ANCESTRY_DEPTH,
        max_auto_spawns_per_run: int = DEFAULT_MAX_AUTO_SPAWNS_PER_RUN,
    ) -> None:
        self._workdir = workdir
        self._max_ancestry_depth = max_ancestry_depth
        self._max_auto_spawns_per_run = max_auto_spawns_per_run
        self._state_path = workdir / _STATE_RELATIVE_PATH

    def evaluate(
        self,
        *,
        kind: str,
        title: str,
        source_title: str | None,
        existing_open_titles: list[str],
    ) -> AutoSpawnDecision:
        """Decide whether an auto-spawn of ``title`` (of type ``kind``) is allowed.

        Args:
            kind: Caller-supplied label for the auto-spawn site, used only for
                logging (e.g. "upgrade_proposal", "watchdog_triage",
                "retry:upgrade_proposal" for a retry-path recreation of a
                meta-task).
            title: The title of the meta-task that would be created.
            source_title: Title of the task/finding this meta-task would be
                about, if any. Used to compute ancestry depth.
            existing_open_titles: Titles of currently-open (open or claimed)
                auto-spawned tasks, for dedupe comparison.

        Every call - allowed or refused - is logged at INFO with the full
        decision inputs (reason, dedupe key, ancestry depth); this is the
        primary debugging surface for auto-spawn behaviour, so the log line
        is never truncated or downgraded to DEBUG.
        """
        # Content-based dedupe key (normalized title) -- see _titles_match's
        # docstring for why this is no longer just the meta-task prefix
        # (that coarser key falsely collided distinct proposals sharing a
        # category, e.g. "Upgrade: Proposal One" vs "Upgrade: Proposal Two").
        dedupe_key = _normalize_title(title)
        depth = compute_ancestry_depth(source_title)
        count = self._load_count()

        if depth > self._max_ancestry_depth:
            decision = AutoSpawnDecision(
                allowed=False,
                reason="depth",
                ancestry_depth=depth,
                current_count=count,
                cap=self._max_auto_spawns_per_run,
            )
            logger.warning(
                "Auto-spawn refused (depth): kind=%s title=%r ancestry_depth=%d max_ancestry_depth=%d "
                "source_title=%r dedupe_key=%r",
                kind,
                title,
                depth,
                self._max_ancestry_depth,
                source_title,
                dedupe_key,
            )
            self._log_decision(
                kind=kind,
                title=title,
                source_title=source_title,
                dedupe_key=dedupe_key,
                decision=decision,
            )
            return decision

        for existing in existing_open_titles:
            is_match = _titles_match(title, existing)
            # Every candidate/existing comparison is logged at INFO with the
            # dedupe key and verdict -- "log every dedupe decision with the
            # key + verdict + reason" (not just the final refusal/allow).
            logger.info(
                "auto_spawn_dedupe_check kind=%s dedupe_key=%r candidate=%r existing=%r match=%s",
                kind,
                dedupe_key,
                title,
                existing,
                is_match,
            )
            if is_match:
                decision = AutoSpawnDecision(
                    allowed=False,
                    reason="dedupe",
                    ancestry_depth=depth,
                    current_count=count,
                    cap=self._max_auto_spawns_per_run,
                )
                logger.warning(
                    "Auto-spawn refused (dedupe): kind=%s title=%r duplicates existing open task %r dedupe_key=%r",
                    kind,
                    title,
                    existing,
                    dedupe_key,
                )
                self._log_decision(
                    kind=kind, title=title, source_title=source_title, dedupe_key=dedupe_key, decision=decision
                )
                return decision

        if count >= self._max_auto_spawns_per_run:
            decision = AutoSpawnDecision(
                allowed=False,
                reason="cap",
                ancestry_depth=depth,
                current_count=count,
                cap=self._max_auto_spawns_per_run,
            )
            logger.warning(
                "Auto-spawn refused (cap): kind=%s title=%r current_count=%d cap=%d dedupe_key=%r",
                kind,
                title,
                count,
                self._max_auto_spawns_per_run,
                dedupe_key,
            )
            self._log_decision(
                kind=kind,
                title=title,
                source_title=source_title,
                dedupe_key=dedupe_key,
                decision=decision,
            )
            return decision

        new_count = count + 1
        self._save_count(new_count)
        decision = AutoSpawnDecision(
            allowed=True,
            reason="allowed",
            ancestry_depth=depth,
            current_count=new_count,
            cap=self._max_auto_spawns_per_run,
        )
        self._log_decision(kind=kind, title=title, source_title=source_title, dedupe_key=dedupe_key, decision=decision)
        return decision

    def _log_decision(
        self,
        *,
        kind: str,
        title: str,
        source_title: str | None,
        dedupe_key: str,
        decision: AutoSpawnDecision,
    ) -> None:
        """Uniform INFO-level record of every auto-spawn decision (allowed or refused).

        This is intentionally separate from (and in addition to) the
        per-branch WARNING logs above: those are for operator alerting on
        refusals, this is the single always-on debugging trail - never
        truncated, always includes the exact dedupe key and ancestry depth
        used, per the "logging IS the debugging interface" rule.
        """
        logger.info(
            "auto_spawn_decision kind=%s title=%r source_title=%r allowed=%s reason=%s dedupe_key=%r "
            "ancestry_depth=%d current_count=%d cap=%d",
            kind,
            title,
            source_title,
            decision.allowed,
            decision.reason,
            dedupe_key,
            decision.ancestry_depth,
            decision.current_count,
            decision.cap,
        )

    def _load_count(self) -> int:
        if not self._state_path.exists():
            return 0
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Auto-spawn guard state unreadable, resetting: %s", self._state_path)
            return 0
        if not isinstance(raw, dict):
            return 0
        count = raw.get("count", 0)
        return count if isinstance(count, int) else 0

    def _save_count(self, count: int) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"count": count, "updated_at": time.time()}
        self._state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
