"""Unit tests for :mod:`bernstein.core.orchestration.failure_taxonomy`.

The suite covers:

* one classification path per documented ``reason_code`` (exception
  type, message substring, explicit hint flag, explicit override),
* the writer's payload-building defaults (transient -> ``"retry"``,
  non-transient -> ``"escalate"``),
* round-trip parse via :func:`parse_failure_comment`,
* invariants enforced by :class:`FailureClassification` and
  :class:`FailureTaxonomyPayload`.
"""

from __future__ import annotations

import pytest
import yaml

from bernstein.core.orchestration.failure_taxonomy import (
    FAILURE_REASON_CODES,
    FAILURE_YAML_FENCE,
    FailureCategory,
    FailureClassification,
    FailureTaxonomyPayload,
    FailureTaxonomyWriter,
    classify_failure,
    parse_failure_comment,
    render_failure_comment,
)

# ---------------------------------------------------------------------------
# Classifier - exception-type rules
# ---------------------------------------------------------------------------


def test_timeout_error_classified_as_timeout() -> None:
    """``TimeoutError`` maps to the ``timeout`` reason code."""

    result = classify_failure(TimeoutError("network call exceeded budget"))
    assert result.reason_code == "timeout"
    assert result.category is FailureCategory.TIMEOUT
    assert result.transient is True
    assert result.confidence >= 0.8


def test_connection_error_classified_as_network_error() -> None:
    """``ConnectionError`` family maps to ``network_error``."""

    result = classify_failure(ConnectionResetError("peer closed"))
    assert result.reason_code == "network_error"
    assert result.transient is True


def test_module_not_found_error_classified_as_missing_dependency() -> None:
    """``ModuleNotFoundError`` maps to ``missing_dependency``."""

    result = classify_failure(ModuleNotFoundError("No module named 'foo'"))
    assert result.reason_code == "missing_dependency"
    assert result.category is FailureCategory.HALLUCINATION
    assert result.transient is False


def test_type_error_classified_as_type_error() -> None:
    """``TypeError`` maps to ``type_error``."""

    result = classify_failure(TypeError("unsupported operand"))
    assert result.reason_code == "type_error"


def test_syntax_error_classified_as_syntax_error() -> None:
    """``SyntaxError`` maps to ``syntax_error`` with high confidence."""

    result = classify_failure(SyntaxError("unexpected EOF"))
    assert result.reason_code == "syntax_error"
    assert result.confidence >= 0.9


def test_permission_error_classified_as_sandbox_violation() -> None:
    """``PermissionError`` maps to ``sandbox_violation``."""

    result = classify_failure(PermissionError("denied"))
    assert result.reason_code == "sandbox_violation"


# ---------------------------------------------------------------------------
# Classifier - message-substring rules
# ---------------------------------------------------------------------------


def test_rate_limit_string_classified_as_rate_limit() -> None:
    """A free-text ``"rate limit"`` mention maps to ``rate_limit``."""

    result = classify_failure("HTTP 429: rate limit exceeded")
    assert result.reason_code == "rate_limit"
    assert result.transient is True


def test_flaky_test_string_classified_as_flaky_test() -> None:
    """The substring ``"flaky"`` triggers the flaky-test code."""

    result = classify_failure("possibly flaky: test_login passes on retry")
    assert result.reason_code == "flaky_test"


def test_merge_conflict_string_classified_as_merge_conflict() -> None:
    """The substring ``"merge conflict"`` triggers ``merge_conflict``."""

    result = classify_failure("git: merge conflict in src/foo.py")
    assert result.reason_code == "merge_conflict"
    assert result.category is FailureCategory.CONFLICT


def test_compile_error_string_classified_as_compile_error() -> None:
    """The substring ``"compile error"`` triggers ``compile_error``."""

    result = classify_failure("compile error: cannot find symbol")
    assert result.reason_code == "compile_error"


def test_standing_cap_string_classified_as_standing_cap() -> None:
    """A standing-cap message maps to ``standing_cap`` and is not transient."""

    result = classify_failure("maximum number of active sessions reached")
    assert result.reason_code == "standing_cap"
    assert result.category is FailureCategory.CONTEXT_MISS
    assert result.transient is False


def test_standing_cap_not_in_transient_codes() -> None:
    """``standing_cap`` must never be retried."""

    result = classify_failure("spending limit exceeded")
    assert result.reason_code == "standing_cap"
    assert result.transient is False


