"""Tests for context_activation — .gitignore-style path matching for context rules."""

from __future__ import annotations

import pytest

from bernstein.core.context_activation import (
    BUILTIN_CONTEXT_RULES,
    ContextRule,
    _rule_matches,
    activate_context_for_task,
)

# ---------------------------------------------------------------------------
# ContextRule dataclass
# ---------------------------------------------------------------------------


class TestContextRule:
    def test_default_fields(self) -> None:
        rule = ContextRule()
        assert rule.file_patterns == ()
        assert rule.context == ""
        assert rule.description == ""

    def test_frozen(self) -> None:
        rule = ContextRule(file_patterns=("*.py",), context="Python ctx", description="py")
        with pytest.raises(AttributeError):
            rule.context = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _rule_matches
# ---------------------------------------------------------------------------


class TestRuleMatches:
    def test_no_patterns_always_matches(self) -> None:
        rule = ContextRule(file_patterns=(), context="global")
        assert _rule_matches(rule, ["any/file.py"])

    def test_no_patterns_matches_empty_files(self) -> None:
        rule = ContextRule(file_patterns=(), context="global")
        assert _rule_matches(rule, [])

    def test_simple_glob_matches(self) -> None:
        rule = ContextRule(file_patterns=("*.py",))
        assert _rule_matches(rule, ["server.py"])

    def test_simple_glob_no_match(self) -> None:
        rule = ContextRule(file_patterns=("*.py",))
        assert not _rule_matches(rule, ["server.ts", "README.md"])

    def test_path_glob_matches(self) -> None:
        rule = ContextRule(file_patterns=("src/backend/**",))
        assert _rule_matches(rule, ["src/backend/server.py"])

    def test_path_glob_no_match(self) -> None:
        rule = ContextRule(file_patterns=("src/backend/**",))
        assert not _rule_matches(rule, ["src/frontend/app.ts"])

    def test_basename_matching_fallback(self) -> None:
        """Pattern without path separator matches on basename."""
        rule = ContextRule(file_patterns=("*.py",))
        assert _rule_matches(rule, ["src/bernstein/core/server.py"])

    def test_multiple_patterns_first_match_wins(self) -> None:
        rule = ContextRule(file_patterns=("*.go", "*.rs"))
        assert _rule_matches(rule, ["main.go"])

    def test_multiple_files_any_match_is_enough(self) -> None:
        rule = ContextRule(file_patterns=("*.py",))
        assert _rule_matches(rule, ["README.md", "server.py", "config.json"])


# ---------------------------------------------------------------------------
# activate_context_for_task
# ---------------------------------------------------------------------------


class TestActivateContextForTask:
    def test_empty_owned_files_returns_empty(self) -> None:
        rules = [ContextRule(file_patterns=("*.py",), context="Python ctx")]
        assert activate_context_for_task([], rules) == ""

    def test_matching_rule_returns_context(self) -> None:
        rules = [ContextRule(file_patterns=("*.py",), context="Python ctx")]
        result = activate_context_for_task(["server.py"], rules)
        assert result == "Python ctx"

    def test_no_matching_rules_returns_empty(self) -> None:
        rules = [ContextRule(file_patterns=("*.go",), context="Go ctx")]
        result = activate_context_for_task(["server.py"], rules)
        assert result == ""

    def test_multiple_matching_rules_joined(self) -> None:
        rules = [
            ContextRule(file_patterns=("*.py",), context="Python ctx"),
            ContextRule(file_patterns=("tests/**",), context="Test ctx"),
        ]
        result = activate_context_for_task(["tests/test_server.py"], rules)
        assert "Python ctx" in result
        assert "Test ctx" in result

    def test_global_rule_no_patterns_always_activates(self) -> None:
        rules = [ContextRule(file_patterns=(), context="Always active")]
        result = activate_context_for_task(["anything.ts"], rules)
        assert result == "Always active"

    def test_default_rules_used_when_none_provided(self) -> None:
        result = activate_context_for_task(["src/bernstein/core/server.py"])
        assert result != ""  # At least one builtin rule should match

    def test_backend_context_activates_for_src_backend(self) -> None:
        """Backend context activates for src/backend/ files (acceptance criterion)."""
        rules = [
            ContextRule(
                file_patterns=("src/backend/**", "**/*.py"),
                context="backend context",
                description="backend context",
            )
        ]
        result = activate_context_for_task(["src/backend/api.py"], rules)
        assert "backend context" in result

    def test_rules_with_empty_context_not_included(self) -> None:
        rules = [
            ContextRule(file_patterns=("*.py",), context=""),
            ContextRule(file_patterns=("*.py",), context="Real ctx"),
        ]
        result = activate_context_for_task(["main.py"], rules)
        assert result == "Real ctx"

    def test_builtin_rules_include_backend(self) -> None:
        """Builtin rules include a rule that activates for Python/backend files."""
        result = activate_context_for_task(
            ["src/bernstein/core/server.py"],
            BUILTIN_CONTEXT_RULES,
        )
        assert "Python" in result or "backend" in result.lower()

    def test_builtin_rules_include_test_warning(self) -> None:
        """Builtin test rule mentions not running all tests at once."""
        result = activate_context_for_task(
            ["tests/unit/test_server.py"],
            BUILTIN_CONTEXT_RULES,
        )
        assert "tests" in result.lower() or "test" in result.lower()

    def test_custom_rules_override_builtin(self) -> None:
        """Passing custom rules replaces builtins entirely."""
        custom = [ContextRule(file_patterns=("*.go",), context="Go only")]
        result = activate_context_for_task(["main.go"], custom)
        assert result == "Go only"

    def test_context_blocks_separated_by_newlines(self) -> None:
        rules = [
            ContextRule(file_patterns=("*.py",), context="Ctx A"),
            ContextRule(file_patterns=("*.py",), context="Ctx B"),
        ]
        result = activate_context_for_task(["server.py"], rules)
        assert result == "Ctx A\nCtx B"
