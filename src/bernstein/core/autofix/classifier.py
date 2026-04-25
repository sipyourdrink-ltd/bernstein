"""Failure classifier — maps a log blob to a routing bucket.

The autofix daemon dispatches to one of three bandit arms depending
on the *kind* of failure observed:

* ``security`` failures (CodeQL alerts, leaked secrets, vulnerable
  dependencies) need the most capable model — they are routed to
  ``opus``.
* ``flaky`` failures (intermittent tests, timeouts, deadlocks) get
  ``sonnet``: the model that is good enough to fix unstable tests
  without overspending.
* ``config`` failures (lint, formatter, missing config keys, broken
  YAML) get ``haiku`` — these are mechanical and cheap.

The classifier is deliberately deterministic: it scans the failing
log for keyword sets and picks the *highest* priority bucket that
matches, so a security signal always wins over a flaky one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

#: Three classification buckets, ordered from most to least urgent.
ClassificationKind = Literal["security", "flaky", "config"]


# ---------------------------------------------------------------------------
# Keyword sets
# ---------------------------------------------------------------------------

_SECURITY_PATTERNS: Final[tuple[str, ...]] = (
    r"\bcodeql\b",
    r"\bvulnerability\b",
    r"\bvulnerab",
    r"\bcve-\d{4}-\d{4,7}\b",
    r"\bsecret\s+detected\b",
    r"\bleaked\s+secret\b",
    r"\bsecret\s+scanning\b",
    r"\bsecurity\s+alert\b",
    r"\bdependabot\s+alert\b",
    r"\bpotential\s+sql\s+injection\b",
    r"\bcross-site\s+scripting\b",
    r"\binsecure\s+deserial",
)

_FLAKY_PATTERNS: Final[tuple[str, ...]] = (
    r"\bflaky\b",
    r"\bintermittent\b",
    r"\btimed\s+out\b",
    r"\btimeout\b",
    r"\btime[d-]\s*out\b",
    r"\bdeadlock\b",
    r"\bconnection\s+reset\b",
    r"\b502\s+bad\s+gateway\b",
    r"\b503\s+service\s+unavailable\b",
    r"\b504\s+gateway\s+timeout\b",
    r"\bsocket\s+hang\s+up\b",
    r"\beconnreset\b",
    r"\bnetworkerror\b",
    r"\brate\s+limit\b",
    r"\brate-?limit",
    r"\brace\s+condition\b",
)

_CONFIG_PATTERNS: Final[tuple[str, ...]] = (
    r"\bsyntaxerror\b",
    r"\bparse\s+error\b",
    r"\binvalid\s+yaml\b",
    r"\binvalid\s+toml\b",
    r"\binvalid\s+json\b",
    r"\bmissing\s+(?:config|configuration|setting|key|env)\b",
    r"\benvironment\s+variable\s+\w+\s+(?:is\s+)?(?:not\s+set|missing)\b",
    r"\beslint\b",
    r"\bruff\b",
    r"\bblack\b",
    r"\bprettier\b",
    r"\bflake8\b",
    r"\bmypy\b",
    r"\btypecheck\b",
    r"\btype\s+check\b",
    r"\bpyright\b",
    r"\bunknown\s+option\b",
    r"\bbad\s+config\b",
    r"\bcommand\s+not\s+found\b",
    r"\bmodule\s+not\s+found\b",
    r"\bmodulenotfounderror\b",
    r"\bimport\s*error\b",
)


# ---------------------------------------------------------------------------
# Bandit-arm map
# ---------------------------------------------------------------------------

#: Routing decisions per classification.  These map directly onto the
#: ``BanditRouter._DEFAULT_ARMS`` arm names (haiku/sonnet/opus) so the
#: dispatcher can hand them straight through to the bandit policy.
_ARM_FOR_KIND: Final[dict[ClassificationKind, str]] = {
    "security": "opus",
    "flaky": "sonnet",
    "config": "haiku",
}


@dataclass(frozen=True)
class Classification:
    """The result of classifying a failing-log blob.

    Attributes:
        kind: The classification bucket — ``security`` (regression in
            an auth/crypto/dependency path), ``flaky`` (intermittent
            test/timeout) or ``config`` (lint, formatter, missing
            setting).
        model: The bandit arm to route the dispatch to (one of
            ``opus``, ``sonnet``, ``haiku``).
        matched_signals: A tuple of human-readable signal names that
            triggered the classification — recorded in the audit
            trail so operators can audit why a route was chosen.
    """

    kind: ClassificationKind
    model: str
    matched_signals: tuple[str, ...]


def _matches(text: str, patterns: tuple[str, ...]) -> tuple[str, ...]:
    """Return the subset of ``patterns`` that fire against ``text``."""
    found: list[str] = []
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            found.append(pattern)
    return tuple(found)


def classify_failure(log: str) -> Classification:
    """Classify a failing-log blob into a routing bucket.

    Args:
        log: The (possibly truncated) failing-job log extracted from
            ``gh run view --log-failed``.  May be empty.

    Returns:
        A :class:`Classification` describing the bucket, the bandit
        arm to route to, and the list of patterns that matched.  When
        no pattern matches the failure is treated as ``config`` (the
        cheapest arm) so the daemon never escalates an unknown
        failure to ``opus`` by accident.
    """
    text = log if isinstance(log, str) else ""

    # Walk the buckets in priority order — the *first* hit wins.
    for kind, patterns in (
        ("security", _SECURITY_PATTERNS),
        ("flaky", _FLAKY_PATTERNS),
        ("config", _CONFIG_PATTERNS),
    ):
        signals = _matches(text, patterns)
        if signals:
            kind_typed: ClassificationKind = kind  # type: ignore[assignment]
            return Classification(
                kind=kind_typed,
                model=_ARM_FOR_KIND[kind_typed],
                matched_signals=signals,
            )

    # Default fallback — treat as cheap "config" failure rather than
    # escalating to opus on an unknown payload.
    return Classification(
        kind="config",
        model=_ARM_FOR_KIND["config"],
        matched_signals=("default-config-fallback",),
    )
