"""Per-attempt orchestration logic for the autofix daemon.

The dispatcher is plain Python — no LLM in the scheduling loop —
which keeps every retry reproducible and every routing decision
auditable.  A single attempt walks through the following pipeline:

1. **Cap check.**  If the PR has already used its three attempts on
   the active push SHA the dispatcher escalates to ``needs-human``
   instead of dispatching a fourth.
2. **Cost check.**  The repo's ``cost_cap_usd`` budget is consulted
   *before* dispatch; an attempt that would exceed the cap is
   aborted and a comment is posted.
3. **Audit open.**  An HMAC-chained ``autofix.attempt.start`` event
   is appended to the audit log so any later analysis can replay
   exactly what the daemon decided to do, and why.
4. **Classify.**  The failing log is run through the keyword
   classifier; the chosen bucket maps directly onto a bandit arm
   (``opus``/``sonnet``/``haiku``).
5. **Goal synthesis.**  A short, deterministic goal string is built
   from the PR/run metadata and the truncated log.  It is the only
   thing handed to the spawned Bernstein run, so a future operator
   can reproduce the run by hand.
6. **Spawn.**  The dispatch hook is invoked.  Tests inject a stub;
   in production the hook calls ``bernstein run`` with the
   synthesised goal and the bandit-selected model.
7. **Audit close.**  A second event records the outcome (commit
   SHA, spend, success/failure).  The two events share a stable
   ``attempt_id`` so they can be joined by ``bernstein audit``.

The dispatcher does not push to git or comment on PRs by itself —
those side-effects are wired through the ``ActionAdapter`` protocol
so the daemon can be exercised in tests without ever touching the
network.
"""

from __future__ import annotations

import contextlib
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from bernstein.core.autofix.classifier import Classification, classify_failure
from bernstein.core.autofix.config import MAX_ATTEMPTS_PER_PUSH
from bernstein.core.autofix.metrics import (
    autofix_attempts_total,
    autofix_cost_usd_total,
)
from bernstein.core.autofix.ownership import render_session_trailer

if TYPE_CHECKING:
    from bernstein.core.autofix.config import RepoConfig
    from bernstein.core.autofix.gh_logs import LogExtraction
    from bernstein.core.autofix.ownership import PullRequestMetadata
    from bernstein.core.security.audit import AuditLog

#: Terminal outcomes emitted by :meth:`Dispatcher.dispatch`.  These
#: are also the values exported on the ``outcome`` Prometheus label.
AttemptOutcome = Literal[
    "success",
    "failed",
    "cost_capped",
    "needs_human",
    "skipped",
]


# ---------------------------------------------------------------------------
# Protocols (test seams)
# ---------------------------------------------------------------------------


class ActionAdapter(Protocol):
    """Side-effecting operations the dispatcher delegates to GitHub/git."""

    def post_comment(self, repo: str, pr_number: int, body: str) -> None:
        """Post a markdown comment on the PR."""

    def add_label(self, repo: str, pr_number: int, label: str) -> None:
        """Add a label to the PR."""

    def remove_label(self, repo: str, pr_number: int, label: str) -> None:
        """Remove a label from the PR (best-effort)."""


class DispatchHook(Protocol):
    """Spawn a fresh Bernstein run and return the resulting commit SHA."""

    def __call__(
        self,
        *,
        goal: str,
        model: str,
        effort: str,
        repo: str,
        head_branch: str,
        allow_force_push: bool,
        cost_cap_usd: float,
    ) -> DispatchResult: ...


class CostProbe(Protocol):
    """Return the USD spend of the most-recently-completed dispatch."""

    def __call__(self) -> float: ...


# ---------------------------------------------------------------------------
# Result / record dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchResult:
    """The outcome of a single dispatch invocation.

    Attributes:
        success: ``True`` when the spawned run produced a commit and
            CI flipped (or is expected to flip) green.
        commit_sha: The SHA of the attempt commit, or empty string
            when ``success`` is ``False``.
        cost_usd: USD spend reported by the cost tracker.  The
            dispatcher *also* surfaces this through
            :class:`AttemptRecord` so tests need not stub
            ``CostProbe`` separately.
        message: Human-readable summary suitable for an audit
            trailer / PR comment.
    """

    success: bool
    commit_sha: str = ""
    cost_usd: float = 0.0
    message: str = ""