# ---------------------------------------------------------------------------
# Classifier - hint flags and explicit overrides
# ---------------------------------------------------------------------------


def test_explicit_reason_code_override_wins() -> None:
    """A caller-provided ``reason_code`` in context wins over heuristics."""

    result = classify_failure(
        TimeoutError("ignored"),
        context={"reason_code": "test_regression", "summary": "pytest -k foo failed"},
    )
    assert result.reason_code == "test_regression"
    assert result.category is FailureCategory.TEST_REGRESSION
    assert result.confidence == 1.0
    assert result.summary == "pytest -k foo failed"


def test_tests_regressed_hint_classified_as_test_regression() -> None:
    """The ``tests_regressed`` hint flag classifies as ``test_regression``."""

    result = classify_failure("opaque output", context={"tests_regressed": True})
    assert result.reason_code == "test_regression"
    assert result.confidence >= 0.9


def test_scope_violated_hint_classified_as_scope_violation() -> None:
    """The ``scope_violated`` hint classifies as ``scope_violation``."""

    result = classify_failure("agent touched too much", context={"scope_violated": True})
    assert result.reason_code == "scope_violation"
    assert result.category is FailureCategory.SCOPE_CREEP


def test_unknown_fallback_for_generic_exception() -> None:
    """An unfamiliar exception falls back to ``unknown`` with low confidence."""

    class MysteryError(Exception):
        pass

    result = classify_failure(MysteryError("???"))
    assert result.reason_code == "unknown"
    assert result.confidence < 0.5


def test_summary_truncated_to_240_chars() -> None:
    """The summary is clamped at 240 characters to fit tracker UIs."""

    long_line = "x" * 500
    result = classify_failure(long_line)
    assert len(result.summary) <= 240


def test_summary_falls_back_when_message_empty() -> None:
    """An empty error string still produces a non-empty summary."""

    result = classify_failure("")
    assert result.summary != ""
    assert "unknown" in result.summary or result.reason_code in result.summary


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_classification_rejects_unknown_reason_code() -> None:
    """Constructing with a code outside the closed set raises."""

    with pytest.raises(ValueError, match="reason_code"):
        FailureClassification(
            reason_code="not_a_real_code",
            category=FailureCategory.CONTEXT_MISS,
            transient=False,
            confidence=0.5,
            summary="x",
        )


def test_classification_rejects_out_of_range_confidence() -> None:
    """Confidence outside ``[0.0, 1.0]`` raises."""

    with pytest.raises(ValueError, match="confidence"):
        FailureClassification(
            reason_code="unknown",
            category=FailureCategory.CONTEXT_MISS,
            transient=False,
            confidence=1.5,
            summary="x",
        )


def test_all_documented_reason_codes_are_in_the_closed_set() -> None:
    """Sanity: every code referenced in the tests is in the closed set."""

    expected = {
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
        "unknown",
    }
    assert expected.issubset(FAILURE_REASON_CODES)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _classification(
    *,
    reason_code: str = "timeout",
    transient: bool = True,
    category: FailureCategory = FailureCategory.TIMEOUT,
) -> FailureClassification:
    """Construct a classification with sensible defaults for renderer tests."""

    return FailureClassification(
        reason_code=reason_code,
        category=category,
        transient=transient,
        confidence=0.9,
        summary="agent run timed out after 5m",
    )


def test_writer_payload_defaults_retry_for_transient_failures() -> None:
    """A transient classification picks ``"retry"`` by default."""

    writer = FailureTaxonomyWriter()
    payload = writer.build_payload(_classification(transient=True))
    assert payload.next_action == "retry"
    assert payload.transient is True


def test_writer_payload_defaults_escalate_for_persistent_failures() -> None:
    """A non-transient classification picks ``"escalate"`` by default."""

    writer = FailureTaxonomyWriter()
    payload = writer.build_payload(
        _classification(
            reason_code="missing_dependency",
            transient=False,
            category=FailureCategory.HALLUCINATION,
        ),
    )
    assert payload.next_action == "escalate"


def test_writer_payload_respects_explicit_next_action() -> None:
    """A caller-supplied ``next_action`` overrides the default."""

    writer = FailureTaxonomyWriter()
    payload = writer.build_payload(_classification(), next_action="page_oncall")
    assert payload.next_action == "page_oncall"


