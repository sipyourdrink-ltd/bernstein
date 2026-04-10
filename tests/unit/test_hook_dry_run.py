"""Tests for HOOK-015 — hook dry-run mode (hook_dry_run.py)."""

from __future__ import annotations

from bernstein.core.hook_dry_run import (
    DEFAULT_HOOK_REGISTRY,
    DryRunReport,
    DryRunResult,
    format_dry_run_report,
    simulate_hook_event,
)

# ---------------------------------------------------------------------------
# DryRunResult dataclass
# ---------------------------------------------------------------------------


class TestDryRunResult:
    """DryRunResult is frozen and stores all required fields."""

    def test_construction(self) -> None:
        result = DryRunResult(
            hook_name="slack_notify",
            event_type="task.failed",
            would_execute=True,
            payload={"task_id": "t-1"},
            matched_handlers=["slack_notify", "pagerduty_alert"],
            execution_time_ms=0.05,
        )
        assert result.hook_name == "slack_notify"
        assert result.event_type == "task.failed"
        assert result.would_execute is True
        assert result.payload == {"task_id": "t-1"}
        assert result.matched_handlers == ["slack_notify", "pagerduty_alert"]
        assert result.execution_time_ms == 0.05

    def test_frozen(self) -> None:
        result = DryRunResult(
            hook_name="log_handler",
            event_type="agent.spawned",
            would_execute=True,
            payload={},
            matched_handlers=["log_handler"],
        )
        try:
            result.hook_name = "other"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
        except AttributeError:
            pass

    def test_default_execution_time(self) -> None:
        result = DryRunResult(
            hook_name="h",
            event_type="e",
            would_execute=False,
            payload={},
            matched_handlers=[],
        )
        assert result.execution_time_ms is None


# ---------------------------------------------------------------------------
# DryRunReport dataclass
# ---------------------------------------------------------------------------


class TestDryRunReport:
    """DryRunReport is frozen and aggregates results."""

    def test_construction(self) -> None:
        report = DryRunReport(
            event_type="task.completed",
            payload={"task_id": "t-2"},
            results=[],
            total_handlers=0,
            would_trigger=0,
        )
        assert report.event_type == "task.completed"
        assert report.payload == {"task_id": "t-2"}
        assert report.results == []
        assert report.total_handlers == 0
        assert report.would_trigger == 0

    def test_frozen(self) -> None:
        report = DryRunReport(
            event_type="e",
            payload={},
        )
        try:
            report.event_type = "other"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# simulate_hook_event — known event type
# ---------------------------------------------------------------------------


class TestSimulateKnownEvent:
    """Simulate with an event type present in the registry."""

    def test_task_failed_returns_two_handlers(self) -> None:
        report = simulate_hook_event(
            "task.failed",
            {"task_id": "t-99", "error": "segfault"},
        )
        assert report.event_type == "task.failed"
        assert report.total_handlers == 2
        assert report.would_trigger == 2
        handler_names = [r.hook_name for r in report.results]
        assert "slack_notify" in handler_names
        assert "pagerduty_alert" in handler_names

    def test_all_results_would_execute(self) -> None:
        report = simulate_hook_event("task.failed", {"error": "boom"})
        for result in report.results:
            assert result.would_execute is True

    def test_payload_propagated(self) -> None:
        payload = {"task_id": "t-5", "role": "backend"}
        report = simulate_hook_event("task.completed", payload)
        for result in report.results:
            assert result.payload is payload

    def test_agent_spawned_single_handler(self) -> None:
        report = simulate_hook_event("agent.spawned", {"session_id": "s-1"})
        assert report.total_handlers == 1
        assert report.results[0].hook_name == "log_handler"

    def test_execution_time_is_set(self) -> None:
        report = simulate_hook_event("task.failed", {})
        for result in report.results:
            assert result.execution_time_ms is not None
            assert result.execution_time_ms >= 0.0


# ---------------------------------------------------------------------------
# simulate_hook_event — unknown event type
# ---------------------------------------------------------------------------


