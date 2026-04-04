"""Tests for permission mode hierarchy (permission_mode.py).

Table-driven tests covering each mode against representative tool calls,
severity relaxation, legacy flag migration, and compatibility matrix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.permission_mode import (
    LEGACY_FLAG_TO_MODE,
    MODE_ENFORCES,
    MODE_RANK,
    SEVERITY_RANK,
    PermissionMode,
    RuleSeverity,
    default_for_no_match,
    effective_action,
    is_enforced,
    resolve_mode,
)
from bernstein.core.permission_rules import (
    PermissionRule,
    PermissionRuleEngine,
    RuleAction,
)

# ---------------------------------------------------------------------------
# PermissionMode enum basics
# ---------------------------------------------------------------------------


class TestPermissionModeEnum:
    def test_values(self) -> None:
        assert set(PermissionMode) == {
            PermissionMode.BYPASS,
            PermissionMode.PLAN,
            PermissionMode.AUTO,
            PermissionMode.DEFAULT,
        }

    def test_string_values(self) -> None:
        assert PermissionMode.BYPASS == "bypass"
        assert PermissionMode.DEFAULT == "default"

    def test_rank_ordering(self) -> None:
        assert MODE_RANK[PermissionMode.BYPASS] < MODE_RANK[PermissionMode.PLAN]
        assert MODE_RANK[PermissionMode.PLAN] < MODE_RANK[PermissionMode.AUTO]
        assert MODE_RANK[PermissionMode.AUTO] < MODE_RANK[PermissionMode.DEFAULT]


class TestRuleSeverityEnum:
    def test_values(self) -> None:
        assert set(RuleSeverity) == {
            RuleSeverity.CRITICAL,
            RuleSeverity.HIGH,
            RuleSeverity.MEDIUM,
            RuleSeverity.LOW,
        }

    def test_rank_ordering(self) -> None:
        assert SEVERITY_RANK[RuleSeverity.LOW] < SEVERITY_RANK[RuleSeverity.MEDIUM]
        assert SEVERITY_RANK[RuleSeverity.MEDIUM] < SEVERITY_RANK[RuleSeverity.HIGH]
        assert SEVERITY_RANK[RuleSeverity.HIGH] < SEVERITY_RANK[RuleSeverity.CRITICAL]


# ---------------------------------------------------------------------------
# Compatibility matrix: is_enforced
# ---------------------------------------------------------------------------


class TestCompatibilityMatrix:
    """Table-driven tests for the mode × severity → enforced matrix."""

    @pytest.mark.parametrize(
        ("mode", "severity", "expected"),
        [
            # bypass: only critical enforced
            (PermissionMode.BYPASS, RuleSeverity.CRITICAL, True),
            (PermissionMode.BYPASS, RuleSeverity.HIGH, False),
            (PermissionMode.BYPASS, RuleSeverity.MEDIUM, False),
            (PermissionMode.BYPASS, RuleSeverity.LOW, False),
            # plan: critical + high enforced
            (PermissionMode.PLAN, RuleSeverity.CRITICAL, True),
            (PermissionMode.PLAN, RuleSeverity.HIGH, True),
            (PermissionMode.PLAN, RuleSeverity.MEDIUM, False),
            (PermissionMode.PLAN, RuleSeverity.LOW, False),
            # auto: critical + high + medium enforced
            (PermissionMode.AUTO, RuleSeverity.CRITICAL, True),
            (PermissionMode.AUTO, RuleSeverity.HIGH, True),
            (PermissionMode.AUTO, RuleSeverity.MEDIUM, True),
            (PermissionMode.AUTO, RuleSeverity.LOW, False),
            # default: everything enforced
            (PermissionMode.DEFAULT, RuleSeverity.CRITICAL, True),
            (PermissionMode.DEFAULT, RuleSeverity.HIGH, True),
            (PermissionMode.DEFAULT, RuleSeverity.MEDIUM, True),
            (PermissionMode.DEFAULT, RuleSeverity.LOW, True),
        ],
        ids=[
            "bypass-critical",
            "bypass-high",
            "bypass-medium",
            "bypass-low",
            "plan-critical",
            "plan-high",
            "plan-medium",
            "plan-low",
            "auto-critical",
            "auto-high",
            "auto-medium",
            "auto-low",
            "default-critical",
            "default-high",
            "default-medium",
            "default-low",
        ],
    )
    def test_is_enforced(self, mode: PermissionMode, severity: RuleSeverity, expected: bool) -> None:
        assert is_enforced(mode, severity) is expected

    def test_critical_always_enforced(self) -> None:
        """Critical severity is enforced in every mode."""
        for mode in PermissionMode:
            assert is_enforced(mode, RuleSeverity.CRITICAL) is True

    def test_matrix_complete(self) -> None:
        """Every mode × severity combination is defined."""
        for mode in PermissionMode:
            assert mode in MODE_ENFORCES
            for severity in RuleSeverity:
                assert severity in MODE_ENFORCES[mode]


# ---------------------------------------------------------------------------
# effective_action
# ---------------------------------------------------------------------------


class TestEffectiveAction:
    def test_enforced_deny_stays_deny(self) -> None:
        assert effective_action(PermissionMode.DEFAULT, RuleAction.DENY, RuleSeverity.HIGH) == RuleAction.DENY

    def test_enforced_ask_stays_ask(self) -> None:
        assert effective_action(PermissionMode.DEFAULT, RuleAction.ASK, RuleSeverity.LOW) == RuleAction.ASK

    def test_relaxed_deny_becomes_allow(self) -> None:
        assert effective_action(PermissionMode.BYPASS, RuleAction.DENY, RuleSeverity.HIGH) == RuleAction.ALLOW

    def test_relaxed_ask_becomes_allow(self) -> None:
        assert effective_action(PermissionMode.PLAN, RuleAction.ASK, RuleSeverity.MEDIUM) == RuleAction.ALLOW

    def test_allow_always_allow(self) -> None:
        for mode in PermissionMode:
            for sev in RuleSeverity:
                assert effective_action(mode, RuleAction.ALLOW, sev) == RuleAction.ALLOW

    def test_critical_deny_never_relaxed(self) -> None:
        for mode in PermissionMode:
            assert effective_action(mode, RuleAction.DENY, RuleSeverity.CRITICAL) == RuleAction.DENY


# ---------------------------------------------------------------------------
# default_for_no_match
# ---------------------------------------------------------------------------


class TestDefaultForNoMatch:
    def test_default_mode_returns_ask(self) -> None:
        assert default_for_no_match(PermissionMode.DEFAULT) == RuleAction.ASK

    def test_other_modes_return_allow(self) -> None:
        assert default_for_no_match(PermissionMode.BYPASS) == RuleAction.ALLOW
        assert default_for_no_match(PermissionMode.PLAN) == RuleAction.ALLOW
        assert default_for_no_match(PermissionMode.AUTO) == RuleAction.ALLOW


# ---------------------------------------------------------------------------
# resolve_mode (parsing + legacy flags)
# ---------------------------------------------------------------------------


class TestResolveMode:
    def test_canonical_values(self) -> None:
        assert resolve_mode("bypass") == PermissionMode.BYPASS
        assert resolve_mode("plan") == PermissionMode.PLAN
        assert resolve_mode("auto") == PermissionMode.AUTO
        assert resolve_mode("default") == PermissionMode.DEFAULT

    def test_case_insensitive(self) -> None:
        assert resolve_mode("BYPASS") == PermissionMode.BYPASS
        assert resolve_mode("Plan") == PermissionMode.PLAN

    def test_whitespace_stripped(self) -> None:
        assert resolve_mode("  auto  ") == PermissionMode.AUTO

    def test_none_returns_default(self) -> None:
        assert resolve_mode(None) == PermissionMode.DEFAULT

    def test_unknown_returns_default(self) -> None:
        assert resolve_mode("unknown_mode") == PermissionMode.DEFAULT

    def test_legacy_flags(self) -> None:
        assert resolve_mode("dangerously-skip-permissions") == PermissionMode.BYPASS
        assert resolve_mode("dangerously_skip_permissions") == PermissionMode.BYPASS
        assert resolve_mode("plan_mode") == PermissionMode.PLAN

    def test_legacy_map_complete(self) -> None:
        for key, expected_mode in LEGACY_FLAG_TO_MODE.items():
            assert resolve_mode(key) == expected_mode


# ---------------------------------------------------------------------------
# Integration: PermissionRuleEngine with mode
# ---------------------------------------------------------------------------


class TestEngineWithMode:
    """Test that the engine applies mode-based severity relaxation."""

    def _engine(self) -> PermissionRuleEngine:
        return PermissionRuleEngine(
            rules=[
                PermissionRule(
                    id="critical-deny-force-push",
                    action=RuleAction.DENY,
                    tool="Bash",
                    command="git push *--force*",
                    severity=RuleSeverity.CRITICAL,
                ),
                PermissionRule(
                    id="high-ask-write-config",
                    action=RuleAction.ASK,
                    tool="Write",
                    path="*.yaml",
                    severity=RuleSeverity.HIGH,
                ),
                PermissionRule(
                    id="medium-ask-bash",
                    action=RuleAction.ASK,
                    tool="Bash",
                    severity=RuleSeverity.MEDIUM,
                ),
                PermissionRule(
                    id="low-ask-read",
                    action=RuleAction.ASK,
                    tool="Read",
                    severity=RuleSeverity.LOW,
                ),
            ]
        )

    def test_default_mode_enforces_all(self) -> None:
        engine = self._engine()
        r = engine.evaluate("Bash", {"command": "ls"}, mode=PermissionMode.DEFAULT)
        assert r.action == RuleAction.ASK
        r = engine.evaluate("Read", {}, mode=PermissionMode.DEFAULT)
        assert r.action == RuleAction.ASK

    def test_auto_mode_relaxes_low(self) -> None:
        engine = self._engine()
        # Low severity ask → allow in auto mode
        r = engine.evaluate("Read", {}, mode=PermissionMode.AUTO)
        assert r.action == RuleAction.ALLOW
        # Medium severity ask → still ask in auto mode
        r = engine.evaluate("Bash", {"command": "ls"}, mode=PermissionMode.AUTO)
        assert r.action == RuleAction.ASK

    def test_plan_mode_relaxes_medium_and_low(self) -> None:
        engine = self._engine()
        r = engine.evaluate("Read", {}, mode=PermissionMode.PLAN)
        assert r.action == RuleAction.ALLOW
        r = engine.evaluate("Bash", {"command": "ls"}, mode=PermissionMode.PLAN)
        assert r.action == RuleAction.ALLOW
        # High severity ask → still ask
        r = engine.evaluate("Write", {"file_path": "config.yaml"}, mode=PermissionMode.PLAN)
        assert r.action == RuleAction.ASK

    def test_bypass_mode_only_critical(self) -> None:
        engine = self._engine()
        # Critical deny → still deny in bypass
        r = engine.evaluate("Bash", {"command": "git push origin --force"}, mode=PermissionMode.BYPASS)
        assert r.action == RuleAction.DENY
        # High ask → allow in bypass
        r = engine.evaluate("Write", {"file_path": "config.yaml"}, mode=PermissionMode.BYPASS)
        assert r.action == RuleAction.ALLOW
        # Medium ask → allow in bypass
        r = engine.evaluate("Bash", {"command": "ls"}, mode=PermissionMode.BYPASS)
        assert r.action == RuleAction.ALLOW

    def test_no_mode_passes_original_action(self) -> None:
        engine = self._engine()
        r = engine.evaluate("Read", {})
        assert r.action == RuleAction.ASK  # low severity, but no mode = raw action

    def test_evaluate_to_decision_with_mode(self) -> None:
        engine = self._engine()
        from bernstein.core.policy_engine import DecisionType

        # Low severity in auto mode → relaxed to allow
        decision = engine.evaluate_to_decision("Read", {}, mode=PermissionMode.AUTO)
        assert decision is not None
        assert decision.type == DecisionType.ALLOW


# ---------------------------------------------------------------------------
# YAML loading with severity
# ---------------------------------------------------------------------------


class TestYamlSeverityLoading:
    def test_severity_parsed_from_yaml(self, tmp_path: Path) -> None:
        from bernstein.core.permission_rules import load_permission_rules

        rules_dir = tmp_path / ".bernstein"
        rules_dir.mkdir()
        (rules_dir / "rules.yaml").write_text(
            """\
permission_rules:
  - id: critical-deny
    action: deny
    tool: Bash
    command: "rm -rf /*"
    severity: critical
  - id: low-ask
    action: ask
    tool: Read
    severity: low
  - id: no-severity
    action: ask
    tool: Write
""",
            encoding="utf-8",
        )
        engine = load_permission_rules(tmp_path)
        assert len(engine.rules) == 3
        assert engine.rules[0].severity == RuleSeverity.CRITICAL
        assert engine.rules[1].severity == RuleSeverity.LOW
        assert engine.rules[2].severity == RuleSeverity.MEDIUM  # default

    def test_invalid_severity_defaults_to_medium(self, tmp_path: Path) -> None:
        from bernstein.core.permission_rules import load_permission_rules

        rules_dir = tmp_path / ".bernstein"
        rules_dir.mkdir()
        (rules_dir / "rules.yaml").write_text(
            """\
permission_rules:
  - id: bad-sev
    action: deny
    tool: Bash
    severity: extreme
""",
            encoding="utf-8",
        )
        engine = load_permission_rules(tmp_path)
        assert len(engine.rules) == 1
        assert engine.rules[0].severity == RuleSeverity.MEDIUM
