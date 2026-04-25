"""Prometheus counters for the review responder.

Registered against the same dedicated registry used by the rest of the
project so ``/metrics`` exposes them automatically.  Stub fallbacks
inherit from the shared ``prometheus_client`` shim, which means tests
running without the real client (e.g. constrained CI) can still call
``.labels(...).inc()`` safely.
"""

from __future__ import annotations

from bernstein.core.observability.prometheus import Counter, registry

review_responder_rounds_total: Counter = Counter(
    "review_responder_rounds_total",
    "Review responder rounds completed, partitioned by repo and outcome.",
    labelnames=["repo", "outcome"],
    registry=registry,
)

review_responder_comments_addressed_total: Counter = Counter(
    "review_responder_comments_addressed_total",
    "Review comments the responder addressed (committed or replied), per repo.",
    labelnames=["repo"],
    registry=registry,
)


def record_round(repo: str, outcome: str, comments_addressed: int) -> None:
    """Increment both counters in one call.

    Args:
        repo: Repository slug used as a Prometheus label.
        outcome: One of the :class:`RoundOutcome` string values.
        comments_addressed: Number of comments the round resolved; the
            comments-addressed counter is incremented by this value.
    """
    review_responder_rounds_total.labels(repo=repo, outcome=outcome).inc()
    if comments_addressed > 0:
        review_responder_comments_addressed_total.labels(repo=repo).inc(comments_addressed)
