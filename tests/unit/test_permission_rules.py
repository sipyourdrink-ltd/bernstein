"""Tests for rule-based permission engine (permission_rules.py)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.permission_rules import (
    PermissionRule,
    PermissionRuleEngine,
    RuleAction,
    _glob_match,
    _glob_to_regex,
    load_permission_rules,
)
from bernstein.core.policy_engine import DecisionType

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# _glob_match
# ---------------------------------------------------------------------------


class TestGlobMatch:
    def test_exact_match(self) -> None:
        assert _glob_match("Bash", "Bash")

    def test_star_wildcard(self) -> None:
        assert _glob_match("*.py", "foo.py")
        assert not _glob_match("*.py", "foo.txt")

    def test_question_mark(self) -> None:
        assert _glob_match("foo?", "foob")
        assert not _glob_match("foo?", "foobar")

    def test_double_star_deep_path(self) -> None:
        assert _glob_match("src/**", "src/foo/bar/baz.py")
        assert _glob_match("src/**/*.py", "src/deep/nested/file.py")
        assert not _glob_match("src/**", "tests/foo.py")

    def test_case_insensitive(self) -> None:
        assert _glob_match("bash", "Bash", case_insensitive=True)
        assert _glob_match("BASH", "bash", case_insensitive=True)
        assert not _glob_match("bash", "Bash", case_insensitive=False)

    def test_star_matches_any(self) -> None:
        assert _glob_match("*", "anything")
        assert _glob_match("*", "Read")

    def test_command_glob_with_star(self) -> None:
        assert _glob_match("git push *--force*", "git push origin --force")
        assert not _glob_match("git push *--force*", "git push origin main")

    def test_path_with_single_star(self) -> None:
        # Single * should NOT cross path separators in ** mode
        assert _glob_match("src/*.py", "src/foo.py")
        # fnmatch * matches everything including slashes in non-** mode
        # but our ** regex mode treats * as non-separator
        assert not _glob_match("src/**/*.py", "src/foo.txt")


class TestGlobToRegex:
    def test_double_star(self) -> None:
        regex = _glob_to_regex("src/**/*.py")
        assert regex  # Should produce a non-empty regex

    def test_single_star(self) -> None:
        regex = _glob_to_regex("src/*.py")
        # Single star should not match path separators
        assert "[^/]*" in regex

    def test_question_mark(self) -> None:
        regex = _glob_to_regex("foo?")
        assert "[^/]" in regex

    def test_literal_chars_escaped(self) -> None:
        regex = _glob_to_regex("foo.bar")
        assert r"\." in regex

    def test_bracket_passthrough(self) -> None:
        regex = _glob_to_regex("[abc]")
        assert "[abc]" in regex


# ---------------------------------------------------------------------------
# PermissionRule matching
# ---------------------------------------------------------------------------


class TestPermissionRuleMatching:
    def test_tool_only_match(self) -> None:
        engine = PermissionRuleEngine(
            rules=[PermissionRule(id="allow-read", action=RuleAction.ALLOW, tool="Read")]
        )
        result = engine.evaluate("Read", {})
        assert result.matched
        assert result.action == RuleAction.ALLOW
        assert result.rule_id == "allow-read"

    def test_tool_glob_match(self) -> None:
        engine = PermissionRuleEngine(
            rules=[PermissionRule(id="allow-all", action=RuleAction.ALLOW, tool="*")]
        )
        result = engine.evaluate("AnyTool", {"command": "anything"})
        assert result.matched

    def test_tool_case_insensitive(self) -> None:
        engine = PermissionRuleEngine(
            rules=[PermissionRule(id="allow-bash", action=RuleAction.ALLOW, tool="bash")]
        )
        result = engine.evaluate("Bash", {"command": "ls"})
        assert result.matched

    def test_path_match(self) -> None:
        engine = PermissionRuleEngine(
            rules=[
                PermissionRule(
                    id="allow-src",
                    action=RuleAction.ALLOW,
                    tool="Read",
                    path="src/**",
                )
            ]
        )
        assert engine.evaluate("Read", {"file_path": "src/foo/bar.py"}).matched
        assert not engine.evaluate("Read", {"file_path": "tests/test.py"}).matched

    def test_command_match(self) -> None:
        engine = PermissionRuleEngine(
            rules=[
                PermissionRule(
                    id="deny-force-push",
                    action=RuleAction.DENY,
                    tool="Bash",
                    command="git push *--force*",
                )
            ]
        )
        assert engine.evaluate("Bash", {"command": "git push origin --force"}).matched
        assert not engine.evaluate("Bash", {"command": "git push origin main"}).matched

    def test_path_and_tool_must_both_match(self) -> None:
        engine = PermissionRuleEngine(
            rules=[
                PermissionRule(
                    id="allow-write-src",
                    action=RuleAction.ALLOW,
                    tool="Write",
                    path="src/**",
                )
            ]
        )
        # Tool matches, path matches
        assert engine.evaluate("Write", {"file_path": "src/main.py"}).matched
        # Tool doesn't match
        assert not engine.evaluate("Read", {"file_path": "src/main.py"}).matched
        # Path doesn't match
        assert not engine.evaluate("Write", {"file_path": "tests/test.py"}).matched

    def test_no_match_returns_unmatched(self) -> None:
        engine = PermissionRuleEngine(
            rules=[PermissionRule(id="only-read", action=RuleAction.ALLOW, tool="Read")]
        )
        result = engine.evaluate("Write", {"file_path": "foo.py"})
        assert not result.matched
        assert result.rule_id is None

    def test_path_absent_from_input_fails_match(self) -> None:
        engine = PermissionRuleEngine(
            rules=[
                PermissionRule(
                    id="need-path",
                    action=RuleAction.ALLOW,
                    tool="*",
                    path="src/**",
                )
            ]
        )
        # No path field in input → rule requires path so doesn't match
        result = engine.evaluate("Read", {"query": "something"})
        assert not result.matched

    def test_command_absent_from_input_fails_match(self) -> None:
        engine = PermissionRuleEngine(
            rules=[
                PermissionRule(
                    id="need-cmd",
                    action=RuleAction.DENY,
                    tool="Bash",
                    command="rm *",
                )
            ]
        )
        result = engine.evaluate("Bash", {"file_path": "/tmp/x"})
        assert not result.matched

    def test_empty_engine_no_match(self) -> None:
        engine = PermissionRuleEngine()
        result = engine.evaluate("Read", {})
        assert not result.matched


# ---------------------------------------------------------------------------
# First-match-wins precedence
# ---------------------------------------------------------------------------


class TestFirstMatchWins:
    def test_first_rule_wins(self) -> None:
        engine = PermissionRuleEngine(
            rules=[
                PermissionRule(id="deny-all", action=RuleAction.DENY, tool="*"),
                PermissionRule(id="allow-all", action=RuleAction.ALLOW, tool="*"),
            ]
        )
        result = engine.evaluate("Read", {})
        assert result.matched
        assert result.rule_id == "deny-all"
        assert result.action == RuleAction.DENY

    def test_specific_before_general(self) -> None:
        engine = PermissionRuleEngine(
            rules=[
                PermissionRule(
                    id="allow-src-read",
                    action=RuleAction.ALLOW,
                    tool="Read",
                    path="src/**",
                ),
                PermissionRule(
                    id="deny-all-read",
                    action=RuleAction.DENY,
                    tool="Read",
                ),
            ]
        )
        # Specific rule matches first
        r1 = engine.evaluate("Read", {"file_path": "src/foo.py"})
        assert r1.rule_id == "allow-src-read"
        assert r1.action == RuleAction.ALLOW

        # General rule catches the rest
        r2 = engine.evaluate("Read", {"file_path": "secrets/creds.json"})
        assert r2.rule_id == "deny-all-read"
        assert r2.action == RuleAction.DENY

    def test_skip_non_matching_to_reach_later_rule(self) -> None:
        engine = PermissionRuleEngine(
            rules=[
                PermissionRule(
                    id="deny-force-push",
                    action=RuleAction.DENY,
                    tool="Bash",
                    command="git push *--force*",
                ),
                PermissionRule(
                    id="allow-bash",
                    action=RuleAction.ALLOW,
                    tool="Bash",
                ),
            ]
        )
        # Non-force push falls through to the second rule
        result = engine.evaluate("Bash", {"command": "git push origin main"})
        assert result.rule_id == "allow-bash"
        assert result.action == RuleAction.ALLOW


# ---------------------------------------------------------------------------
# evaluate_to_decision
# ---------------------------------------------------------------------------


class TestEvaluateToDecision:
    def test_deny_produces_deny_decision(self) -> None:
        engine = PermissionRuleEngine(
            rules=[PermissionRule(id="d", action=RuleAction.DENY, tool="*")]
        )
        decision = engine.evaluate_to_decision("Write", {})
        assert decision is not None
        assert decision.type == DecisionType.DENY

    def test_ask_produces_ask_decision(self) -> None:
        engine = PermissionRuleEngine(
            rules=[PermissionRule(id="a", action=RuleAction.ASK, tool="*")]
        )
        decision = engine.evaluate_to_decision("Write", {})
        assert decision is not None
        assert decision.type == DecisionType.ASK

    def test_allow_produces_allow_decision(self) -> None:
        engine = PermissionRuleEngine(
            rules=[PermissionRule(id="a", action=RuleAction.ALLOW, tool="*")]
        )
        decision = engine.evaluate_to_decision("Read", {})
        assert decision is not None
        assert decision.type == DecisionType.ALLOW

    def test_no_match_returns_none(self) -> None:
        engine = PermissionRuleEngine()
        decision = engine.evaluate_to_decision("Read", {})
        assert decision is None


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def _write_rules_yaml(tmp_path: Path, content: str) -> None:
    rules_dir = tmp_path / ".bernstein"
    rules_dir.mkdir(exist_ok=True)
    (rules_dir / "rules.yaml").write_text(content, encoding="utf-8")


class TestLoadPermissionRules:
    def test_absent_file_returns_empty_engine(self, tmp_path: Path) -> None:
        engine = load_permission_rules(tmp_path)
        assert engine.rules == []

    def test_no_permission_rules_section(self, tmp_path: Path) -> None:
        _write_rules_yaml(tmp_path, "version: 1\nrules: []\n")
        engine = load_permission_rules(tmp_path)
        assert engine.rules == []

    def test_parses_valid_rules(self, tmp_path: Path) -> None:
        _write_rules_yaml(
            tmp_path,
            """\
