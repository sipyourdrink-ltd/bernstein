"""Structured failure taxonomy for tracker comment write-back.

When an agent run fails on a tracker ticket, the orchestrator posts a
free-text comment summarising the failure. Free text is not parseable by
downstream automation (auto-escalation, retry-with-continuation, dead
letter pipelines).

This module produces a machine-readable failure summary that ships
inside a fenced YAML block alongside the human-readable preamble. The
YAML schema is intentionally small and stable so downstream consumers
can rely on it:

    reason_code:    closed-set code (see ``FAILURE_REASON_CODES``)
    category:       taxonomy category from :class:`FailureCategory`
    transient:      bool, true when a retry is likely to recover
    next_action:    short hint for the auto-escalator or operator
    evidence_path:  relative path to a trace/log file, or ``""``

The :class:`FailureCategory` enum is re-exported from
:mod:`bernstein.eval.taxonomy` so the eval harness and the tracker
pipeline classify into the same closed set. Adding a new category
remains a single-source change.

The exception classifier is heuristic by design. It pattern-matches on
exception types and on substrings of the rendered message; callers may
override the classification by passing explicit hints (``timed_out``,
``rate_limited``, etc.) when the run loop already knows the cause.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

import yaml

from bernstein.eval.taxonomy import FailureCategory

__all__ = [
    "FAILURE_REASON_CODES",
    "FAILURE_YAML_FENCE",
    "FailureCategory",
    "FailureClassification",
    "FailureTaxonomyPayload",
    "FailureTaxonomyWriter",
    "classify_failure",
    "render_failure_comment",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FAILURE_YAML_FENCE: Final[str] = "bernstein-failure-v1"
"""Fenced-code-block info-string for downstream parsers.

The fence is deliberately versioned. A future schema bump moves to
``bernstein-failure-v2`` and downstream parsers can ignore older
versions or upgrade selectively.
"""

FAILURE_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "test_regression",
        "timeout",
        "rate_limit",
        "network_error",
        "sandbox_violation",
        "missing_dependency",
        "type_error",
        "syntax_error",
        "flaky_test",
        "scope_violation",
        "merge_conflict",
        "compile_error",
        "context_miss",
        "standing_cap",
        "unknown",
    }
)
"""Closed set of machine-readable reason codes.

