"""Tests for cost notification hooks (COST-007)."""

from __future__ import annotations

from bernstein.core.cost_hooks import (
    CostHookManager,
    CostThreshold,
    CostThresholdEvent,
    create_notification_hook,
)


def test_hook_fires_at_threshold() -> None:
    """Hook fires when spend crosses a threshold."""
    manager = CostHookManager(run_id="r1", budget_usd=10.0)
    fired: list[CostThresholdEvent] = []
    manager.register(fired.append)

    events = manager.check(spent_usd=6.0)  # 60% — fires "info" at 50%
    assert len(events) == 1
    assert events[0].threshold_name == "info"
    assert len(fired) == 1


def test_hook_fires_multiple_thresholds() -> None:
    """Multiple thresholds fire when spend jumps past several."""
    manager = CostHookManager(run_id="r1", budget_usd=10.0)
    fired: list[CostThresholdEvent] = []
    manager.register(fired.append)

    events = manager.check(spent_usd=9.6)  # 96% — fires info, warning, critical
    assert len(events) == 3
    names = {e.threshold_name for e in events}
    assert names == {"info", "warning", "critical"}


def test_hook_fires_once_per_threshold() -> None:
    """Each threshold fires only once."""
    manager = CostHookManager(run_id="r1", budget_usd=10.0)
    fired: list[CostThresholdEvent] = []
    manager.register(fired.append)

    manager.check(spent_usd=6.0)  # fires info
    manager.check(spent_usd=7.0)  # info already fired, nothing new
    assert len(fired) == 1


def test_no_hooks_for_unlimited_budget() -> None:
    """No hooks fire when budget is unlimited (0)."""
    manager = CostHookManager(run_id="r1", budget_usd=0.0)
    fired: list[CostThresholdEvent] = []
    manager.register(fired.append)

    events = manager.check(spent_usd=1000.0)
    assert len(events) == 0
    assert len(fired) == 0


def test_reset_allows_re_fire() -> None:
    """After reset, thresholds can fire again."""
    manager = CostHookManager(run_id="r1", budget_usd=10.0)
    fired: list[CostThresholdEvent] = []
    manager.register(fired.append)

    manager.check(spent_usd=6.0)  # fires info
    assert len(fired) == 1

    manager.reset()
    manager.check(spent_usd=6.0)  # fires info again
    assert len(fired) == 2


def test_custom_thresholds() -> None:
    """Custom thresholds override defaults."""
    thresholds = [CostThreshold(name="half", pct=0.50)]
    manager = CostHookManager(run_id="r1", budget_usd=10.0, thresholds=thresholds)

    events = manager.check(spent_usd=5.5)
    assert len(events) == 1
    assert events[0].threshold_name == "half"


def test_event_to_dict() -> None:
    """CostThresholdEvent.to_dict produces expected keys."""
    event = CostThresholdEvent(
        run_id="r1",
        threshold_name="warning",
        threshold_pct=0.8,
        current_pct=0.85,
        spent_usd=8.5,
        budget_usd=10.0,
    )
    d = event.to_dict()
    assert d["run_id"] == "r1"
    assert d["threshold_name"] == "warning"
    assert d["spent_usd"] == 8.5


def test_fired_events_list() -> None:
    """fired_events returns accumulated events."""
    manager = CostHookManager(run_id="r1", budget_usd=10.0)
    assert len(manager.fired_events) == 0

    manager.check(spent_usd=6.0)
    assert len(manager.fired_events) == 1

    manager.check(spent_usd=8.5)
    assert len(manager.fired_events) == 2  # info + warning


def test_callback_error_swallowed() -> None:
    """Errors in callbacks are swallowed, not propagated."""

    def _bad_callback(event: CostThresholdEvent) -> None:
        msg = "Intentional test error"
        raise RuntimeError(msg)

    manager = CostHookManager(run_id="r1", budget_usd=10.0)
    manager.register(_bad_callback)

    # Should not raise
    events = manager.check(spent_usd=6.0)
    assert len(events) == 1


def test_create_notification_hook() -> None:
    """create_notification_hook produces a callable that invokes notify."""
    calls: list[tuple[str, object]] = []

    class FakeNotificationManager:
        def notify(self, event: str, payload: object) -> None:
            calls.append((event, payload))

    hook = create_notification_hook(FakeNotificationManager())
    event = CostThresholdEvent(
        run_id="r1",
        threshold_name="warning",
        threshold_pct=0.8,
        current_pct=0.85,
        spent_usd=8.5,
        budget_usd=10.0,
    )
    hook(event)
    assert len(calls) == 1
    assert calls[0][0] == "budget.warning"