version: 1
permission_rules:
  - id: deny-force-push
    action: deny
    tool: Bash
    command: "git push *--force*"
    description: "Block force pushes"
  - id: allow-read-src
    action: allow
    tool: Read
    path: "src/**"
""",
        )
        engine = load_permission_rules(tmp_path)
        assert len(engine.rules) == 2
        assert engine.rules[0].id == "deny-force-push"
        assert engine.rules[0].action == RuleAction.DENY
        assert engine.rules[0].command == "git push *--force*"
        assert engine.rules[1].id == "allow-read-src"
        assert engine.rules[1].action == RuleAction.ALLOW
        assert engine.rules[1].path == "src/**"

    def test_skips_invalid_action(self, tmp_path: Path) -> None:
        _write_rules_yaml(
            tmp_path,
            """\
permission_rules:
  - id: bad
    action: explode
    tool: Bash
""",
        )
        engine = load_permission_rules(tmp_path)
        assert engine.rules == []

    def test_skips_missing_id(self, tmp_path: Path) -> None:
        _write_rules_yaml(
            tmp_path,
            """\
permission_rules:
  - action: deny
    tool: Bash
""",
        )
        engine = load_permission_rules(tmp_path)
        assert engine.rules == []

    def test_default_tool_is_star(self, tmp_path: Path) -> None:
        _write_rules_yaml(
            tmp_path,
            """\
