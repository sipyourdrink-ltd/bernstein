"""Data-driven classifier for HTTP 429 responses.

A 429 status alone does not tell the caller whether the limit is a
transient request-rate cap (clears with time) or a standing account /
key / session / spend cap (will not clear within the run). This module
classifies a 429 response body into one of the two kinds so the backoff
decision can branch before :class:`~bernstein.adapters.base.RateLimitMeter`
advises a retry.

The pattern table is data-driven: each entry is a ``(needle,
classification, reason_label)`` tuple and the classifier is a single
loop over it. Adding a new endpoint's wording is a one-line append; no
control flow changes.

An unrecognised body falls back to :data:`HTTP429Classification.RETRYABLE`
— misclassifying an unknown limit as permanent would be worse than the
current state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final


class HTTP429Classification(Enum):
    """Two-way split of a 429 response before the backoff decision.

    Attributes:
        RETRYABLE: Request-rate limit; clears with time.
        STANDING: Account, key, session, or spend cap; will not clear
            within the run.
    """

    RETRYABLE = "retryable"
    STANDING = "standing"


@dataclass(frozen=True, slots=True)
class _PatternRule:
    """One data-driven classification rule.

    Attributes:
        needle: Case-insensitive substring to match against the body.
        classification: Result when the needle matches.
        reason_label: Short machine-readable reason (e.g. ``session_cap``).
        exhaustion_pattern: Stronger match when the input may include log
            context outside the provider's response.
    """

    needle: str
    classification: HTTP429Classification
    reason_label: str
    exhaustion_pattern: str | None = None


_SPEND_EXHAUSTION = (
    r"(?:spend(?:ing)?|daily) limit (?:of [^\n]+? )?(?:has been |was |is )?(?:reached|exceeded|exhausted)\b|"
    r"\b(?:hit|reached|exceeded) (?:the |your |its )?(?:daily |(?:daily )?spend(?:ing)? )limit\b"
)


#: Data-driven pattern table. Append a new endpoint's wording here; the
#: classifier loop needs no changes.
_PATTERN_RULES: Final[tuple[_PatternRule, ...]] = (
    _PatternRule(
        "maximum number of active sessions",
        HTTP429Classification.STANDING,
        "session_cap",
    ),
    _PatternRule(
        "close unused sessions",
        HTTP429Classification.STANDING,
        "session_cap",
    ),
    _PatternRule("spending limit", HTTP429Classification.STANDING, "spend_cap", _SPEND_EXHAUSTION),
    _PatternRule("spend limit", HTTP429Classification.STANDING, "spend_cap", _SPEND_EXHAUSTION),
    _PatternRule("daily limit", HTTP429Classification.STANDING, "spend_cap", _SPEND_EXHAUSTION),
    _PatternRule("budget exceeded", HTTP429Classification.STANDING, "spend_cap"),
    _PatternRule("insufficient_quota", HTTP429Classification.STANDING, "quota_exceeded"),
    _PatternRule("quota exceeded", HTTP429Classification.STANDING, "quota_exceeded"),
    _PatternRule(
        "credit balance",
        HTTP429Classification.STANDING,
        "credit_cap",
        r"insufficient credit balance|(?:out of|exhausted) (?:your )?credit balance|"
        r"credit balance (?:is |has been )?(?:zero|too low|exhausted|\$?0(?:\.0+)?)(?:\W|$)",
    ),
)


def classify_429(
    body: str, retry_after: str | None = None, *, require_explicit_cap: bool = False
) -> HTTP429Classification:
    """Classify a 429 response body into RETRYABLE or STANDING.

    Args:
        body: The response body text. Matched case-insensitively against
            :data:`_PATTERN_RULES`.
        retry_after: Optional ``Retry-After`` header value. Reserved for
            future standing-cap heuristics; classification is currently
            body-driven.
        require_explicit_cap: Require exhaustion wording for ambiguous spend
            and balance phrases when scanning surrounding log lines.

    Returns:
        :data:`HTTP429Classification.RETRYABLE` for unknown or
        rate-limit-shaped bodies, :data:`HTTP429Classification.STANDING`
        for session-cap and spend-cap bodies.
    """

    lowered = body.lower()
    for rule in _PATTERN_RULES:
        if rule.needle not in lowered:
            continue
        if require_explicit_cap and rule.exhaustion_pattern and not re.search(rule.exhaustion_pattern, lowered):
            continue
        return rule.classification
    return HTTP429Classification.RETRYABLE
