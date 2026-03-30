"""Tests for organizational rule enforcement (rule_enforcer.py)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.rule_enforcer import (
    RulesConfig,
    RuleSpec,
    _check_command,
    _check_forbidden_pattern,
    _check_required_file,
    _parse_diff_additions,
    get_rule_violation_stats,
    load_rules_config,
    run_rule_enforcement,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(*, id: str = "T-001", role: str = "backend") -> Task:
    return Task(
        id=id,
        title="Test task",
        description="Do something.",
        role=role,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
    )


_SAMPLE_DIFF = """\
diff --git a/src/foo.py b/src/foo.py
index abc..def 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,3 +1,5 @@
 import logging
+print("debug output")
+logger = logging.getLogger(__name__)
 def foo():
-    pass
+    print("inside foo")
diff --git a/tests/test_foo.py b/tests/test_foo.py
index 111..222 100644
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -1,2 +1,3 @@
 def test_foo():
+    print("test output")
     assert True
"""


def _write_rules_yaml(tmp_path: Path, content: str) -> None:
    rules_dir = tmp_path / ".bernstein"
    rules_dir.mkdir()
    (rules_dir / "rules.yaml").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# _parse_diff_additions
# ---------------------------------------------------------------------------


class TestParseDiffAdditions:
    def test_extracts_added_lines(self) -> None:
        additions = _parse_diff_additions(_SAMPLE_DIFF, None)
        assert "src/foo.py" in additions
        added = additions["src/foo.py"]
        assert any("print" in ln for ln in added)

    def test_excludes_file_header_lines(self) -> None:
        additions = _parse_diff_additions(_SAMPLE_DIFF, None)
        for lines in additions.values():
            assert not any(ln.startswith("+") for ln in lines)

    def test_glob_filter_restricts_files(self) -> None:
        additions = _parse_diff_additions(_SAMPLE_DIFF, "src/*.py")
        assert "src/foo.py" in additions
        assert "tests/test_foo.py" not in additions

    def test_empty_diff_returns_empty(self) -> None:
        assert _parse_diff_additions("", None) == {}


# ---------------------------------------------------------------------------
# _check_forbidden_pattern
# ---------------------------------------------------------------------------


class TestCheckForbiddenPattern:
    def test_detects_violation_in_additions(self) -> None:
        rule = RuleSpec(id="no-print", type="forbidden_pattern", pattern=r"\bprint\s*\(")
        violation = _check_forbidden_pattern(rule, _SAMPLE_DIFF)
        assert violation is not None
        assert violation.rule_id == "no-print"
        assert violation.blocked  # default severity=error

    def test_no_violation_when_pattern_absent(self) -> None:
        rule = RuleSpec(id="no-todo", type="forbidden_pattern", pattern=r"\bTODO\b")
        violation = _check_forbidden_pattern(rule, _SAMPLE_DIFF)
        assert violation is None

    def test_excludes_matching_files(self) -> None:
        rule = RuleSpec(
            id="no-print-prod",
            type="forbidden_pattern",
            pattern=r"\bprint\s*\(",
            exclude=["src/**", "tests/**"],
        )
        violation = _check_forbidden_pattern(rule, _SAMPLE_DIFF)
        assert violation is None

    def test_file_glob_restricts_check(self) -> None:
        # Pattern matches only in src/foo.py; restrict to tests/**
        rule = RuleSpec(
            id="no-print-tests",
            type="forbidden_pattern",
            pattern=r"\bprint\s*\(",
            files="tests/*.py",
        )
        violation = _check_forbidden_pattern(rule, _SAMPLE_DIFF)
        assert violation is not None
        assert violation.files == ["tests/test_foo.py"]

    def test_warning_severity_not_blocked(self) -> None:
        rule = RuleSpec(
            id="warn-print",
            type="forbidden_pattern",
            pattern=r"\bprint\s*\(",
            severity="warning",
        )
        violation = _check_forbidden_pattern(rule, _SAMPLE_DIFF)
        assert violation is not None
        assert not violation.blocked

    def test_custom_message_in_fix_hint(self) -> None:
        rule = RuleSpec(
            id="no-print",
            type="forbidden_pattern",
            pattern=r"\bprint\s*\(",
            message="Use logger.info() instead",
        )
        violation = _check_forbidden_pattern(rule, _SAMPLE_DIFF)
        assert violation is not None
        assert "Use logger.info()" in violation.fix_hint

    def test_invalid_regex_returns_none(self) -> None:
        rule = RuleSpec(id="bad-regex", type="forbidden_pattern", pattern="[invalid")
        violation = _check_forbidden_pattern(rule, _SAMPLE_DIFF)
        assert violation is None

    def test_missing_pattern_returns_none(self) -> None:
        rule = RuleSpec(id="no-pattern", type="forbidden_pattern")
        violation = _check_forbidden_pattern(rule, _SAMPLE_DIFF)
        assert violation is None

    def test_empty_diff_no_violation(self) -> None:
        rule = RuleSpec(id="no-print", type="forbidden_pattern", pattern=r"\bprint\s*\(")
        violation = _check_forbidden_pattern(rule, "")
        assert violation is None


# ---------------------------------------------------------------------------
# _check_required_file
# ---------------------------------------------------------------------------


class TestCheckRequiredFile:
    def test_file_exists_passes(self, tmp_path: Path) -> None:
        (tmp_path / "CHANGELOG.md").write_text("# Changelog")
        rule = RuleSpec(id="changelog", type="required_file", path="CHANGELOG.md")
        assert _check_required_file(rule, tmp_path) is None

    def test_file_absent_returns_violation(self, tmp_path: Path) -> None:
        rule = RuleSpec(id="changelog", type="required_file", path="CHANGELOG.md")
        violation = _check_required_file(rule, tmp_path)
        assert violation is not None
        assert violation.rule_id == "changelog"
        assert violation.blocked  # default severity=error

    def test_warning_severity_not_blocked(self, tmp_path: Path) -> None:
        rule = RuleSpec(
            id="changelog-warn",
            type="required_file",
            path="CHANGELOG.md",
            severity="warning",
        )
        violation = _check_required_file(rule, tmp_path)
        assert violation is not None
        assert not violation.blocked

    def test_missing_path_returns_none(self, tmp_path: Path) -> None:
        rule = RuleSpec(id="no-path", type="required_file")
        assert _check_required_file(rule, tmp_path) is None


# ---------------------------------------------------------------------------
# _check_command
# ---------------------------------------------------------------------------


class TestCheckCommand:
    def test_exit_zero_passes(self, tmp_path: Path) -> None:
        rule = RuleSpec(id="ok-cmd", type="command", command="exit 0")
        assert _check_command(rule, tmp_path) is None

    def test_nonzero_exit_returns_violation(self, tmp_path: Path) -> None:
        rule = RuleSpec(id="fail-cmd", type="command", command="exit 1")
        violation = _check_command(rule, tmp_path)
        assert violation is not None
        assert violation.rule_id == "fail-cmd"
        assert violation.blocked

    def test_warning_severity_not_blocked(self, tmp_path: Path) -> None:
        rule = RuleSpec(id="warn-cmd", type="command", command="exit 1", severity="warning")
        violation = _check_command(rule, tmp_path)
        assert violation is not None
        assert not violation.blocked

    def test_timeout_returns_violation(self, tmp_path: Path) -> None:
        rule = RuleSpec(id="slow-cmd", type="command", command="sleep 60")
        violation = _check_command(rule, tmp_path, timeout_s=1)
        assert violation is not None
        assert "timed out" in violation.detail.lower()

    def test_missing_command_returns_none(self, tmp_path: Path) -> None:
        rule = RuleSpec(id="no-cmd", type="command")
        assert _check_command(rule, tmp_path) is None


# ---------------------------------------------------------------------------
# load_rules_config
# ---------------------------------------------------------------------------


class TestLoadRulesConfig:
    def test_absent_file_returns_none(self, tmp_path: Path) -> None:
        assert load_rules_config(tmp_path) is None

    def test_parses_valid_config(self, tmp_path: Path) -> None:
        _write_rules_yaml(
            tmp_path,
            """\
