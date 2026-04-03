"""Tests for permission_explain — progressive disclosure for permission requests."""

from __future__ import annotations

from bernstein.core.policy_engine import DecisionType, PermissionDecision
from bernstein.permission_explain import PermissionExplanation, explain_decision

# --- TestPermissionExplanation ---


class TestPermissionExplanation:
    def test_render_basic(self) -> None:
        exp = PermissionExplanation(
            summary="Write src/auth.py",
            tool_name="claude",
            operation="write_file",
            affected_paths="src/auth.py",
            risk_level="medium",
            rationale="Agent needs to modify authentication to add new endpoint",
        )
        rendered = exp.render()
        assert "write_file" in rendered
        assert "src/auth.py" in rendered
        assert "medium" in rendered
        assert "authentication" in rendered

    def test_render_with_rationale_wrap(self) -> None:
        long_rationale = (
            "This is a very long rationale that should wrap across multiple lines when rendered in the terminal output"
        )
        exp = PermissionExplanation(
            summary="Run npm install",
            tool_name="codex",
            operation="run_command",
            affected_paths="npm install --save new-package",
            risk_level="low",
            rationale=long_rationale,
        )
        rendered = exp.render()
        assert "run_command" in rendered
        assert "npm install" in rendered

    def test_risk_indicator_high(self) -> None:
        exp = PermissionExplanation(
            summary="Delete .env",
            tool_name="claude",
            operation="delete_file",
            affected_paths=".env",
            risk_level="high",
        )
        rendered = exp.render()
        assert "high" in rendered

    def test_default_risk_level(self) -> None:
        exp = PermissionExplanation(
            summary="Read config",
            tool_name="manager",
            operation="read_file",
            affected_paths="config.yaml",
        )
        assert exp.risk_level == "low"


# --- TestExplainDecision ---


class TestExplainDecision:
    def test_deny_is_high_risk(self) -> None:
        decision = PermissionDecision(type=DecisionType.DENY, reason="blocked by policy")
        exp = explain_decision(decision)
        assert exp.risk_level == "high"

    def test_allow_is_low_risk(self) -> None:
        decision = PermissionDecision(type=DecisionType.ALLOW, reason="allowed by default")
        exp = explain_decision(decision)
        assert exp.risk_level == "low"

    def test_contains_decision_reason(self) -> None:
        decision = PermissionDecision(type=DecisionType.DENY, reason="dangerous operation detected")
        exp = explain_decision(decision)
        assert "dangerous operation" in exp.summary
        assert "dangerous operation" in exp.rationale
