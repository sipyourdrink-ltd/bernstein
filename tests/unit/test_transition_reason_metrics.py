"""Tests for transition reason histogram/counter metrics.

Validates that TransitionReason values are recorded as labeled Prometheus
counters with cardinality guards and kill-switch support.
"""

from __future__ import annotations

from bernstein.core.models import TransitionReason
from prometheus_client import CollectorRegistry, Counter, generate_latest

# ---------------------------------------------------------------------------
# Helpers — fresh registry per test to avoid cross-test pollution
# ---------------------------------------------------------------------------


def _make_counters(reg: CollectorRegistry) -> tuple[Counter, Counter]:
    """Create agent + task transition-reason counters on *reg*."""
    agent_ctr = Counter(
        "bernstein_agent_transition_reasons_total",
        "Agent lifecycle transitions by reason.",
        labelnames=["reason", "role"],
        registry=reg,
    )
    task_ctr = Counter(
        "bernstein_task_transition_reasons_total",
        "Task lifecycle transitions by reason.",
        labelnames=["reason", "role"],
        registry=reg,
    )
    return agent_ctr, task_ctr


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTransitionReasonCounterDefinitions:
    """Counters exist on the shared registry and have the right labels."""

    def test_agent_counter_exists(self) -> None:
        from bernstein.core.prometheus import agent_transition_reasons_total

        assert agent_transition_reasons_total is not None

    def test_task_counter_exists(self) -> None:
        from bernstein.core.prometheus import task_transition_reasons_total

        assert task_transition_reasons_total is not None

    def test_agent_counter_label_names(self) -> None:
        from bernstein.core.prometheus import agent_transition_reasons_total

        assert list(agent_transition_reasons_total._labelnames) == ["reason", "role"]

    def test_task_counter_label_names(self) -> None:
        from bernstein.core.prometheus import task_transition_reasons_total

        assert list(task_transition_reasons_total._labelnames) == ["reason", "role"]


class TestRecordTransitionReason:
    """record_transition_reason() increments the correct counter."""

    def test_agent_reason_increments(self) -> None:
        reg = CollectorRegistry()
        agent_ctr, _task_ctr = _make_counters(reg)

        agent_ctr.labels(reason="aborted", role="backend").inc()
        agent_ctr.labels(reason="aborted", role="backend").inc()
        agent_ctr.labels(reason="completed", role="qa").inc()

        output = generate_latest(reg).decode()
        assert 'bernstein_agent_transition_reasons_total{reason="aborted",role="backend"} 2.0' in output
        assert 'bernstein_agent_transition_reasons_total{reason="completed",role="qa"} 1.0' in output

    def test_task_reason_increments(self) -> None:
        reg = CollectorRegistry()
        _agent_ctr, task_ctr = _make_counters(reg)

        task_ctr.labels(reason="retry", role="backend").inc()

        output = generate_latest(reg).decode()
        assert 'bernstein_task_transition_reasons_total{reason="retry",role="backend"} 1.0' in output

    def test_record_function_agent(self) -> None:
        """record_transition_reason() with entity_type='agent'."""
        from bernstein.core.prometheus import (
            agent_transition_reasons_total,
            record_transition_reason,
            registry,
        )

        # Snapshot current value
        before = agent_transition_reasons_total.labels(reason="max_output_tokens", role="security")._value.get()
        record_transition_reason("max_output_tokens", "security", entity_type="agent")
        after = agent_transition_reasons_total.labels(reason="max_output_tokens", role="security")._value.get()

        assert after == before + 1

        output = generate_latest(registry).decode()
        assert 'bernstein_agent_transition_reasons_total{reason="max_output_tokens",role="security"}' in output

    def test_record_function_task(self) -> None:
        """record_transition_reason() with entity_type='task'."""
        from bernstein.core.prometheus import (
            record_transition_reason,
            registry,
            task_transition_reasons_total,
        )

        before = task_transition_reasons_total.labels(reason="prompt_too_long", role="backend")._value.get()
        record_transition_reason("prompt_too_long", "backend", entity_type="task")
        after = task_transition_reasons_total.labels(reason="prompt_too_long", role="backend")._value.get()

        assert after == before + 1

        output = generate_latest(registry).decode()
        assert 'bernstein_task_transition_reasons_total{reason="prompt_too_long",role="backend"}' in output


