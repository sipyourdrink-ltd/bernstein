"""High-level orchestration of a single review round.

The responder takes a sealed :class:`ReviewRound`, classifies each
comment (address / dismiss-stale / dismiss-question), runs the dispatch
through a caller-supplied ``round_runner`` callable, consults the
always-allow gate before letting the runner commit, enforces a per-round
cost cap via :class:`bernstein.core.cost.cost_tracker.CostTracker`, and
records an HMAC-chained audit entry for every outcome.

The runner contract is intentionally narrow so tests can supply a fake:

.. code-block:: python

    def runner(round, prompt) -> RunnerOutcome: ...

where ``RunnerOutcome`` carries the resulting commit SHA, cost, and a
summary message that gets posted back to the PR.
"""

from __future__ import annotations

import logging
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.review_responder.gh_client import GhClient
from bernstein.core.review_responder.metrics import record_round
from bernstein.core.review_responder.models import (
    CommentDecision,
    ReviewRound,
    RoundOutcome,
    RoundResult,
)

if TYPE_CHECKING:
    from bernstein.core.review_responder.dedup import DedupQueue
    from bernstein.core.review_responder.models import (
        ResponderConfig,
        ReviewComment,
    )
    from bernstein.core.security.always_allow import AlwaysAllowEngine
    from bernstein.core.security.audit import AuditLog

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunnerOutcome:
    """What a round runner returns to the responder.

    Attributes:
        commit_sha: SHA produced by the round (empty on no-op).
        cost_usd: Cost incurred while running the round.
        summary: One-line summary posted back to the PR.
        success: ``True`` when the runner produced a usable commit, else
            ``False`` (which causes the responder to record an ``ERROR``
            outcome without committing).
    """

    commit_sha: str
    cost_usd: float
    summary: str
    success: bool = True


@dataclass(frozen=True)
class GateAdvice:
    """Result of consulting the always-allow gate.

    Attributes:
        allowed: ``True`` to proceed with the commit, ``False`` to abort.
        reason: Human-readable rationale recorded in the audit log.
    """

    allowed: bool
    reason: str


GateConsult = Callable[[ReviewRound, RunnerOutcome], GateAdvice]
RoundRunner = Callable[[ReviewRound, str], RunnerOutcome]


def _default_gate(_round: ReviewRound, _outcome: RunnerOutcome) -> GateAdvice:
    """Fallback gate that allows commits unconditionally.

    The production wiring replaces this with a function that consults the
    real :class:`AlwaysAllowEngine`.  Tests use this default to keep the
    happy path concise.
    """
    return GateAdvice(allowed=True, reason="default-gate-allow")


