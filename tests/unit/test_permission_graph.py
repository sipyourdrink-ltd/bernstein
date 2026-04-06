"""Tests for SEC-009: Layered permission decision graph with audit trail."""

from __future__ import annotations

from bernstein.core.permission_graph import (
    ClassifierLayer,
    DenyRulesLayer,
    PermissionGraph,
    PermissionRequest,
    PermissionVerdict,
    PromptLayer,
)


def _req(action: str = "bash", resource: str = "echo hello") -> PermissionRequest:
    return PermissionRequest(agent_id="agent-1", action=action, resource=resource)


class TestDenyRulesLayer:
    def test_deny_matches_pattern(self) -> None:
        layer = DenyRulesLayer(deny_patterns={"bash": ["/etc/shadow"]})
        decision = layer.evaluate(_req(action="bash", resource="cat /etc/shadow"))
        assert decision.verdict == PermissionVerdict.DENY
        assert decision.layer_name == "deny_rules"

    def test_skip_when_no_match(self) -> None:
        layer = DenyRulesLayer(deny_patterns={"bash": ["/etc/shadow"]})
        decision = layer.evaluate(_req(action="bash", resource="ls /tmp"))
        assert decision.verdict == PermissionVerdict.SKIP

    def test_skip_when_different_action(self) -> None:
        layer = DenyRulesLayer(deny_patterns={"write": [".env"]})
        decision = layer.evaluate(_req(action="bash", resource=".env"))
        assert decision.verdict == PermissionVerdict.SKIP

    def test_empty_deny_patterns(self) -> None:
        layer = DenyRulesLayer()
        decision = layer.evaluate(_req())
        assert decision.verdict == PermissionVerdict.SKIP


class TestClassifierLayer:
    def test_destructive_action_denied(self) -> None:
        layer = ClassifierLayer()
        decision = layer.evaluate(_req(action="delete"))
        assert decision.verdict == PermissionVerdict.DENY
        assert "destructive" in decision.reason

    def test_read_only_allowed(self) -> None:
        layer = ClassifierLayer()
        decision = layer.evaluate(_req(action="read"))
        assert decision.verdict == PermissionVerdict.ALLOW

    def test_unknown_action_escalated(self) -> None:
        layer = ClassifierLayer()
        decision = layer.evaluate(_req(action="deploy"))
        assert decision.verdict == PermissionVerdict.ASK

    def test_unknown_action_skipped_when_not_escalating(self) -> None:
        layer = ClassifierLayer(escalate_unknown=False)
        decision = layer.evaluate(_req(action="deploy"))
        assert decision.verdict == PermissionVerdict.SKIP

    def test_custom_destructive_actions(self) -> None:
        layer = ClassifierLayer(destructive_actions={"deploy"})
        decision = layer.evaluate(_req(action="deploy"))
        assert decision.verdict == PermissionVerdict.DENY


class TestPromptLayer:
    def test_asks_by_default(self) -> None:
        layer = PromptLayer()
        decision = layer.evaluate(_req())
        assert decision.verdict == PermissionVerdict.ASK

    def test_auto_approve(self) -> None:
        layer = PromptLayer(auto_approve=True)
        decision = layer.evaluate(_req())
        assert decision.verdict == PermissionVerdict.ALLOW


class TestPermissionGraph:
    def test_deny_layer_wins(self) -> None:
        graph = PermissionGraph()
        graph.add_layer(DenyRulesLayer(deny_patterns={"bash": ["/etc/shadow"]}))
        graph.add_layer(ClassifierLayer())
        graph.add_layer(PromptLayer(auto_approve=True))

        result = graph.evaluate(_req(action="bash", resource="cat /etc/shadow"))
        assert result.verdict == PermissionVerdict.DENY
        assert len(result.decisions) == 1  # Stopped at first non-SKIP

    def test_classifier_fires_after_deny_skips(self) -> None:
        graph = PermissionGraph()
        graph.add_layer(DenyRulesLayer(deny_patterns={"write": ["/etc/shadow"]}))
        graph.add_layer(ClassifierLayer())

        result = graph.evaluate(_req(action="delete", resource="file.txt"))
        assert result.verdict == PermissionVerdict.DENY
        assert len(result.decisions) == 2

    def test_all_skip_uses_default(self) -> None:
        graph = PermissionGraph(default_verdict=PermissionVerdict.ASK)
        graph.add_layer(DenyRulesLayer())
        graph.add_layer(ClassifierLayer(escalate_unknown=False))

        result = graph.evaluate(_req(action="unknown_action"))
        assert result.verdict == PermissionVerdict.ASK
        assert "default" in result.reason.lower()

    def test_empty_graph_uses_default(self) -> None:
        graph = PermissionGraph(default_verdict=PermissionVerdict.ALLOW)
        result = graph.evaluate(_req())
        assert result.verdict == PermissionVerdict.ALLOW

    def test_audit_log_records_decisions(self) -> None:
        graph = PermissionGraph()
        graph.add_layer(PromptLayer(auto_approve=True))

        graph.evaluate(_req(action="read"))
        graph.evaluate(_req(action="write"))
        assert len(graph.audit_log) == 2

    def test_clear_audit_log(self) -> None:
        graph = PermissionGraph()
        graph.add_layer(PromptLayer(auto_approve=True))
        graph.evaluate(_req())
        graph.clear_audit_log()
        assert len(graph.audit_log) == 0

    def test_result_contains_request(self) -> None:
        graph = PermissionGraph()
        graph.add_layer(PromptLayer(auto_approve=True))
        req = _req(action="test")
        result = graph.evaluate(req)
        assert result.request == req

    def test_layers_property(self) -> None:
        graph = PermissionGraph()
        layer1 = DenyRulesLayer()
        layer2 = PromptLayer()
        graph.add_layer(layer1)
        graph.add_layer(layer2)
        assert len(graph.layers) == 2

    def test_read_only_passes_through_to_allow(self) -> None:
        graph = PermissionGraph()
        graph.add_layer(DenyRulesLayer())
        graph.add_layer(ClassifierLayer())
        result = graph.evaluate(_req(action="read"))
        assert result.verdict == PermissionVerdict.ALLOW