@dataclass(frozen=True)
class AttemptRecord:
    """A single attempt's full audit-trail data.

    Attributes:
        attempt_id: Stable identifier shared by the open/close audit
            events.  Generated by the dispatcher.
        repo: ``owner/name`` repo identifier.
        pr_number: Pull-request number.
        push_sha: Head SHA of the PR at attempt time; the per-push
            cap key.
        run_id: GitHub Actions run identifier the daemon is repairing.
        session_id: Session id that established ownership.
        attempt_index: 1-indexed attempt counter for the active push
            SHA.  ``MAX_ATTEMPTS_PER_PUSH`` triggers ``needs_human``.
        classification: Routing decision, populated when the attempt
            reached the classifier stage.
        outcome: Terminal status; one of :data:`AttemptOutcome`.
        commit_sha: Commit SHA produced by the attempt, when any.
        cost_usd: USD recorded by the cost tracker.
        reason: Human-readable summary that explains the outcome.
    """

    attempt_id: str
    repo: str
    pr_number: int
    push_sha: str
    run_id: str
    session_id: str
    attempt_index: int
    outcome: AttemptOutcome
    classification: Classification | None = None
    commit_sha: str = ""
    cost_usd: float = 0.0
    reason: str = ""
    started_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Goal synthesis
# ---------------------------------------------------------------------------