def _compose_prompt(round_obj: ReviewRound) -> str:
    """Render a deterministic prompt for the round runner.

    The prompt embeds, for every comment, the file path, line range,
    reviewer username, and body — enough context for the spawned agent
    to act without re-reading the PR.
    """
    lines: list[str] = [
        f"You are addressing review comments on PR #{round_obj.pr_number} of {round_obj.repo}.",
        "Apply the smallest correct change for each comment.  Do NOT auto-merge.",
        "Produce ONE commit covering all comments below.",
        "",
        "Comments:",
    ]
    for c in round_obj.comments:
        lines.append(f"  - file=`{c.path}` lines={c.line_start}-{c.line_end} reviewer=@{c.reviewer} id={c.comment_id}")
        body = textwrap.indent(c.body.strip(), "      | ")
        lines.append(body if body else "      | (empty body)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _is_question(body: str, markers: tuple[str, ...]) -> bool:
    """Return ``True`` when ``body`` looks like a discussion question.

    Args:
        body: Comment body text.
        markers: Phrases that flag a comment as conversational.

    Returns:
        ``True`` if any marker substring is present (case-insensitive).
    """
    low = body.lower()
    return any(m in low for m in markers)


def classify_comments(
    round_obj: ReviewRound,
    *,
    diff_lines: dict[str, set[int]] | None,
    config: ResponderConfig,
) -> list[CommentDecision]:
    """Decide whether to address, dismiss-stale, or dismiss-question each comment.

    Args:
        round_obj: Sealed round.
        diff_lines: Mapping of file path to changed line numbers.  When
            ``None``, the staleness check is skipped (used by tests).
        config: Responder configuration; only ``question_markers`` is read.

    Returns:
        One :class:`CommentDecision` per comment, in the same order.
    """
    decisions: list[CommentDecision] = []
    for c in round_obj.comments:
        if _is_question(c.body, config.question_markers):
            decisions.append(
                CommentDecision(
                    comment=c,
                    action="dismiss_question",
                    reason="discussion-style question — needs a human reply",
                )
            )
            continue

        if diff_lines is not None and not _line_still_in_diff(c, diff_lines):
            decisions.append(
                CommentDecision(
                    comment=c,
                    action="dismiss_stale",
                    reason="cited line range no longer present in PR diff",
                )
            )
            continue

        decisions.append(CommentDecision(comment=c, action="address", reason=""))
    return decisions


def _line_still_in_diff(comment: ReviewComment, diff_lines: dict[str, set[int]]) -> bool:
    """Return ``True`` if any cited line is still in the diff for the file."""
    file_lines = diff_lines.get(comment.path)
    if file_lines is None:
        return False
    return any(ln in file_lines for ln in range(comment.line_start, comment.line_end + 1))


@dataclass
class ReviewResponder:
    """Coordinator that turns sealed rounds into PR replies.

    Args:
        config: Responder configuration.
        runner: Callable that performs the actual code change.
        audit: HMAC-chained audit log used for every round outcome.
        dedup: Persistent dedup queue; the responder marks records with
            their final outcome so a daemon restart can skip them.
        gh: Wrapper around the ``gh`` CLI for GitHub writes.
        gate_consult: Function that ANDs together the always-allow check
            and the per-round cost cap.  Tests pass a stub.
        diff_provider: Optional override for fetching ``{path: line_set}``;
            when ``None`` the responder falls back to ``gh.get_pr_diff_lines``.
    """

    config: ResponderConfig
    runner: RoundRunner
    audit: AuditLog
    dedup: DedupQueue
    gh: GhClient = field(default_factory=GhClient)
    gate_consult: GateConsult = field(default=_default_gate)
    diff_provider: Callable[[ReviewRound], dict[str, set[int]] | None] | None = None

    # ------------------------------------------------------------------
    # Run a round
    # ------------------------------------------------------------------

    def run_round(self, round_obj: ReviewRound) -> RoundResult:
        """Execute a sealed round end-to-end.

        Args:
            round_obj: The bundle to process.

        Returns:
            A :class:`RoundResult` with the final outcome.  The same
            object is also reflected in metrics, audit, and the dedup
            queue.
        """
        result = RoundResult(round_id=round_obj.round_id)

        diff_lines = self._fetch_diff_lines(round_obj)
        decisions = classify_comments(round_obj, diff_lines=diff_lines, config=self.config)

        addressables = [d for d in decisions if d.action == "address"]
        for d in decisions:
            if d.action == "dismiss_stale":
                self._dismiss_stale(round_obj, d, result)
            elif d.action == "dismiss_question":
                self._dismiss_question(round_obj, d, result)

        if not addressables:
            self._finalise(round_obj, result, default_outcome=RoundOutcome.NO_OP)
            return result

        addressable_round = ReviewRound(
            round_id=round_obj.round_id,
            repo=round_obj.repo,
            pr_number=round_obj.pr_number,
            comments=tuple(d.comment for d in addressables),
            opened_at=round_obj.opened_at,
            sealed_at=round_obj.sealed_at,
        )
        prompt = _compose_prompt(addressable_round)
        outcome = self._run_runner(addressable_round, prompt, result)
        if outcome is None:
            self._finalise(round_obj, result, default_outcome=RoundOutcome.ERROR)
            return result

        if outcome.cost_usd > self.config.per_round_cost_cap_usd:
            result.cost_usd = outcome.cost_usd
            result.outcome = RoundOutcome.COST_CAP_BREACHED
            result.notes = f"cost cap breached: ${outcome.cost_usd:.4f} > ${self.config.per_round_cost_cap_usd:.4f}"
            self._post_needs_human(round_obj, result)
            self._finalise(round_obj, result, default_outcome=RoundOutcome.COST_CAP_BREACHED)
            return result

        gate = self.gate_consult(addressable_round, outcome)
        if not gate.allowed:
            result.outcome = RoundOutcome.NEEDS_HUMAN
            result.cost_usd = outcome.cost_usd
            result.notes = f"gate denied: {gate.reason}"
            self._post_needs_human(round_obj, result)
            self._finalise(round_obj, result, default_outcome=RoundOutcome.NEEDS_HUMAN)
            return result

        # Commit succeeded — post resolution per-comment + summary.
        result.commit_sha = outcome.commit_sha
        result.cost_usd = outcome.cost_usd
        result.outcome = RoundOutcome.COMMITTED
        result.notes = f"runner committed {outcome.commit_sha[:12]}"
        for d in addressables:
            self._mark_addressed(round_obj, d.comment, outcome.commit_sha)
            result.addressed.append(d.comment.comment_id)
        self._post_round_summary(round_obj, outcome, addressables)
        self._finalise(round_obj, result, default_outcome=RoundOutcome.COMMITTED)
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_diff_lines(self, round_obj: ReviewRound) -> dict[str, set[int]] | None:
        """Resolve ``diff_lines`` via the injected provider or the GH client."""
        if self.diff_provider is not None:
            return self.diff_provider(round_obj)
        return self.gh.get_pr_diff_lines(round_obj.repo, round_obj.pr_number)

    def _run_runner(
        self,
        round_obj: ReviewRound,
        prompt: str,
        result: RoundResult,
    ) -> RunnerOutcome | None:
        """Invoke the configured runner, capturing exceptions as ERROR notes."""
        try:
            outcome = self.runner(round_obj, prompt)
        except Exception as exc:  # pragma: no cover - exercised by tests via stubs
            logger.exception("Round runner crashed for %s", round_obj.round_id)
            result.notes = f"runner-exception: {type(exc).__name__}"
            result.outcome = RoundOutcome.ERROR
            return None
        if not outcome.success:
            result.notes = outcome.summary or "runner reported failure"
            result.outcome = RoundOutcome.ERROR
            result.cost_usd = outcome.cost_usd
            return None
        return outcome

    def _dismiss_stale(
        self,
        round_obj: ReviewRound,
        decision: CommentDecision,
        result: RoundResult,
    ) -> None:
        """Reply to a stale comment with an explanation, never re-attempt."""
        body = (
            f"@{decision.comment.reviewer} The line range "
            f"`{decision.comment.path}:{decision.comment.line_start}-"
            f"{decision.comment.line_end}` is no longer present in the diff "
            "after recent edits. Marking this comment as stale; please re-anchor "
            "it to the current code if the concern still applies."
        )
        self.gh.reply_to_comment(
            repo=round_obj.repo,
            pr_number=round_obj.pr_number,
            comment_id=decision.comment.comment_id,
            body=body,
        )
        self.dedup.mark_outcome(
            decision.comment.comment_id,
            outcome=RoundOutcome.DISMISSED_STALE.value,
            round_id=round_obj.round_id,
        )
        result.dismissed.append((decision.comment.comment_id, decision.reason))

    def _dismiss_question(
        self,
        round_obj: ReviewRound,
        decision: CommentDecision,
        result: RoundResult,
    ) -> None:
        """Reply to a discussion-style question with an apology."""
        body = (
            f"@{decision.comment.reviewer} Sorry — this looks like a question "
            "rather than a change request, so the review responder is leaving it "
            "for a human teammate to answer."
        )
        self.gh.reply_to_comment(
            repo=round_obj.repo,
            pr_number=round_obj.pr_number,
            comment_id=decision.comment.comment_id,
            body=body,
        )
        self.dedup.mark_outcome(
            decision.comment.comment_id,
            outcome=RoundOutcome.DISMISSED_QUESTION.value,
            round_id=round_obj.round_id,
        )
        result.dismissed.append((decision.comment.comment_id, decision.reason))

    def _mark_addressed(
        self,
        round_obj: ReviewRound,
        comment: ReviewComment,
        commit_sha: str,
    ) -> None:
        """Try to PATCH the comment as resolved; reply citing the SHA on failure."""
        if self.gh.patch_resolve_comment(repo=round_obj.repo, comment_id=comment.comment_id):
            return
        body = (
            f"Addressed in commit `{commit_sha[:12]}`. "
            "(GitHub did not accept an automated resolve — please dismiss the thread manually.)"
        )
        self.gh.reply_to_comment(
            repo=round_obj.repo,
            pr_number=round_obj.pr_number,
            comment_id=comment.comment_id,
            body=body,
        )

    def _post_round_summary(
        self,
        round_obj: ReviewRound,
        outcome: RunnerOutcome,
        addressables: list[CommentDecision],
    ) -> None:
        """Post the single round-summary comment back to the PR."""
        ids = ", ".join(f"#{d.comment.comment_id}" for d in addressables)
        body = (
            f"Bernstein review responder addressed {len(addressables)} comment(s) "
            f"({ids}) in commit `{outcome.commit_sha[:12]}`.\n\n"
            f"{outcome.summary}\n\n"
            f"_Cost: ${outcome.cost_usd:.4f} (cap ${self.config.per_round_cost_cap_usd:.2f})._"
        )
        self.gh.post_pr_comment(repo=round_obj.repo, pr_number=round_obj.pr_number, body=body)

    def _post_needs_human(self, round_obj: ReviewRound, result: RoundResult) -> None:
        """Post a top-level ``needs-human`` notice when a round cannot land."""
        body = (
            "Bernstein review responder is escalating round "
            f"`{result.round_id}` to a human teammate.\n\n"
            f"Reason: {result.notes or result.outcome.value}.\n"
            f"Cost so far: ${result.cost_usd:.4f}."
        )
        self.gh.post_pr_comment(repo=round_obj.repo, pr_number=round_obj.pr_number, body=body)

    def _finalise(
        self,
        round_obj: ReviewRound,
        result: RoundResult,
        *,
        default_outcome: RoundOutcome,
    ) -> None:
        """Emit metrics, audit entry, and dedup updates for the round."""
        if result.outcome == RoundOutcome.NO_OP and result.dismissed:
            # All comments were dismissed; surface that as a dedicated bucket.
            result.outcome = (
                RoundOutcome.DISMISSED_STALE
                if any(r == "cited line range no longer present in PR diff" for _, r in result.dismissed)
                else RoundOutcome.DISMISSED_QUESTION
            )
        elif result.outcome == RoundOutcome.NO_OP:
            result.outcome = default_outcome

        # Audit must run before we touch metrics so a crash here is surfaced
        # in the chain rather than silently lost.
        self._record_audit(round_obj, result)

        record_round(
            repo=round_obj.repo,
            outcome=result.outcome.value,
            comments_addressed=len(result.addressed),
        )

        for cid in result.addressed:
            self.dedup.mark_outcome(
                cid,
                outcome=result.outcome.value,
                round_id=round_obj.round_id,
            )

    def _record_audit(self, round_obj: ReviewRound, result: RoundResult) -> None:
        """Append an HMAC audit entry summarising the round."""
        details: dict[str, object] = {
            "comments": [c.comment_id for c in round_obj.comments],
            "reviewers": list(round_obj.reviewers),
            "outcome": result.outcome.value,
            "commit_sha": result.commit_sha,
            "cost_usd": round(result.cost_usd, 6),
            "addressed": result.addressed,
            "dismissed": [{"comment_id": cid, "reason": reason} for cid, reason in result.dismissed],
            "adapter": self.config.adapter,
            "notes": result.notes,
            "pr_number": round_obj.pr_number,
            # Embed a short, deterministic goal string so audit replay can
            # reconstruct the operator-visible intent without re-hydrating
            # the full prompt.
            "goal": f"Address {len(round_obj.comments)} review comment(s) on PR #{round_obj.pr_number}",
        }
        self.audit.log(
            event_type="review_responder.round",
            actor="review_responder",
            resource_type="pull_request",
            resource_id=f"{round_obj.repo}#{round_obj.pr_number}/{round_obj.round_id}",
            details=details,
        )


def build_always_allow_gate(engine: AlwaysAllowEngine) -> GateConsult:
    """Build a :data:`GateConsult` that consults the project's always-allow rules.

    The gate evaluates a synthetic ``"review_responder.commit"`` tool name
    against the supplied engine.  An ALLOW match permits the commit; any
    other result blocks it (defensive default — operators must opt-in).

    Args:
        engine: Already-loaded :class:`AlwaysAllowEngine`.

    Returns:
        A :data:`GateConsult` callable suitable for
        :class:`ReviewResponder.gate_consult`.
    """

    def _consult(round_obj: ReviewRound, _outcome: RunnerOutcome) -> GateAdvice:
        path_descriptor = ",".join(sorted({c.path for c in round_obj.comments}))
        match = engine.match(
            tool_name="review_responder.commit",
            input_value=path_descriptor or "<no-files>",
            input_field="path",
        )
        if match.matched:
            return GateAdvice(allowed=True, reason=match.reason)
        return GateAdvice(
            allowed=False,
            reason=match.reason or "no always-allow rule matched review_responder.commit",
        )

    return _consult
