"""Tests for GuardRegistry.

The property this protects: a guard that was evaluated and never fired must
be distinguishable from a guard that was never evaluated at all. Both look
like "zero firings" from the outside; only a recorded evaluation count tells
them apart.
"""

from __future__ import annotations

from bernstein.core.observability.guard_registry import (
    GuardRegistry,
    reachability_report,
)


class TestGuardRegistryRecording:
    def test_record_evaluation_counts_calls_and_outcome_distribution(self) -> None:
        registry = GuardRegistry()
        registry.record_evaluation("guard-a", "clean")
        registry.record_evaluation("guard-a", "clean")
        registry.record_evaluation("guard-a", "violation")

        [record] = registry.records()
        assert record.guard_id == "guard-a"
        assert record.evaluations == 3
        assert record.outcomes == {"clean": 2, "violation": 1}

    def test_register_without_recording_reports_zero_evaluations(self) -> None:
        registry = GuardRegistry()
        registry.register("guard-b")

        [record] = registry.records()
        assert record.evaluations == 0
        assert record.outcomes == {}

    def test_record_evaluation_implicitly_registers_the_guard(self) -> None:
        registry = GuardRegistry()
        registry.record_evaluation("guard-c", "clean")

        assert [r.guard_id for r in registry.records()] == ["guard-c"]


class TestReachabilityReport:
    def test_orders_by_guard_id(self) -> None:
        registry = GuardRegistry()
        registry.record_evaluation("zebra", "clean")
        registry.record_evaluation("alpha", "clean")
        registry.record_evaluation("mid", "clean")

        assert reachability_report(registry) == [
            ("alpha", 1),
            ("mid", 1),
            ("zebra", 1),
        ]

    def test_includes_registered_but_never_evaluated_guards(self) -> None:
        registry = GuardRegistry()
        registry.register("never-called")
        registry.record_evaluation("called", "clean")

        assert reachability_report(registry) == [
            ("called", 1),
            ("never-called", 0),
        ]

    def test_is_a_pure_projection_not_a_live_view(self) -> None:
        registry = GuardRegistry()
        registry.record_evaluation("guard-d", "clean")

        report = reachability_report(registry)
        registry.record_evaluation("guard-d", "clean")

        # The snapshot taken before the second call does not change under us.
        assert report == [("guard-d", 1)]
        assert reachability_report(registry) == [("guard-d", 2)]


class TestEvaluatedGuardIsDistinguishableFromUnevaluatedGuard:
    """This is the whole feature: two reports that both show zero firings
    must not be equal when one of them was checked and the other was not."""

    def test_ten_passing_checks_differ_from_a_guard_that_was_never_called(self) -> None:
        evaluated = GuardRegistry()
        for _ in range(10):
            evaluated.record_evaluation("breaker", "clean")

        never_called = GuardRegistry()
        never_called.register("breaker")

        evaluated_report = reachability_report(evaluated)
        never_called_report = reachability_report(never_called)

        assert evaluated_report == [("breaker", 10)]
        assert never_called_report == [("breaker", 0)]
        assert evaluated_report != never_called_report

        [record] = evaluated.records()
        assert record.outcomes.get("violation", 0) == 0
        assert record.outcomes == {"clean": 10}