version: 1
enabled: true
rules:
  - id: no-print
    type: forbidden_pattern
    pattern: 'print\\('
    severity: error
""",
        )
        config = load_rules_config(tmp_path)
        assert config is not None
        assert config.enabled
        assert len(config.rules) == 1
        assert config.rules[0].id == "no-print"

    def test_disabled_master_switch(self, tmp_path: Path) -> None:
        _write_rules_yaml(tmp_path, "version: 1\nenabled: false\nrules: []\n")
        config = load_rules_config(tmp_path)
        assert config is not None
        assert not config.enabled

    def test_skips_rules_without_id(self, tmp_path: Path) -> None:
        _write_rules_yaml(
            tmp_path,
            "version: 1\nrules:\n  - type: forbidden_pattern\n    pattern: x\n",
        )
        config = load_rules_config(tmp_path)
        assert config is not None
        assert config.rules == []

    def test_malformed_yaml_returns_none(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bernstein"
        rules_dir.mkdir()
        (rules_dir / "rules.yaml").write_text("just a string, not a mapping\n", encoding="utf-8")
        assert load_rules_config(tmp_path) is None

    def test_parses_exclude_list(self, tmp_path: Path) -> None:
        _write_rules_yaml(
            tmp_path,
            """\
version: 1
rules:
  - id: no-print
    type: forbidden_pattern
    pattern: 'print'
    exclude:
      - "tests/**"
      - "scripts/**"