permission_rules:
  - id: deny-all
    action: deny
""",
        )
        engine = load_permission_rules(tmp_path)
        assert engine.rules[0].tool == "*"

    def test_malformed_yaml_returns_empty(self, tmp_path: Path) -> None:
        rules_dir = tmp_path / ".bernstein"
        rules_dir.mkdir()
        (rules_dir / "rules.yaml").write_text(
            "just a string\n", encoding="utf-8"
        )
        engine = load_permission_rules(tmp_path)
        assert engine.rules == []

    def test_permission_rules_not_a_list(self, tmp_path: Path) -> None:
        _write_rules_yaml(
            tmp_path,
            "permission_rules: not-a-list\n",
        )
        engine = load_permission_rules(tmp_path)
        assert engine.rules == []

    def test_loaded_engine_evaluates(self, tmp_path: Path) -> None:
        _write_rules_yaml(
            tmp_path,
            """\
permission_rules:
  - id: allow-read
    action: allow
    tool: Read
  - id: ask-default
    action: ask
    tool: "*"
""",
        )
        engine = load_permission_rules(tmp_path)
        r1 = engine.evaluate("Read", {})
        assert r1.action == RuleAction.ALLOW
        r2 = engine.evaluate("Write", {"file_path": "foo.py"})
        assert r2.action == RuleAction.ASK


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_tool_input(self) -> None:
        engine = PermissionRuleEngine(
            rules=[PermissionRule(id="a", action=RuleAction.ALLOW, tool="*")]
        )
        assert engine.evaluate("Read", {}).matched

    def test_path_field_alternatives(self) -> None:
        """Test that path, file_path, and filepath are all checked."""
        engine = PermissionRuleEngine(
            rules=[
                PermissionRule(
                    id="a", action=RuleAction.ALLOW, tool="*", path="src/*"
                )
            ]
        )
        assert engine.evaluate("X", {"path": "src/a.py"}).matched
        assert engine.evaluate("X", {"file_path": "src/b.py"}).matched
        assert engine.evaluate("X", {"filepath": "src/c.py"}).matched
        assert not engine.evaluate("X", {"other": "src/d.py"}).matched

    def test_overlapping_path_and_command(self) -> None:
        """Both path and command specified — both must match."""
        engine = PermissionRuleEngine(
            rules=[
                PermissionRule(
                    id="special",
                    action=RuleAction.DENY,
                    tool="Bash",
                    path="src/**",
                    command="rm *",
                )
            ]
        )
        # Both match
        assert engine.evaluate(
            "Bash", {"file_path": "src/x.py", "command": "rm foo"}
        ).matched
        # Only command matches
        assert not engine.evaluate(
            "Bash", {"file_path": "tests/x.py", "command": "rm foo"}
        ).matched
        # Only path matches
        assert not engine.evaluate(
            "Bash", {"file_path": "src/x.py", "command": "ls"}
        ).matched

    def test_description_in_reason(self) -> None:
        engine = PermissionRuleEngine(
            rules=[
                PermissionRule(
                    id="r",
                    action=RuleAction.DENY,
                    tool="*",
                    description="No soup for you",
                )
            ]
        )
        result = engine.evaluate("X", {})
        assert "No soup for you" in result.reason
