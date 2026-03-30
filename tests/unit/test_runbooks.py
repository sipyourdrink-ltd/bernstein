"""Tests for bernstein.core.runbooks — Runbook automation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.runbooks import RunbookEngine, RunbookRule

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# RunbookRule
# ---------------------------------------------------------------------------


class TestRunbookRule:
    def test_matches_import_error(self) -> None:
        rule = RunbookRule(
            name="import_error",
            detect=r"ModuleNotFoundError: No module named '(\S+)'",
            action="pip install {module}",
        )
        m = rule.matches("ModuleNotFoundError: No module named 'requests'")
        assert m is not None
        assert m.group(1) == "requests"

    def test_no_match(self) -> None:
        rule = RunbookRule(
            name="import_error",
            detect=r"ModuleNotFoundError: No module named '(\S+)'",
            action="pip install {module}",
        )
        assert rule.matches("Everything is fine") is None

    def test_matches_lint_failure(self) -> None:
        rule = RunbookRule(
            name="lint_failure",
            detect=r"ruff check failed|Ruff.*error|ruff.*Found \d+ error",
            action="ruff check --fix .",
        )
        assert rule.matches("ruff check failed") is not None
        assert rule.matches("ruff: Found 3 errors") is not None

    def test_matches_port_conflict(self) -> None:
        rule = RunbookRule(
            name="port_conflict",
            detect=r"Address already in use|EADDRINUSE.*:(\d+)|port (\d+).*in use",
            action="lsof -ti:{port} | xargs kill -9",
        )
        assert rule.matches("Address already in use") is not None

    def test_matches_rate_limit(self) -> None:
        rule = RunbookRule(
            name="rate_limit",
            detect=r"rate.?limit|429|Too Many Requests|throttl",
            action="Wait and retry",
        )
        assert rule.matches("HTTP 429 Too Many Requests") is not None
        assert rule.matches("rate_limit exceeded") is not None
        assert rule.matches("throttled by provider") is not None


# ---------------------------------------------------------------------------
# RunbookMatch
# ---------------------------------------------------------------------------


class TestRunbookMatch:
    def test_extracted_value(self) -> None:
        rule = RunbookRule(
            name="import_error",
            detect=r"ModuleNotFoundError: No module named '(\S+)'",
            action="pip install {module}",
        )
        m = rule.matches("ModuleNotFoundError: No module named 'flask'")
        assert m is not None
        from bernstein.core.runbooks import RunbookMatch

        match = RunbookMatch(rule=rule, match=m)
        assert match.extracted_value == "flask"
        assert match.interpolated_action == "pip install flask"

    def test_no_capture_group(self) -> None:
        rule = RunbookRule(
            name="lint_failure",
            detect=r"ruff check failed",
            action="ruff check --fix .",
        )
        m = rule.matches("ruff check failed")
        assert m is not None
        from bernstein.core.runbooks import RunbookMatch

        match = RunbookMatch(rule=rule, match=m)
        assert match.extracted_value is None
        assert match.interpolated_action == "ruff check --fix ."


# ---------------------------------------------------------------------------
# RunbookEngine
# ---------------------------------------------------------------------------


class TestRunbookEngine:
    def test_default_rules_loaded(self) -> None:
        engine = RunbookEngine()
        assert len(engine.rules) > 5  # We have 10 default rules

    def test_match_finds_first_matching_rule(self) -> None:
        engine = RunbookEngine()
        result = engine.match("ModuleNotFoundError: No module named 'numpy'")
        assert result is not None
        assert result.rule.name == "import_error"

    def test_match_returns_none_for_unknown_error(self) -> None:
        engine = RunbookEngine()
        assert engine.match("Everything is working perfectly") is None

    def test_record_execution(self) -> None:
        engine = RunbookEngine()
        engine.record_execution(
            rule_name="import_error",
            task_id="T-001",
            action="pip install numpy",
            success=True,
        )
        assert len(engine.executions) == 1
        assert engine.executions[0].rule_name == "import_error"

    def test_get_stats(self) -> None:
        engine = RunbookEngine()
        engine.record_execution("import_error", "T-001", "pip install numpy", success=True)
        engine.record_execution("import_error", "T-002", "pip install flask", success=False)
        engine.record_execution("lint_failure", "T-003", "ruff check --fix", success=True)

        stats = engine.get_stats()
        assert stats["total_executions"] == 3
        assert stats["by_rule"]["import_error"]["total"] == 2
        assert stats["by_rule"]["import_error"]["success"] == 1
        assert stats["by_rule"]["lint_failure"]["total"] == 1

    def test_save_clears_in_memory(self, tmp_path: Path) -> None:
        engine = RunbookEngine()
        engine.record_execution("test", "T-001", "do thing", success=True)
        assert len(engine.executions) == 1
        engine.save(tmp_path)
        assert len(engine.executions) == 0
        # Check file written
        log_path = tmp_path / "runbook_log.jsonl"
        assert log_path.exists()
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["rule_name"] == "test"

    def test_load_rules_from_config(self, tmp_path: Path) -> None:
        config = {
            "runbooks": [
                {
                    "name": "custom_rule",
                    "detect": r"CustomError: (\w+)",
                    "action": "fix {module}",
                    "auto_execute": True,
                    "max_retries": 3,
                }
            ]
        }
        config_path = tmp_path / "runbooks.json"
        config_path.write_text(json.dumps(config))

        rules = RunbookEngine.load_rules(config_path)
        assert len(rules) == 1
        assert rules[0].name == "custom_rule"
        assert rules[0].auto_execute is True

    def test_load_rules_fallback_to_defaults(self, tmp_path: Path) -> None:
        rules = RunbookEngine.load_rules(tmp_path / "nonexistent.json")
        assert len(rules) > 5  # Should load defaults
