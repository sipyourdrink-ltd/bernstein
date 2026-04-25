"""PR review responder — react to inline review comments on Bernstein PRs.

The responder ingests GitHub pull-request review-comment events through one
of two listeners:

* :class:`WebhookListener` — verifies an ``X-Hub-Signature-256`` signature
  and queues normalised events for the bundler.  The transport (e.g.
  ``cloudflared``) is supplied by the v1.8.15 tunnel wrapper and is not
  modified here.
* :class:`PollingListener` — falls back to the GitHub REST API via
  ``gh api`` when no tunnel is available.  Same normaliser, same queue.

Each unresolved inline comment becomes a Bernstein task whose prompt
embeds the file path, line range, comment body, and reviewer username.
Comments arriving inside a configurable quiet window collapse into a
single round, producing exactly one commit and one summary reply.

The responder consults the always-allow gate before committing,
respects a per-round cost cap reusing the cost tracker, and writes
HMAC-chained audit entries for every round.  Auto-merge is never
triggered.
"""

from __future__ import annotations

from bernstein.core.review_responder.bundling import RoundBundler
from bernstein.core.review_responder.dedup import DedupQueue
from bernstein.core.review_responder.metrics import (
    review_responder_comments_addressed_total,
    review_responder_rounds_total,
)
from bernstein.core.review_responder.models import (
    ResponderConfig,
    ReviewComment,
    ReviewRound,
    RoundOutcome,
)
from bernstein.core.review_responder.normaliser import (
    EventParseError,
    normalise_polling_payload,
    normalise_webhook_payload,
)
from bernstein.core.review_responder.polling import PollingListener
from bernstein.core.review_responder.responder import ReviewResponder
from bernstein.core.review_responder.webhook import (
    WebhookListener,
    verify_signature,
)

__all__ = [
    "DedupQueue",
    "EventParseError",
    "PollingListener",
    "ResponderConfig",
    "ReviewComment",
    "ReviewResponder",
    "ReviewRound",
    "RoundBundler",
    "RoundOutcome",
    "WebhookListener",
    "normalise_polling_payload",
    "normalise_webhook_payload",
    "review_responder_comments_addressed_total",
    "review_responder_rounds_total",
    "verify_signature",
]
