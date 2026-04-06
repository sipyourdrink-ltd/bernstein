"""Tests for bernstein.core.config_lint (CFG-013)."""

from __future__ import annotations

from bernstein.core.config_lint import LintFinding, LintReport, lint_config


class TestLintFinding:
    def test_to_dict(self) -> None:
        f = LintFinding(rule="test-rule", severity="warning", message="Test msg")
        d = f.to_dict()
        assert d["rule"] == "test-rule"
        assert d["severity"] == "warning"


class TestLintReport:
    def test_has_errors(self) -> None:
        report = LintReport(error_count=1)
        assert report.has_errors

    def test_no_errors(self) -> None:
        report = LintReport()
        assert not report.has_errors

    def test_has_warnings(self) -> None:
        report = LintReport(warning_count=1)
        assert report.has_warnings


class TestLintConfig:
    def test_valid_config_few_findings(self) -> None:
        config = {
            "goal": "Test project",
            "max_agents": 6,
            "budget": "$20",
            "internal_llm_provider": "openrouter_free",
            "quality_gates": {"enabled": True, "tests": True},
        }
        report = lint_config(config)
        assert not report.has_errors

    def test_no_budget_warning(self) -> None:
        config = {"goal": "Test"}
        report = lint_config(config)
        rules = [f.rule for f in report.findings]
        assert "no-budget-set" in rules

    def test_high_max_agents_warning(self) -> None:
        config = {"goal": "Test", "max_agents": 20, "budget": "$50"}
        report = lint_config(config)
        rules = [f.rule for f in report.findings]
        assert "high-max-agents" in rules

    def test_auto_merge_no_gates_error(self) -> None:
        config = {
            "goal": "Test",
            "auto_merge": True,
            "quality_gates": {"enabled": False},
            "budget": "$10",
        }
        report = lint_config(config)
        assert report.has_errors
        rules = [f.rule for f in report.findings]
        assert "auto-merge-no-gates" in rules

    def test_gates_no_tests_info(self) -> None:
        config = {
            "goal": "Test",
            "quality_gates": {"enabled": True, "tests": False},
            "budget": "$10",
        }
        report = lint_config(config)
        rules = [f.rule for f in report.findings]
        assert "gates-no-tests" in rules

    def test_direct_merge_warning(self) -> None:
        config = {"goal": "Test", "merge_strategy": "direct", "budget": "$10"}
        report = lint_config(config)
        rules = [f.rule for f in report.findings]
        assert "direct-merge-risky" in rules

    def test_evolution_no_llm_error(self) -> None:
        config = {
            "goal": "Test",
            "evolution_enabled": True,
            "internal_llm_provider": "none",
            "budget": "$10",
        }
        report = lint_config(config)
        rules = [f.rule for f in report.findings]
        assert "evolution-no-llm" in rules

    def test_missing_goal_error(self) -> None:
        config: dict[str, object] = {"budget": "$10"}
        report = lint_config(config)
        rules = [f.rule for f in report.findings]
        assert "missing-goal" in rules

    def test_single_role_team_info(self) -> None:
        config = {"goal": "Test", "team": ["backend"], "budget": "$10"}
        report = lint_config(config)
        rules = [f.rule for f in report.findings]
        assert "single-role-team" in rules

    def test_to_dict(self) -> None:
        config = {"goal": "Test", "budget": "$10"}
        report = lint_config(config)
        d = report.to_dict()
        assert "findings" in d
        assert isinstance(d["error_count"], int)
