"""Dataclasses shared across the review responder modules.

Keeping the typed domain in one file lets the listeners, bundler, and
responder talk to each other without circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class RoundOutcome(str, Enum):  # noqa: UP042 - StrEnum forces str-cmp wins; we want explicit dual-base for label exports
    """Outcome label attached to a completed (or aborted) review round.

    Values are kept stable because they appear as a Prometheus label.
    """

    COMMITTED = "committed"
    NEEDS_HUMAN = "needs_human"
    NO_OP = "no_op"
    DISMISSED_STALE = "dismissed_stale"
    DISMISSED_QUESTION = "dismissed_question"
    COST_CAP_BREACHED = "cost_cap_breached"
    ERROR = "error"


@dataclass(frozen=True)
class ReviewComment:
    """Normalised inline review comment from GitHub.

    The shape is deliberately minimal — we only carry what the responder
    actually needs (path, line range, body, reviewer, timestamps, ids).

    Attributes:
        comment_id: GitHub PR review-comment id (stable per comment).
        pr_number: Pull-request number this comment belongs to.
        repo: ``owner/repo`` slug for the host repository.
        reviewer: GitHub login of the comment author.
        body: Plain-text comment body (markdown stripped at rendering time).
        path: Repository-relative file path the comment is anchored to.
        line_start: First line in the cited range (1-based, inclusive).
        line_end: Last line in the cited range (1-based, inclusive).
        commit_id: SHA the comment is anchored to.
        original_commit_id: SHA the comment was originally posted against.
        diff_hunk: Diff hunk snippet attached by GitHub, may be empty.
        created_at: ISO 8601 timestamp from GitHub.
        updated_at: ISO 8601 timestamp from GitHub.
        in_reply_to: ``comment_id`` of the parent comment, when threaded.
    """

    comment_id: int
    pr_number: int
    repo: str
    reviewer: str
    body: str
    path: str
    line_start: int
    line_end: int
    commit_id: str
    original_commit_id: str
    diff_hunk: str
    created_at: str
    updated_at: str
    in_reply_to: int | None = None

    @property
    def dedup_key(self) -> tuple[int, str]:
        """Return the key used by :class:`DedupQueue` to suppress replays."""
        return (self.comment_id, self.updated_at)


@dataclass(frozen=True)
class ReviewRound:
    """A bundle of comments collected inside a single quiet window.

    Attributes:
        round_id: Stable id derived from PR + window-start timestamp.
        repo: Repository slug.
        pr_number: PR number all comments share.
        comments: Ordered list of :class:`ReviewComment` belonging to the round.
        opened_at: Unix timestamp of the first comment in the bundle.
        sealed_at: Unix timestamp the bundler closed the bundle on.
    """

    round_id: str
    repo: str
    pr_number: int
    comments: tuple[ReviewComment, ...]
    opened_at: float
    sealed_at: float

    @property
    def reviewers(self) -> tuple[str, ...]:
        """Distinct reviewer usernames present in the bundle, in arrival order."""
        seen: list[str] = []
        for c in self.comments:
            if c.reviewer not in seen:
                seen.append(c.reviewer)
        return tuple(seen)


@dataclass(frozen=True)
class ResponderConfig:
    """Tunables for :class:`ReviewResponder`.

    Attributes:
        repo: Default ``owner/repo`` to operate on.
        quiet_window_s: Seconds of silence before a round is sealed.
        per_round_cost_cap_usd: Hard cost ceiling for one round; breach
            posts a ``needs-human`` reply and aborts.
        adapter: Adapter name used to label the dispatched task.
        webhook_secret_env: Env var name carrying the GitHub webhook secret.
        polling_interval_s: How often :class:`PollingListener` polls when
            no tunnel is active.
        question_markers: Substrings whose presence flags a comment as a
            discussion question (responder skips with an apology reply).
        listen_host: Bind host for :class:`WebhookListener`.
        listen_port: Bind port for :class:`WebhookListener` (the tunnel
            forwards public traffic to this port).
        max_comments_per_round: Safety cap; rounds larger than this split
            into ``ceil(N/cap)`` follow-up rounds.
    """

    repo: str
    quiet_window_s: float = 90.0
    per_round_cost_cap_usd: float = 2.50
    adapter: Literal["claude", "codex", "gemini", "aider", "generic"] = "claude"
    webhook_secret_env: str = "BERNSTEIN_REVIEW_WEBHOOK_SECRET"
    polling_interval_s: float = 60.0
    question_markers: tuple[str, ...] = (
        "can you explain",
        "could you explain",
        "why did you",
        "why does",
        "what does",
        "how does this",
        "could you clarify",
        "can you clarify",
    )
    listen_host: str = "127.0.0.1"
    listen_port: int = 8053
    max_comments_per_round: int = 25


@dataclass(frozen=True)
class CommentDecision:
    """Per-comment routing decision computed before a round is dispatched.

    Attributes:
        comment: The original comment.
        action: ``"address"`` to dispatch, ``"dismiss_stale"`` for stale
            line references, ``"dismiss_question"`` for discussion-style
            comments the responder should not act on.
        reason: Human-readable reason recorded in the audit entry.
    """

    comment: ReviewComment
    action: Literal["address", "dismiss_stale", "dismiss_question"]
    reason: str = ""


@dataclass
class RoundResult:
    """Mutable record of what happened to a round.

    Updated in place by :class:`ReviewResponder` so the audit/metrics
    stages see a single coherent view.

    Attributes:
        round_id: Round identifier.
        outcome: Final :class:`RoundOutcome`.
        commit_sha: SHA produced by the round, when ``outcome`` is
            :attr:`RoundOutcome.COMMITTED`.
        cost_usd: Cumulative spend on the round.
        addressed: List of comment ids the responder replied to or
            silently committed against.
        dismissed: List of ``(comment_id, reason)`` for skipped comments.
        notes: Free-form trace string surfaced in audit entry.
    """

    round_id: str
    outcome: RoundOutcome = RoundOutcome.NO_OP
    commit_sha: str = ""
    cost_usd: float = 0.0
    addressed: list[int] = field(default_factory=list)
    dismissed: list[tuple[int, str]] = field(default_factory=list)
    notes: str = ""
