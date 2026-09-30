"""Unit tests for layered permission decision graph."""

from __future__ import annotations

import pytest
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


@pytest.mark.parametrize("deny_first", [True, False], ids=["deny-added-first", "immune-added-first"])
def test_deny_beats_immune_in_both_insertion_orders(deny_first: bool) -> None:
    """DENY outranks IMMUNE whichever order the two arrive in.

    `evaluate()` sorts by a precedence table and `sorted` is stable, so if DENY
    and IMMUNE ever shared a rank the winner would be decided by insertion
    order -- and with it the `reason` the operator reads and the
    `bypass_immune` flag consulted for the verdict. The existing precedence
    test never puts the two in one graph, so that tie passed unnoticed.
    """
    deny = PermissionDecision(DecisionType.DENY, "denied by rule")
    immune = PermissionDecision(DecisionType.IMMUNE, "immune layer")

    graph = DecisionGraph(bypass_enabled=False)
    for decision in (deny, immune) if deny_first else (immune, deny):
        graph.add_decision(decision)

    result = graph.evaluate()
    assert result.type == DecisionType.DENY
    assert result.reason == "denied by rule"


@pytest.mark.parametrize("deny_first", [True, False], ids=["deny-added-first", "immune-added-first"])
def test_immune_survives_bypass_when_deny_is_bypassable(deny_first: bool) -> None:
    """A bypassed DENY hands the verdict to IMMUNE, not to ALLOW.

    Ranking DENY first is what makes it the decision bypass is applied to;
    IMMUNE is then the next one considered, and it refuses to be bypassed.
    Neither half of that depends on which decision was added first.
    """
    deny = PermissionDecision(DecisionType.DENY, "denied by rule", bypass_immune=False)
    immune = PermissionDecision(DecisionType.IMMUNE, "immune layer", bypass_immune=True)

    graph = DecisionGraph(bypass_enabled=True)
    for decision in (deny, immune) if deny_first else (immune, deny):
        graph.add_decision(decision)

    result = graph.evaluate()
    assert result.type == DecisionType.IMMUNE
    assert result.reason == "immune layer"