""",
        )
        config = load_rules_config(tmp_path)
        assert config is not None
        assert config.rules[0].exclude == ["tests/**", "scripts/**"]


# ---------------------------------------------------------------------------
# run_rule_enforcement
# ---------------------------------------------------------------------------


class TestRunRuleEnforcement:
    def test_disabled_config_passes(self, tmp_path: Path) -> None:
        config = RulesConfig(enabled=False, rules=[])
        task = _make_task()
        result = run_rule_enforcement(task, tmp_path, tmp_path, config)
        assert result.passed
        assert result.violations == []

    def test_empty_rules_passes(self, tmp_path: Path) -> None:
        config = RulesConfig(rules=[])
        task = _make_task()
        result = run_rule_enforcement(task, tmp_path, tmp_path, config)
        assert result.passed

    def test_no_violations_passes(self, tmp_path: Path) -> None:
        config = RulesConfig(rules=[RuleSpec(id="no-todo", type="forbidden_pattern", pattern=r"\bTODO\b")])
        task = _make_task()
        # No diff to inspect (empty repo or no changes)
        result = run_rule_enforcement(task, tmp_path, tmp_path, config)
        assert result.passed

    def test_required_file_violation_blocks(self, tmp_path: Path) -> None:
        config = RulesConfig(rules=[RuleSpec(id="need-changelog", type="required_file", path="CHANGELOG.md")])
        task = _make_task()
        result = run_rule_enforcement(task, tmp_path, tmp_path, config)
        assert not result.passed
        assert len(result.violations) == 1
        assert result.violations[0].rule_id == "need-changelog"

    def test_warning_only_does_not_fail_overall(self, tmp_path: Path) -> None:
        config = RulesConfig(
            rules=[
                RuleSpec(
                    id="warn-changelog",
                    type="required_file",
                    path="CHANGELOG.md",
                    severity="warning",
                )
            ]
        )
        task = _make_task()
        result = run_rule_enforcement(task, tmp_path, tmp_path, config)
        assert result.passed  # warning doesn't block
        assert len(result.violations) == 1
        assert not result.violations[0].blocked

    def test_unknown_rule_type_skipped(self, tmp_path: Path) -> None:
        config = RulesConfig(rules=[RuleSpec(id="weird", type="unknown_type")])
        task = _make_task()
        result = run_rule_enforcement(task, tmp_path, tmp_path, config)
        assert result.passed
        assert result.violations == []

    def test_command_rule_blocks_on_failure(self, tmp_path: Path) -> None:
        config = RulesConfig(rules=[RuleSpec(id="fail-cmd", type="command", command="exit 1")])
        task = _make_task()
        result = run_rule_enforcement(task, tmp_path, tmp_path, config)
        assert not result.passed

    def test_multiple_rules_all_evaluated(self, tmp_path: Path) -> None:
        # Both rules fail — both violations should be collected
        config = RulesConfig(
            rules=[
                RuleSpec(id="need-a", type="required_file", path="a.txt"),
                RuleSpec(id="need-b", type="required_file", path="b.txt"),
            ]
        )
        task = _make_task()
        result = run_rule_enforcement(task, tmp_path, tmp_path, config)
        assert not result.passed
        assert len(result.violations) == 2


# ---------------------------------------------------------------------------
# Metrics recording
# ---------------------------------------------------------------------------


class TestGetRuleViolationStats:
    def test_absent_metrics_file_returns_zeros(self, tmp_path: Path) -> None:
        stats = get_rule_violation_stats(tmp_path)
        assert stats["total"] == 0
        assert stats["blocked"] == 0

    def test_counts_events_correctly(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        events = [
            {"task_id": "T1", "rule_id": "r1", "result": "blocked"},
            {"task_id": "T1", "rule_id": "r2", "result": "flagged"},
            {"task_id": "T2", "rule_id": "r1", "result": "blocked"},
        ]
        with open(metrics_dir / "rule_violations.jsonl", "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

        stats = get_rule_violation_stats(tmp_path)
        assert stats["total"] == 3
        assert stats["blocked"] == 2
        assert stats["flagged"] == 1
        assert stats["by_rule"]["r1"]["blocked"] == 2
        assert stats["by_rule"]["r2"]["flagged"] == 1

    def test_skips_malformed_lines(self, tmp_path: Path) -> None:
        metrics_dir = tmp_path / ".sdd" / "metrics"
        metrics_dir.mkdir(parents=True)
        with open(metrics_dir / "rule_violations.jsonl", "w") as f:
            f.write("not-json\n")
            f.write(json.dumps({"rule_id": "r1", "result": "blocked"}) + "\n")
        stats = get_rule_violation_stats(tmp_path)
        assert stats["total"] == 1
