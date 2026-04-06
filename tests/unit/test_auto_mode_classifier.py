"""Tests for SEC-010: Auto-mode classifier adjusting strictness."""

from __future__ import annotations

from bernstein.core.auto_mode_classifier import (
    AutoModeClassifier,
    OperationContext,
    StrictnessLevel,
)


class TestStrictnessLevel:
    def test_ordering(self) -> None:
        assert StrictnessLevel.MINIMAL < StrictnessLevel.LOW
        assert StrictnessLevel.LOW < StrictnessLevel.MEDIUM
        assert StrictnessLevel.MEDIUM < StrictnessLevel.HIGH
        assert StrictnessLevel.HIGH < StrictnessLevel.MAXIMUM


class TestAutoModeClassifier:
    def test_read_only_action_lowers_strictness(self) -> None:
        classifier = AutoModeClassifier()
        result = classifier.classify(OperationContext(action="read"))
        assert result.level == StrictnessLevel.MINIMAL

    def test_safe_write_slightly_lowers(self) -> None:
        classifier = AutoModeClassifier()
        result = classifier.classify(OperationContext(action="write"))
        assert result.level == StrictnessLevel.LOW

    def test_destructive_command_raises_strictness(self) -> None:
        classifier = AutoModeClassifier()
        result = classifier.classify(OperationContext(action="bash", command="rm -rf /tmp/project"))
        assert result.level == StrictnessLevel.MAXIMUM

    def test_network_command_raises_strictness(self) -> None:
        classifier = AutoModeClassifier()
        result = classifier.classify(OperationContext(action="bash", command="curl https://example.com"))
        assert result.level == StrictnessLevel.HIGH

    def test_small_scope_increases_strictness(self) -> None:
        classifier = AutoModeClassifier()
        result = classifier.classify(OperationContext(action="bash", scope="small"))
        assert result.level >= StrictnessLevel.HIGH

    def test_large_scope_lowers_strictness(self) -> None:
        classifier = AutoModeClassifier()
        result = classifier.classify(OperationContext(action="bash", scope="large"))
        assert result.level <= StrictnessLevel.LOW

    def test_sandbox_discount(self) -> None:
        classifier = AutoModeClassifier()
        without_sandbox = classifier.classify(OperationContext(action="bash"))
        with_sandbox = classifier.classify(OperationContext(action="bash", is_sandbox=True))
        assert with_sandbox.level <= without_sandbox.level

    def test_custom_base_level(self) -> None:
        classifier = AutoModeClassifier(base_level=StrictnessLevel.HIGH)
        result = classifier.classify(OperationContext(action="bash"))
        assert result.level == StrictnessLevel.HIGH

    def test_factors_populated(self) -> None:
        classifier = AutoModeClassifier()
        result = classifier.classify(OperationContext(action="read", is_sandbox=True))
        assert len(result.factors) > 0

    def test_clamped_to_minimum(self) -> None:
        classifier = AutoModeClassifier(base_level=StrictnessLevel.MINIMAL)
        result = classifier.classify(OperationContext(action="read", is_sandbox=True))
        assert result.level == StrictnessLevel.MINIMAL

    def test_clamped_to_maximum(self) -> None:
        classifier = AutoModeClassifier(base_level=StrictnessLevel.MAXIMUM)
        result = classifier.classify(
            OperationContext(
                action="bash",
                command="sudo rm -rf /",
                scope="small",
            )
        )
        assert result.level == StrictnessLevel.MAXIMUM

    def test_sql_drop_is_destructive(self) -> None:
        classifier = AutoModeClassifier()
        result = classifier.classify(OperationContext(action="bash", command="psql -c 'DROP TABLE users'"))
        assert result.level >= StrictnessLevel.MAXIMUM

    def test_git_force_push_is_destructive(self) -> None:
        classifier = AutoModeClassifier()
        result = classifier.classify(OperationContext(action="bash", command="git push --force origin main"))
        assert result.level >= StrictnessLevel.MAXIMUM

    def test_unknown_action_default(self) -> None:
        classifier = AutoModeClassifier()
        result = classifier.classify(OperationContext(action="custom_action"))
        assert result.level == StrictnessLevel.MEDIUM