Reason codes are finer-grained than :class:`FailureCategory` so the
auto-escalator can branch on, e.g., ``rate_limit`` versus
``network_error`` even when both fall under the broader ``TIMEOUT``
category. Adding a new code is a single-line change here; downstream
consumers MUST treat unknown codes as ``unknown``.
"""

# Reason codes that should be retried before paging an operator. These
# are advisory only; the auto-escalator owns the final retry policy.
_TRANSIENT_REASON_CODES: Final[frozenset[str]] = frozenset(
    {
        "rate_limit",
        "network_error",
        "flaky_test",
        "timeout",
    }
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureClassification:
    """Result of classifying a single failure event.

    Attributes:
        reason_code: One of :data:`FAILURE_REASON_CODES`.
        category: Mapped :class:`FailureCategory` value.
        transient: True when retry is likely to recover.
        confidence: Heuristic confidence score in ``[0.0, 1.0]``.
        summary: Short human-readable explanation (one sentence).
    """

    reason_code: str
    category: FailureCategory
    transient: bool
    confidence: float
    summary: str

    def __post_init__(self) -> None:
        if self.reason_code not in FAILURE_REASON_CODES:
            msg = f"reason_code {self.reason_code!r} not in FAILURE_REASON_CODES"
            raise ValueError(msg)
        if not 0.0 <= self.confidence <= 1.0:
            msg = f"confidence {self.confidence!r} outside [0.0, 1.0]"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class FailureTaxonomyPayload:
    """Serialised failure summary written to a tracker comment.

    Attributes:
        reason_code: Closed-set machine-readable code.
        category: Broader taxonomy bucket.
        transient: Hint for the auto-escalator.
        next_action: Short imperative (e.g. ``"retry"``, ``"escalate"``).
        evidence_path: Relative path to a log or trace file. May be
            empty when no evidence file was captured.
    """

    reason_code: str
    category: FailureCategory
    transient: bool
    next_action: str
    evidence_path: str = ""

    def to_mapping(self) -> dict[str, object]:
        """Return a YAML-serialisable mapping.

        ``category`` is rendered as the enum ``value`` (string) so the
        payload round-trips through ``yaml.safe_load`` without custom
        constructors.
        """

        return {
            "reason_code": self.reason_code,
            "category": self.category.value,
            "transient": self.transient,
            "next_action": self.next_action,
            "evidence_path": self.evidence_path,
        }


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


# Mapping from exception type names to (reason_code, category, confidence).
# Lookups are by ``type(exc).__name__`` so adapters can raise vendor-specific
# exception classes without importing them here.
_EXCEPTION_TYPE_RULES: Final[dict[str, tuple[str, FailureCategory, float]]] = {
    "TimeoutError": ("timeout", FailureCategory.TIMEOUT, 0.85),
    "asyncio.TimeoutError": ("timeout", FailureCategory.TIMEOUT, 0.85),
    "ConnectionError": ("network_error", FailureCategory.TIMEOUT, 0.7),
    "ConnectionResetError": ("network_error", FailureCategory.TIMEOUT, 0.8),
    "ConnectionRefusedError": ("network_error", FailureCategory.TIMEOUT, 0.8),
    "RateLimited": ("rate_limit", FailureCategory.TIMEOUT, 0.95),
    "StandingCapError": ("standing_cap", FailureCategory.CONTEXT_MISS, 0.9),
    "PermissionError": ("sandbox_violation", FailureCategory.SCOPE_CREEP, 0.7),
    "ModuleNotFoundError": ("missing_dependency", FailureCategory.HALLUCINATION, 0.9),
    "ImportError": ("missing_dependency", FailureCategory.HALLUCINATION, 0.75),
    "TypeError": ("type_error", FailureCategory.HALLUCINATION, 0.6),
    "AttributeError": ("type_error", FailureCategory.HALLUCINATION, 0.6),
    "SyntaxError": ("syntax_error", FailureCategory.HALLUCINATION, 0.95),
    "IndentationError": ("syntax_error", FailureCategory.HALLUCINATION, 0.95),
}

# Message-substring rules run after type rules and only when the type
# rule did not fire or fired with low confidence. Each substring is
# lowercased before comparison.
_MESSAGE_SUBSTRING_RULES: Final[tuple[tuple[str, str, FailureCategory, float], ...]] = (
    ("rate limit", "rate_limit", FailureCategory.TIMEOUT, 0.9),
    ("429", "rate_limit", FailureCategory.TIMEOUT, 0.85),
    ("timed out", "timeout", FailureCategory.TIMEOUT, 0.8),
    ("timeout", "timeout", FailureCategory.TIMEOUT, 0.75),
    ("connection reset", "network_error", FailureCategory.TIMEOUT, 0.85),
    ("connection refused", "network_error", FailureCategory.TIMEOUT, 0.85),
    ("dns", "network_error", FailureCategory.TIMEOUT, 0.6),
    ("sandbox", "sandbox_violation", FailureCategory.SCOPE_CREEP, 0.75),
    ("permission denied", "sandbox_violation", FailureCategory.SCOPE_CREEP, 0.7),
    ("scope_violation", "scope_violation", FailureCategory.SCOPE_CREEP, 0.95),
    ("merge conflict", "merge_conflict", FailureCategory.CONFLICT, 0.9),
    ("conflict marker", "merge_conflict", FailureCategory.CONFLICT, 0.85),
    ("flaky", "flaky_test", FailureCategory.TEST_REGRESSION, 0.7),
    ("test regression", "test_regression", FailureCategory.TEST_REGRESSION, 0.9),
    ("failed:", "test_regression", FailureCategory.TEST_REGRESSION, 0.5),
    ("no module named", "missing_dependency", FailureCategory.HALLUCINATION, 0.9),
    ("syntaxerror", "syntax_error", FailureCategory.HALLUCINATION, 0.85),
    ("compile error", "compile_error", FailureCategory.HALLUCINATION, 0.8),
    ("active sessions", "standing_cap", FailureCategory.CONTEXT_MISS, 0.8),
    ("session cap", "standing_cap", FailureCategory.CONTEXT_MISS, 0.8),
    ("spending limit", "standing_cap", FailureCategory.CONTEXT_MISS, 0.8),
    ("daily limit", "standing_cap", FailureCategory.CONTEXT_MISS, 0.8),
    ("budget exceeded", "standing_cap", FailureCategory.CONTEXT_MISS, 0.8),
    ("concurrency limit", "standing_cap", FailureCategory.CONTEXT_MISS, 0.8),
)


def classify_failure(
    exc: Exception | str,
    context: dict[str, object] | None = None,
) -> FailureClassification:
    """Classify a failure event into the taxonomy.

    The classifier is intentionally heuristic. It is meant to produce a
    structured summary in the common case so the auto-escalator can
    branch; ambiguous cases fall back to ``reason_code = "unknown"`` with
    low confidence.

    Resolution order:

    1. ``context["reason_code"]`` (explicit override from the caller).
    2. Context hint flags (``timed_out``, ``rate_limited``,
       ``tests_regressed``, ``scope_violated``).
    3. Exception type match against :data:`_EXCEPTION_TYPE_RULES`.
    4. Message substring match against :data:`_MESSAGE_SUBSTRING_RULES`.
    5. Fallback: ``("unknown", CONTEXT_MISS, 0.2)``.

    Args:
        exc: Either the raised exception or a pre-rendered error string
            (e.g. captured from an adapter's stderr).
        context: Optional caller-provided hints. Recognised keys:
            ``reason_code``, ``timed_out``, ``rate_limited``,
            ``tests_regressed``, ``scope_violated``, ``summary``.

    Returns:
        A :class:`FailureClassification`.
    """

    ctx = context or {}

    # 1. Explicit override.
    forced_code = ctx.get("reason_code")
    if isinstance(forced_code, str) and forced_code in FAILURE_REASON_CODES:
        category = _category_for_reason_code(forced_code)
        summary = _coerce_summary(ctx.get("summary"), exc, forced_code)
        return FailureClassification(
            reason_code=forced_code,
            category=category,
            transient=forced_code in _TRANSIENT_REASON_CODES,
            confidence=1.0,
            summary=summary,
        )

    # 2. Context hints from the run loop.
    hint_result = _classify_from_hints(ctx, exc)
    if hint_result is not None:
        return hint_result

    # 3. Exception type.
    type_result = _classify_from_exception_type(exc, ctx)
    if type_result is not None:
        return type_result

    # 4. Message substring.
    message = _rendered_message(exc)
    substring_result = _classify_from_message(message, ctx)
    if substring_result is not None:
        return substring_result

    # 5. Fallback.
    return FailureClassification(
        reason_code="unknown",
        category=FailureCategory.CONTEXT_MISS,
        transient=False,
        confidence=0.2,
        summary=_coerce_summary(ctx.get("summary"), exc, "unknown"),
    )


def _classify_from_hints(
    ctx: dict[str, object],
    exc: Exception | str,
) -> FailureClassification | None:
    """Map caller-provided hint flags to a classification, if any."""

    hint_table: tuple[tuple[str, str, FailureCategory], ...] = (
        ("tests_regressed", "test_regression", FailureCategory.TEST_REGRESSION),
        ("rate_limited", "rate_limit", FailureCategory.TIMEOUT),
        ("timed_out", "timeout", FailureCategory.TIMEOUT),
        ("scope_violated", "scope_violation", FailureCategory.SCOPE_CREEP),
        ("sandbox_violated", "sandbox_violation", FailureCategory.SCOPE_CREEP),
        ("conflict_detected", "merge_conflict", FailureCategory.CONFLICT),
    )
    for hint_key, reason_code, category in hint_table:
        if bool(ctx.get(hint_key)):
            return FailureClassification(
                reason_code=reason_code,
                category=category,
                transient=reason_code in _TRANSIENT_REASON_CODES,
                confidence=0.95,
                summary=_coerce_summary(ctx.get("summary"), exc, reason_code),
            )
    return None


def _classify_from_exception_type(
    exc: Exception | str,
    ctx: dict[str, object],
) -> FailureClassification | None:
    """Look up the exception's class name in the type-rule table."""

    if not isinstance(exc, BaseException):
        return None
    type_name = type(exc).__name__
    rule = _EXCEPTION_TYPE_RULES.get(type_name)
    if rule is None:
        return None
    reason_code, category, confidence = rule
    return FailureClassification(
        reason_code=reason_code,
        category=category,
        transient=reason_code in _TRANSIENT_REASON_CODES,
        confidence=confidence,
        summary=_coerce_summary(ctx.get("summary"), exc, reason_code),
    )


def _classify_from_message(
    message: str,
    ctx: dict[str, object],
) -> FailureClassification | None:
    """Scan the message for known substrings, return the first match."""

    lowered = message.lower()
    for needle, reason_code, category, confidence in _MESSAGE_SUBSTRING_RULES:
        if needle in lowered:
            return FailureClassification(
                reason_code=reason_code,
                category=category,
                transient=reason_code in _TRANSIENT_REASON_CODES,
                confidence=confidence,
                summary=_coerce_summary(ctx.get("summary"), message, reason_code),
            )
    return None


def _rendered_message(exc: Exception | str) -> str:
    """Return the string used for substring matching."""

    if isinstance(exc, BaseException):
        return str(exc)
    return exc


def _coerce_summary(
    explicit: object,
    exc: Exception | str,
    reason_code: str,
) -> str:
    """Pick the best one-line summary for the classification.

    The explicit summary wins. Otherwise the rendered exception/string
    is truncated to a single line, capped at 240 chars. As a last
    resort the reason code is wrapped in a generic message so downstream
    consumers never see an empty ``summary``.
    """

    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().splitlines()[0][:240]
    rendered = _rendered_message(exc).strip()
    if rendered:
        return rendered.splitlines()[0][:240]
    return f"agent run failed with reason_code={reason_code}"


def _category_for_reason_code(reason_code: str) -> FailureCategory:
    """Map an explicit reason code to its broader category."""

    # Defer to the type-rule and substring-rule tables to avoid a third
    # source of truth. First match wins; reason codes not in any rule
    # collapse to ``CONTEXT_MISS``.
    for _rc, category, _conf in _EXCEPTION_TYPE_RULES.values():
        if _rc == reason_code:
            return category
    for _needle, _rc, category, _conf in _MESSAGE_SUBSTRING_RULES:
        if _rc == reason_code:
            return category
    # Hint-only codes (e.g. ``scope_violation``) have no entry in the
    # type table; fall back to a small explicit map.
    fallback_map: dict[str, FailureCategory] = {
        "test_regression": FailureCategory.TEST_REGRESSION,
        "scope_violation": FailureCategory.SCOPE_CREEP,
        "sandbox_violation": FailureCategory.SCOPE_CREEP,
        "merge_conflict": FailureCategory.CONFLICT,
        "context_miss": FailureCategory.CONTEXT_MISS,
        "standing_cap": FailureCategory.CONTEXT_MISS,
        "unknown": FailureCategory.CONTEXT_MISS,
    }
    return fallback_map.get(reason_code, FailureCategory.CONTEXT_MISS)


# ---------------------------------------------------------------------------
# Writer / renderer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureTaxonomyWriter:
    """Render a failure classification into a tracker comment body.

    The comment body has three sections in fixed order:

    1. A human-readable preamble (one short paragraph).
    2. A fenced YAML block (info-string :data:`FAILURE_YAML_FENCE`)
       containing the :class:`FailureTaxonomyPayload`.
    3. An optional triple-backticked traceback block.

    The renderer is deterministic; identical inputs produce
    byte-identical output, which keeps the audit log idempotent.
    """

    next_action: str = "retry"
    """Default next-action when the caller does not provide one.

    ``"retry"`` is safe for transient failures; the auto-escalator
    overrides it when ``transient`` is false.
    """

    preamble_prefix: str = "Bernstein agent run failed."
    """Leading sentence of the preamble. Kept short for tracker UIs."""

    def build_payload(
        self,
        classification: FailureClassification,
        *,
        next_action: str | None = None,
        evidence_path: str = "",
    ) -> FailureTaxonomyPayload:
        """Assemble the payload from a classification.

        ``next_action`` defaults to ``"retry"`` for transient failures
        and ``"escalate"`` for non-transient ones unless the caller
        provides an explicit value.
        """

        chosen_action: str
        if next_action is not None:
            chosen_action = next_action
        elif classification.transient:
            chosen_action = self.next_action
        else:
            chosen_action = "escalate"
        return FailureTaxonomyPayload(
            reason_code=classification.reason_code,
            category=classification.category,
            transient=classification.transient,
            next_action=chosen_action,
            evidence_path=evidence_path,
        )

    def render_yaml_block(self, payload: FailureTaxonomyPayload) -> str:
        """Return the fenced YAML block as a string."""

        body = yaml.safe_dump(
            payload.to_mapping(),
            sort_keys=False,
            default_flow_style=False,
        ).rstrip()
        return f"```{FAILURE_YAML_FENCE}\n{body}\n```"

    def render_comment(
        self,
        classification: FailureClassification,
        *,
        next_action: str | None = None,
        evidence_path: str = "",
        traceback_text: str | None = None,
    ) -> str:
        """Render the full comment body.

        Args:
            classification: Result of :func:`classify_failure`.
            next_action: Override the default ``next_action``.
            evidence_path: Relative path to evidence (log/trace file).
            traceback_text: Optional traceback to append in a separate
                fenced block.

        Returns:
            The full comment body, ready to pass to
            :meth:`AbstractTrackerAdapter.add_comment`.
        """

        payload = self.build_payload(
            classification,
            next_action=next_action,
            evidence_path=evidence_path,
        )
        preamble = (
            f"{self.preamble_prefix} "
            f"Reason: `{classification.reason_code}` "
            f"(category: `{classification.category.value}`, "
            f"transient: {str(classification.transient).lower()}, "
            f"confidence: {classification.confidence:.2f}).\n\n"
            f"{classification.summary}"
        )
        sections: list[str] = [preamble, self.render_yaml_block(payload)]
        if traceback_text:
            trimmed = traceback_text.rstrip()
            sections.append(f"```\n{trimmed}\n```")
        return "\n\n".join(sections) + "\n"


# Module-level convenience writer used by the run loop. Subclasses can
# build their own writer with a different ``preamble_prefix``.
_DEFAULT_WRITER: Final[FailureTaxonomyWriter] = FailureTaxonomyWriter()


def render_failure_comment(
    exc: Exception | str,
    *,
    context: dict[str, object] | None = None,
    evidence_path: str = "",
    traceback_text: str | None = None,
    next_action: str | None = None,
) -> tuple[str, FailureClassification]:
    """Classify ``exc`` and render the full tracker comment body.

    Convenience wrapper for the agent run loop. Returns both the comment
    body (to feed into the tracker adapter) and the underlying
    classification (so the caller can emit a lifecycle event with the
    same fields).
    """

    classification = classify_failure(exc, context)
    body = _DEFAULT_WRITER.render_comment(
        classification,
        next_action=next_action,
        evidence_path=evidence_path,
        traceback_text=traceback_text,
    )
    return body, classification


# ---------------------------------------------------------------------------
# Helpers retained for the dispatcher wiring ticket
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ParsedYAMLBlock:
    """Internal: result of locating the YAML payload in a rendered body."""

    raw: str = ""
    payload: dict[str, object] = field(default_factory=dict)


def parse_failure_comment(body: str) -> _ParsedYAMLBlock:
    """Extract the structured payload from a previously rendered comment.

    Used by downstream parsers (auto-escalation, dead-letter pipeline).
    A missing fence returns an empty :class:`_ParsedYAMLBlock`; this
    function never raises. Tolerant of ``\\r\\n`` line endings introduced
    by tracker UIs or paste pipelines (GitHub web editor, Plane, etc.).
    """

    # Normalise CRLF / CR to LF before fence matching so the search
    # strings below stay simple. yaml.safe_load handles either style,
    # but the literal `\n` boundary in `fence_open` / `fence_close`
    # would otherwise miss a payload that arrived with CRLF endings.
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    fence_open = f"```{FAILURE_YAML_FENCE}\n"
    fence_close = "\n```"
    open_idx = body.find(fence_open)
    if open_idx < 0:
        return _ParsedYAMLBlock()
    start = open_idx + len(fence_open)
    close_idx = body.find(fence_close, start)
    if close_idx < 0:
        return _ParsedYAMLBlock()
    raw = body[start:close_idx]
    try:
        loaded: object = yaml.safe_load(raw)
    except yaml.YAMLError:
        return _ParsedYAMLBlock(raw=raw)
    if not isinstance(loaded, dict):
        return _ParsedYAMLBlock(raw=raw)
    # Cast keys back to ``str`` to keep the public surface narrow.
    typed_payload: dict[str, object] = {str(k): v for k, v in loaded.items()}  # type: ignore[misc]
    return _ParsedYAMLBlock(raw=raw, payload=typed_payload)