class TestSimulateUnknownEvent:
    """Simulate with an event type NOT in the registry."""

    def test_unknown_event_returns_empty(self) -> None:
        report = simulate_hook_event("bogus.event", {"x": 1})
        assert report.event_type == "bogus.event"
        assert report.total_handlers == 0
        assert report.would_trigger == 0
        assert report.results == []

    def test_payload_still_recorded(self) -> None:
        payload = {"key": "value"}
        report = simulate_hook_event("no.such.event", payload)
        assert report.payload is payload


# ---------------------------------------------------------------------------
# simulate_hook_event — custom registry
# ---------------------------------------------------------------------------


class TestSimulateCustomRegistry:
    """Simulate with a caller-provided registry override."""

    def test_custom_registry_used(self) -> None:
        custom: dict[str, list[str]] = {
            "deploy.started": ["deploy_hook", "audit_hook"],
        }
        report = simulate_hook_event("deploy.started", {}, hook_registry=custom)
        assert report.total_handlers == 2
        handler_names = [r.hook_name for r in report.results]
        assert handler_names == ["deploy_hook", "audit_hook"]

    def test_custom_registry_ignores_defaults(self) -> None:
        custom: dict[str, list[str]] = {"task.failed": ["my_handler"]}
        report = simulate_hook_event("task.failed", {}, hook_registry=custom)
        assert report.total_handlers == 1
        assert report.results[0].hook_name == "my_handler"

    def test_empty_custom_registry(self) -> None:
        report = simulate_hook_event("task.failed", {}, hook_registry={})
        assert report.total_handlers == 0
        assert report.results == []

    def test_custom_single_handler(self) -> None:
        custom: dict[str, list[str]] = {"custom.event": ["only_handler"]}
        report = simulate_hook_event("custom.event", {"data": 42}, hook_registry=custom)
        assert report.would_trigger == 1
        assert report.results[0].matched_handlers == ["only_handler"]


# ---------------------------------------------------------------------------
# format_dry_run_report
# ---------------------------------------------------------------------------


class TestFormatDryRunReport:
    """format_dry_run_report produces readable output."""

    def test_no_handlers_message(self) -> None:
        report = simulate_hook_event("unknown.event", {})
        text = format_dry_run_report(report)
        assert "No handlers registered" in text
        assert "unknown.event" in text

    def test_handlers_listed(self) -> None:
        report = simulate_hook_event("task.failed", {"task_id": "t-1"})
        text = format_dry_run_report(report)
        assert "WOULD FIRE" in text
        assert "slack_notify" in text
        assert "pagerduty_alert" in text
        assert "Total handlers: 2" in text
        assert "Would trigger: 2" in text

    def test_output_contains_event_type(self) -> None:
        report = simulate_hook_event("agent.spawned", {})
        text = format_dry_run_report(report)
        assert "agent.spawned" in text

    def test_output_is_string(self) -> None:
        report = simulate_hook_event("task.completed", {})
        text = format_dry_run_report(report)
        assert isinstance(text, str)
        assert len(text) > 0


# ---------------------------------------------------------------------------
# DEFAULT_HOOK_REGISTRY coverage
# ---------------------------------------------------------------------------


class TestDefaultRegistry:
    """The default registry maps well-known events to handlers."""

    def test_registry_is_non_empty(self) -> None:
        assert len(DEFAULT_HOOK_REGISTRY) > 0

    def test_task_failed_has_handlers(self) -> None:
        assert "task.failed" in DEFAULT_HOOK_REGISTRY
        assert len(DEFAULT_HOOK_REGISTRY["task.failed"]) >= 2

    def test_agent_spawned_has_handler(self) -> None:
        assert "agent.spawned" in DEFAULT_HOOK_REGISTRY
        assert "log_handler" in DEFAULT_HOOK_REGISTRY["agent.spawned"]

    def test_all_values_are_string_lists(self) -> None:
        for event_type, handlers in DEFAULT_HOOK_REGISTRY.items():
            assert isinstance(event_type, str), f"key {event_type!r} is not a string"
            assert isinstance(handlers, list), f"value for {event_type!r} is not a list"
            for h in handlers:
                assert isinstance(h, str), f"handler {h!r} in {event_type!r} is not a string"