class TestSanitizeReason:
    """_sanitize_reason() enforces the closed set and cardinality limits."""

    def test_known_reason_passes_through(self) -> None:
        from bernstein.core.prometheus import _sanitize_reason

        for reason in TransitionReason:
            assert _sanitize_reason(reason.value) == reason.value

    def test_unknown_reason_still_passes_under_limit(self) -> None:
        from bernstein.core.prometheus import _sanitize_reason

        result = _sanitize_reason("never_seen_before_xyz_test")
        assert result == "never_seen_before_xyz_test"

    def test_empty_string_becomes_unknown(self) -> None:
        from bernstein.core.prometheus import _sanitize_reason

        assert _sanitize_reason("") == "unknown"
        assert _sanitize_reason("   ") == "unknown"

    def test_whitespace_stripped(self) -> None:
        from bernstein.core.prometheus import _sanitize_reason

        assert _sanitize_reason("  completed  ") == "completed"

    def test_case_insensitive(self) -> None:
        from bernstein.core.prometheus import _sanitize_reason

        assert _sanitize_reason("ABORTED") == "aborted"
        assert _sanitize_reason("Max_Output_Tokens") == "max_output_tokens"


class TestCardinalityLimit:
    """Cardinality limit prevents unbounded label explosion."""

    def test_cardinality_limit_respected(self) -> None:
        from bernstein.core import prometheus as mod

        original_seen = mod._seen_reasons.copy()
        original_limit = mod._CARDINALITY_LIMIT

        try:
            mod._seen_reasons = set()
            mod._CARDINALITY_LIMIT = 3

            # First 3 unknown reasons pass through
            assert mod._sanitize_reason("custom_a") == "custom_a"
            assert mod._sanitize_reason("custom_b") == "custom_b"
            assert mod._sanitize_reason("custom_c") == "custom_c"

            # Fourth unknown reason gets bucketed as "unknown"
            assert mod._sanitize_reason("custom_d") == "unknown"

            # Known reasons still pass through regardless of limit
            assert mod._sanitize_reason("completed") == "completed"
        finally:
            mod._seen_reasons = original_seen
            mod._CARDINALITY_LIMIT = original_limit


class TestKillSwitch:
    """record_transition_reason() respects the Prometheus kill-switch."""

    def test_disabled_sink_is_noop(self) -> None:
        from bernstein.core.prometheus import (
            agent_transition_reasons_total,
            record_transition_reason,
            set_prometheus_enabled,
        )

        before = agent_transition_reasons_total.labels(reason="aborted", role="killswitch_test")._value.get()

        set_prometheus_enabled(False)
        try:
            record_transition_reason("aborted", "killswitch_test", entity_type="agent")
        finally:
            set_prometheus_enabled(True)

        after = agent_transition_reasons_total.labels(reason="aborted", role="killswitch_test")._value.get()

        assert after == before  # no increment


class TestAllTransitionReasonsRecordable:
    """Every TransitionReason enum value can be recorded without error."""

    def test_all_reasons_record_for_agent(self) -> None:
        from bernstein.core.prometheus import (
            agent_transition_reasons_total,
            record_transition_reason,
        )

        for reason in TransitionReason:
            before = agent_transition_reasons_total.labels(reason=reason.value, role="test_all")._value.get()
            record_transition_reason(reason.value, "test_all", entity_type="agent")
            after = agent_transition_reasons_total.labels(reason=reason.value, role="test_all")._value.get()
            assert after == before + 1, f"Failed for reason={reason.value}"

    def test_all_reasons_record_for_task(self) -> None:
        from bernstein.core.prometheus import (
            record_transition_reason,
            task_transition_reasons_total,
        )

        for reason in TransitionReason:
            before = task_transition_reasons_total.labels(reason=reason.value, role="test_all")._value.get()
            record_transition_reason(reason.value, "test_all", entity_type="task")
            after = task_transition_reasons_total.labels(reason=reason.value, role="test_all")._value.get()
            assert after == before + 1, f"Failed for reason={reason.value}"


class TestPrometheusExport:
    """Transition reason metrics appear in the Prometheus text exposition."""

    def test_metrics_appear_in_scrape_output(self) -> None:
        from bernstein.core.prometheus import record_transition_reason, registry

        record_transition_reason("completed", "backend", entity_type="agent")
        record_transition_reason("aborted", "qa", entity_type="task")

        output = generate_latest(registry).decode()

        assert "bernstein_agent_transition_reasons_total" in output
        assert "bernstein_task_transition_reasons_total" in output

    def test_help_text_present(self) -> None:
        from bernstein.core.prometheus import registry

        output = generate_latest(registry).decode()

        assert "# HELP bernstein_agent_transition_reasons_total" in output
        assert "# HELP bernstein_task_transition_reasons_total" in output
