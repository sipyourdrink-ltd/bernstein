"""Unit tests for layered permission decision graph."""

from __future__ import annotations

from bernstein.core.policy_engine import DecisionGraph, DecisionType, PermissionDecision


def test_decision_graph_precedence():
    # ALLOW < ASK < SAFETY < IMMUNE < DENY
    graph = DecisionGraph(bypass_enabled=False)
    graph.add_decision(PermissionDecision(DecisionType.ALLOW, "ok"))
    graph.add_decision(PermissionDecision(DecisionType.ASK, "sure?"))
    graph.add_decision(PermissionDecision(DecisionType.SAFETY, "secret!"))

    result = graph.evaluate()
    assert result.type == DecisionType.SAFETY
    assert result.reason == "secret!"

    graph.add_decision(PermissionDecision(DecisionType.DENY, "NO!"))
    result = graph.evaluate()
    assert result.type == DecisionType.DENY
    assert result.reason == "NO!"


def test_decision_graph_bypass_non_immune():
    # Bypass is enabled, non-immune ASK should be ignored
    graph = DecisionGraph(bypass_enabled=True)
    graph.add_decision(PermissionDecision(DecisionType.ASK, "sure?", bypass_immune=False))

    result = graph.evaluate()
    assert result.type == DecisionType.ALLOW
    assert "All checks passed or bypassed" in result.reason


def test_decision_graph_bypass_immune_stays_blocked():
    # Bypass is enabled, but IMMUNE tier cannot be bypassed
    graph = DecisionGraph(bypass_enabled=True)
    graph.add_decision(PermissionDecision(DecisionType.IMMUNE, "root!", bypass_immune=True))
    graph.add_decision(PermissionDecision(DecisionType.ASK, "sure?", bypass_immune=False))

    result = graph.evaluate()
    assert result.type == DecisionType.IMMUNE
    assert result.reason == "root!"


def test_decision_graph_safety_is_immune_in_practice():
    # Verify that SAFETY can be marked bypass_immune=True and it works
    graph = DecisionGraph(bypass_enabled=True)
    graph.add_decision(PermissionDecision(DecisionType.SAFETY, "secret!", bypass_immune=True))

    result = graph.evaluate()
    assert result.type == DecisionType.SAFETY
    assert result.reason == "secret!"


def test_empty_graph_allows():
    graph = DecisionGraph()
    result = graph.evaluate()
    assert result.type == DecisionType.ALLOW


def test_deny_beats_immune_in_both_insertion_orders():
    # DENY has higher precedence than IMMUNE. The result must not depend
    # on insertion order: sorted() is stable, so a tie in the priority
    # table would resolve by whichever decision was added first.
    for decisions in (
        [
            PermissionDecision(DecisionType.DENY, "deny-reason"),
            PermissionDecision(DecisionType.IMMUNE, "immune-reason", bypass_immune=True),
        ],
        [
            PermissionDecision(DecisionType.IMMUNE, "immune-reason", bypass_immune=True),
            PermissionDecision(DecisionType.DENY, "deny-reason"),
        ],
    ):
        graph = DecisionGraph(bypass_enabled=False)
        for d in decisions:
            graph.add_decision(d)

        result = graph.evaluate()

        assert result.type == DecisionType.DENY, (
            f"DENY must outrank IMMUNE regardless of insertion order; got {result.type} "
            f"for order {[d.type for d in decisions]}"
        )
        assert result.reason == "deny-reason"


def test_immune_survives_bypass_when_deny_is_bypassable():
    # With bypass enabled, a DENY that is not bypass-immune is skipped, so
    # the surviving decision is IMMUNE (bypass_immune=True). The result must
    # not depend on insertion order.
    for decisions in (
        [
            PermissionDecision(DecisionType.DENY, "deny-reason", bypass_immune=False),
            PermissionDecision(DecisionType.IMMUNE, "immune-reason", bypass_immune=True),
        ],
        [
            PermissionDecision(DecisionType.IMMUNE, "immune-reason", bypass_immune=True),
            PermissionDecision(DecisionType.DENY, "deny-reason", bypass_immune=False),
        ],
    ):
        graph = DecisionGraph(bypass_enabled=True)
        for d in decisions:
            graph.add_decision(d)

        result = graph.evaluate()

        assert result.type == DecisionType.IMMUNE, (
            f"IMMUNE must survive bypass when DENY is bypassable; got {result.type} "
            f"for order {[d.type for d in decisions]}"
        )
        assert result.reason == "immune-reason"