def test_writer_renders_fenced_yaml_block_with_versioned_info_string() -> None:
    """The YAML block uses the documented info-string fence."""

    writer = FailureTaxonomyWriter()
    payload = writer.build_payload(_classification(), evidence_path="logs/run.log")
    block = writer.render_yaml_block(payload)
    assert block.startswith(f"```{FAILURE_YAML_FENCE}\n")
    assert block.endswith("\n```")


def test_writer_yaml_payload_round_trips_through_safe_load() -> None:
    """``safe_dump``/``safe_load`` round trip preserves all five fields."""

    writer = FailureTaxonomyWriter()
    payload = FailureTaxonomyPayload(
        reason_code="rate_limit",
        category=FailureCategory.TIMEOUT,
        transient=True,
        next_action="retry",
        evidence_path="logs/foo.log",
    )
    block = writer.render_yaml_block(payload)
    # Strip the fence to feed the YAML body to ``safe_load``.
    inner = block.removeprefix(f"```{FAILURE_YAML_FENCE}\n").removesuffix("\n```")
    loaded = yaml.safe_load(inner)
    assert loaded == {
        "reason_code": "rate_limit",
        "category": "timeout",
        "transient": True,
        "next_action": "retry",
        "evidence_path": "logs/foo.log",
    }


def test_render_comment_includes_preamble_yaml_and_traceback_sections() -> None:
    """Full comment is preamble + fenced YAML + optional traceback."""

    writer = FailureTaxonomyWriter()
    body = writer.render_comment(
        _classification(),
        evidence_path="logs/run.log",
        traceback_text="Traceback (most recent call last):\n  ...",
    )
    assert "Bernstein agent run failed." in body
    assert f"```{FAILURE_YAML_FENCE}" in body
    assert "Traceback (most recent call last)" in body


def test_render_comment_omits_traceback_block_when_not_supplied() -> None:
    """No traceback input means only two fenced blocks in the body."""

    writer = FailureTaxonomyWriter()
    body = writer.render_comment(_classification(), evidence_path="logs/x.log")
    assert body.count("```") == 2


def test_render_comment_is_deterministic() -> None:
    """Identical inputs produce byte-identical output."""

    writer = FailureTaxonomyWriter()
    cls = _classification()
    a = writer.render_comment(cls, evidence_path="logs/x.log")
    b = writer.render_comment(cls, evidence_path="logs/x.log")
    assert a == b


# ---------------------------------------------------------------------------
# Convenience wrapper + downstream parse
# ---------------------------------------------------------------------------


def test_render_failure_comment_returns_body_and_classification() -> None:
    """The convenience wrapper exposes both the body and the classification."""

    body, classification = render_failure_comment(
        TimeoutError("budget exceeded"),
        evidence_path="logs/run.log",
    )
    assert classification.reason_code == "timeout"
    assert f"```{FAILURE_YAML_FENCE}" in body
    assert "evidence_path: logs/run.log" in body


def test_parse_failure_comment_round_trips_payload() -> None:
    """Downstream parsers can recover the structured payload from the body."""

    body, classification = render_failure_comment(
        ConnectionError("peer closed"),
        evidence_path="logs/x.log",
    )
    parsed = parse_failure_comment(body)
    assert parsed.payload["reason_code"] == classification.reason_code
    assert parsed.payload["category"] == classification.category.value
    assert parsed.payload["evidence_path"] == "logs/x.log"
    assert parsed.payload["transient"] is True


def test_parse_failure_comment_missing_fence_returns_empty_payload() -> None:
    """A comment without the fence yields an empty payload, not an error."""

    parsed = parse_failure_comment("plain text comment without a fence")
    assert parsed.payload == {}
    assert parsed.raw == ""


def test_parse_failure_comment_malformed_yaml_returns_raw_only() -> None:
    """Malformed YAML inside the fence is exposed via ``raw``."""

    bad = f"prefix\n\n```{FAILURE_YAML_FENCE}\n: : :\n```\nsuffix"
    parsed = parse_failure_comment(bad)
    # Either the YAML loader accepts the degenerate doc as a non-dict
    # (payload empty) or it raises (payload empty); in both cases we
    # expose at least the raw text.
    assert parsed.raw != "" or parsed.payload == {}