def synthesise_goal(
    *,
    repo: str,
    pr_number: int,
    run_id: str,
    classification: Classification,
    log_excerpt: str,
    session_id: str,
) -> str:
    """Build the goal string passed to the spawned Bernstein run.

    The output is deterministic given identical inputs so the audit
    trail can replay any attempt by hand.

    Args:
        repo: ``owner/name`` repository identifier.
        pr_number: PR number being repaired.
        run_id: Failing GitHub Actions run id.
        classification: Result of the keyword classifier.
        log_excerpt: Truncated failing-log payload.
        session_id: Trailer value that established ownership.

    Returns:
        A multi-line goal string suitable for ``bernstein run``.
    """
    snippet = log_excerpt.strip() or "(no log captured)"
    return (
        f"fix({classification.kind}): repair CI on {repo}#{pr_number}\n"
        f"\n"
        f"GitHub Actions run {run_id} failed; classifier signalled "
        f"`{classification.kind}` (model={classification.model}).\n"
        f"\n"
        f"Failing log excerpt:\n"
        f"```\n{snippet}\n```\n"
        f"\n"
        f"{render_session_trailer(session_id)}"
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class Dispatcher:
    """Drives a single attempt to repair a failing PR.

    The dispatcher is intentionally stateless across attempts; the
    operator's per-PR-per-push counter lives in
    :class:`AttemptCounter`, injected at construction time so tests
    can preload counts.
    """

    def __init__(
        self,
        *,
        audit: AuditLog,
        action_adapter: ActionAdapter,
        dispatch_hook: DispatchHook,
        attempt_counter: AttemptCounter | None = None,
    ) -> None:
        self._audit = audit
        self._actions = action_adapter
        self._dispatch_hook = dispatch_hook
        self._counter = attempt_counter if attempt_counter is not None else AttemptCounter()

    # -- public API ---------------------------------------------------------

    def dispatch(
        self,
        *,
        repo_config: RepoConfig,
        pr: PullRequestMetadata,
        run_id: str,
        log: LogExtraction,
        session_id: str,
    ) -> AttemptRecord:
        """Run the full dispatch pipeline for one PR.

        Args:
            repo_config: Per-repo policy (cost cap, label, force-push
                permission).
            pr: PR metadata (head SHA, branch, labels, ...).
            run_id: Failing run id whose logs the daemon is repairing.
            log: Output of
                :func:`bernstein.core.autofix.gh_logs.extract_failed_log`.
            session_id: Trailer-resolved session id.

        Returns:
            A typed :class:`AttemptRecord` capturing everything that
            happened.  The same data is mirrored to the audit log
            and the Prometheus counters.
        """
        attempt_id = _new_attempt_id()
        attempt_index = self._counter.next_index(pr.head_sha)
        classification: Classification | None = None
        cost_usd = 0.0
        commit_sha = ""

        # ---- attempt cap (3 strikes) -------------------------------------
        if attempt_index > MAX_ATTEMPTS_PER_PUSH:
            self._escalate_to_human(repo_config, pr, run_id)
            record = AttemptRecord(
                attempt_id=attempt_id,
                repo=pr.repo,
                pr_number=pr.number,
                push_sha=pr.head_sha,
                run_id=run_id,
                session_id=session_id,
                attempt_index=attempt_index,
                outcome="needs_human",
                reason=(
                    f"Already attempted {MAX_ATTEMPTS_PER_PUSH} times on push {pr.head_sha[:12]}; escalating to human."
                ),
            )
            self._record_terminal(record)
            return record

        # ---- audit open --------------------------------------------------
        self._audit.log(
            event_type="autofix.attempt.start",
            actor="autofix-daemon",
            resource_type="pull_request",
            resource_id=f"{pr.repo}#{pr.number}",
            details={
                "attempt_id": attempt_id,
                "attempt_index": attempt_index,
                "run_id": run_id,
                "push_sha": pr.head_sha,
                "session_id": session_id,
                "repo": pr.repo,
                "log_truncated": log.truncated,
                "log_bytes": len(log.body.encode("utf-8")) if log.body else 0,
            },
        )

        # ---- classify ----------------------------------------------------
        classification = classify_failure(log.body)
        goal = synthesise_goal(
            repo=pr.repo,
            pr_number=pr.number,
            run_id=run_id,
            classification=classification,
            log_excerpt=log.body,
            session_id=session_id,
        )

        # ---- dispatch ----------------------------------------------------
        try:
            result = self._dispatch_hook(
                goal=goal,
                model=classification.model,
                effort=_effort_for(classification),
                repo=pr.repo,
                head_branch=pr.head_branch,
                allow_force_push=repo_config.allow_force_push,
                cost_cap_usd=repo_config.cost_cap_usd,
            )
        except Exception as exc:
            record = AttemptRecord(
                attempt_id=attempt_id,
                repo=pr.repo,
                pr_number=pr.number,
                push_sha=pr.head_sha,
                run_id=run_id,
                session_id=session_id,
                attempt_index=attempt_index,
                outcome="failed",
                classification=classification,
                reason=f"dispatch hook raised: {exc}",
            )
            self._record_terminal(record)
            return record

        cost_usd = float(result.cost_usd)
        commit_sha = result.commit_sha

        # ---- cost-cap post-check ----------------------------------------
        if repo_config.cost_cap_usd > 0 and cost_usd > repo_config.cost_cap_usd:
            self._comment_cost_cap(repo_config, pr, cost_usd)
            record = AttemptRecord(
                attempt_id=attempt_id,
                repo=pr.repo,
                pr_number=pr.number,
                push_sha=pr.head_sha,
                run_id=run_id,
                session_id=session_id,
                attempt_index=attempt_index,
                outcome="cost_capped",
                classification=classification,
                cost_usd=cost_usd,
                commit_sha=commit_sha,
                reason=(f"Cost ${cost_usd:.2f} exceeded repo cap ${repo_config.cost_cap_usd:.2f}; aborted."),
            )
            self._record_terminal(record)
            return record

        outcome: AttemptOutcome = "success" if result.success else "failed"
        reason = result.message or ("attempt commit landed" if result.success else "dispatch failed")

        record = AttemptRecord(
            attempt_id=attempt_id,
            repo=pr.repo,
            pr_number=pr.number,
            push_sha=pr.head_sha,
            run_id=run_id,
            session_id=session_id,
            attempt_index=attempt_index,
            outcome=outcome,
            classification=classification,
            cost_usd=cost_usd,
            commit_sha=commit_sha,
            reason=reason,
        )
        self._record_terminal(record)
        return record

    # -- helpers -----------------------------------------------------------

    def _record_terminal(self, record: AttemptRecord) -> None:
        """Emit metrics + the closing audit event for ``record``."""
        classifier_label = record.classification.kind if record.classification else "unknown"
        autofix_attempts_total.labels(
            repo=record.repo,
            outcome=record.outcome,
            classifier=classifier_label,
        ).inc()
        if record.cost_usd > 0:
            autofix_cost_usd_total.labels(repo=record.repo).inc(record.cost_usd)

        signals: tuple[str, ...] = record.classification.matched_signals if record.classification else ()
        self._audit.log(
            event_type="autofix.attempt.finish",
            actor="autofix-daemon",
            resource_type="pull_request",
            resource_id=f"{record.repo}#{record.pr_number}",
            details={
                "attempt_id": record.attempt_id,
                "attempt_index": record.attempt_index,
                "outcome": record.outcome,
                "classifier": classifier_label,
                "model": record.classification.model if record.classification else "",
                "matched_signals": list(signals),
                "cost_usd": round(record.cost_usd, 6),
                "commit_sha": record.commit_sha,
                "reason": record.reason,
                "run_id": record.run_id,
                "session_id": record.session_id,
                "push_sha": record.push_sha,
            },
        )

    def _escalate_to_human(
        self,
        repo_config: RepoConfig,
        pr: PullRequestMetadata,
        run_id: str,
    ) -> None:
        """Add ``needs-human`` and post a summary comment."""
        with contextlib.suppress(Exception):
            self._actions.add_label(pr.repo, pr.number, "needs-human")
        body = (
            "Autofix has reached the {cap}-attempt cap on push `{sha}` "
            "(run {run_id}). A human is now needed.\n\n"
            "Remove the `{label}` label after fixing to acknowledge."
        ).format(
            cap=MAX_ATTEMPTS_PER_PUSH,
            sha=pr.head_sha[:12] or "(unknown)",
            run_id=run_id,
            label=repo_config.label,
        )
        with contextlib.suppress(Exception):
            self._actions.post_comment(pr.repo, pr.number, body)

    def _comment_cost_cap(
        self,
        repo_config: RepoConfig,
        pr: PullRequestMetadata,
        cost_usd: float,
    ) -> None:
        """Post a comment explaining a cost-cap abort."""
        body = (
            f"Autofix attempt aborted: spend ${cost_usd:.2f} exceeded the "
            f"per-attempt cap of ${repo_config.cost_cap_usd:.2f} configured "
            f"for `{repo_config.name}`."
        )
        with contextlib.suppress(Exception):
            self._actions.post_comment(pr.repo, pr.number, body)


def _effort_for(classification: Classification) -> str:
    """Pick the bandit effort level given a classification.

    Security regressions deserve ``high`` effort; flaky and config
    failures stay at ``low`` to keep cost predictable.
    """
    return "high" if classification.kind == "security" else "low"


def _new_attempt_id() -> str:
    """Return a short, opaque attempt identifier."""
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Per-PR-per-push attempt counter
# ---------------------------------------------------------------------------


class AttemptCounter:
    """Tracks how many attempts have been dispatched per push SHA.

    The counter is in-memory by design: when the daemon restarts the
    HMAC-chained audit log is the source of truth and the operator
    can rebuild counts by replaying it.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def next_index(self, push_sha: str) -> int:
        """Increment and return the 1-indexed counter for ``push_sha``.

        An empty / unknown push SHA bypasses the cap (returns 1 every
        time) so the daemon never refuses to act when the metadata is
        sparse.
        """
        if not push_sha:
            return 1
        nxt = self._counts.get(push_sha, 0) + 1
        self._counts[push_sha] = nxt
        return nxt

    def reset(self, push_sha: str) -> None:
        """Drop the counter for ``push_sha``."""
        self._counts.pop(push_sha, None)

    def peek(self, push_sha: str) -> int:
        """Return the current count without incrementing."""
        return self._counts.get(push_sha, 0)
