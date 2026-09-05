"""Tests for bernstein.core.quality.gate_pipeline."""

from __future__ import annotations

import pytest

from bernstein.core.quality.gate_pipeline import (
    INCONCLUSIVE_REASONS,
    LEGACY_PYTHON_CONDITION,
    VALID_GATE_CONDITIONS,
    VALID_GATE_NAMES,
    GatePipelineStep,
    GateReport,
    GateResult,
    VerificationScope,
    build_default_pipeline,
    is_dep_file,
    normalize_gate_condition,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_inconclusive_reasons_is_a_frozenset(self) -> None:
        assert isinstance(INCONCLUSIVE_REASONS, frozenset)
        assert len(INCONCLUSIVE_REASONS) > 0

    def test_inconclusive_reasons_contains_expected_members(self) -> None:
        assert "evidence-missing" in INCONCLUSIVE_REASONS
        assert "evidence-unreadable" in INCONCLUSIVE_REASONS
        assert "runner-died-before-output" in INCONCLUSIVE_REASONS
        assert "command-not-found" in INCONCLUSIVE_REASONS

    def test_valid_gate_names_is_frozenset(self) -> None:
        assert isinstance(VALID_GATE_NAMES, frozenset)

    def test_valid_gate_names_contains_expected_gates(self) -> None:
        assert "lint" in VALID_GATE_NAMES
        assert "type_check" in VALID_GATE_NAMES
        assert "tests" in VALID_GATE_NAMES
        assert "coverage_delta" in VALID_GATE_NAMES
        assert "incident_evals" in VALID_GATE_NAMES

    def test_valid_gate_conditions_is_frozenset(self) -> None:
        assert isinstance(VALID_GATE_CONDITIONS, frozenset)

    def test_valid_gate_conditions_contains_all_conditions(self) -> None:
        for cond in ["always", "python_changed", "tests_changed", "any_changed", "deps_changed"]:
            assert cond in VALID_GATE_CONDITIONS


# ---------------------------------------------------------------------------
# is_dep_file
# ---------------------------------------------------------------------------


class TestIsDepFile:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("pyproject.toml", True),
            ("setup.py", True),
            ("setup.cfg", True),
            ("Pipfile", True),
            ("poetry.lock", True),
            ("uv.lock", True),
            ("requirements.txt", True),
            ("requirements-dev.txt", True),
            ("requirements-prod.txt", True),
            ("src/bernstein/pyproject.toml", True),
            ("src/bernstein/setup.cfg", True),
            ("src/foo/requirements-test.txt", True),
            ("src/bernstein/main.py", False),
            ("src/bernstein/core/quality/gate_pipeline.py", False),
            ("README.md", False),
            ("tests/unit/test_foo.py", False),
        ],
    )
    def test_is_dep_file(self, path: str, expected: bool) -> None:
        assert is_dep_file(path) is expected


# ---------------------------------------------------------------------------
# normalize_gate_condition
# ---------------------------------------------------------------------------


class TestNormalizeGateCondition:
    def test_python_changed(self) -> None:
        assert normalize_gate_condition("python_changed") == "python_changed"

    def test_any_changed(self) -> None:
        assert normalize_gate_condition("any_changed") == "any_changed"

    def test_deps_changed(self) -> None:
        assert normalize_gate_condition("deps_changed") == "deps_changed"

    def test_tests_changed(self) -> None:
        assert normalize_gate_condition("tests_changed") == "tests_changed"

    def test_always(self) -> None:
        assert normalize_gate_condition("always") == "always"

    def test_legacy_python_condition_normalized(self) -> None:
        assert normalize_gate_condition(LEGACY_PYTHON_CONDITION) == "python_changed"

    def test_whitespace_stripped(self) -> None:
        assert normalize_gate_condition("  python_changed  ") == "python_changed"

    def test_unsupported_condition_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported gate condition"):
            normalize_gate_condition("never")


# ---------------------------------------------------------------------------
# VerificationScope
# ---------------------------------------------------------------------------


class TestVerificationScope:
    def test_basic_construction(self) -> None:
        scope = VerificationScope(
            oracle_id="ruff-0.5.0",
            kind="lint",
            checked=("src/foo.py", "src/bar.py"),
            cannot_check=("generated.py",),
        )
        assert scope.oracle_id == "ruff-0.5.0"
        assert scope.kind == "lint"
        assert scope.checked == ("src/foo.py", "src/bar.py")
        assert scope.cannot_check == ("generated.py",)

    def test_empty_tuples(self) -> None:
        scope = VerificationScope(oracle_id=None, kind=None, checked=(), cannot_check=())
        assert scope.checked == ()
        assert scope.cannot_check == ()


# ---------------------------------------------------------------------------
# GateResult – status ↔ reason invariant
# ---------------------------------------------------------------------------


class TestGateResultReasonInvariant:
    def test_inconclusive_requires_reason(self) -> None:
        with pytest.raises(ValueError, match="inconclusive.*requires a reason"):
            GateResult(
                name="lint",
                status="inconclusive",
                required=True,
                blocked=False,
                cached=False,
                duration_ms=100,
                details="",
                reason=None,
            )

    def test_inconclusive_reason_not_in_closed_set(self) -> None:
        with pytest.raises(ValueError, match="not in the closed set INCONCLUSIVE_REASONS"):
            GateResult(
                name="lint",
                status="inconclusive",
                required=True,
                blocked=False,
                cached=False,
                duration_ms=100,
                details="",
                reason="not-a-real-reason",
            )

    @pytest.mark.parametrize("status", ["pass", "fail", "warn", "timeout", "skipped", "bypassed", "command_not_found"])
    def test_non_inconclusive_rejects_reason(self, status: str) -> None:
        with pytest.raises(ValueError, match="reason=.*only valid with status='inconclusive'"):
            GateResult(
                name="lint",
                status=status,
                required=True,
                blocked=False,
                cached=False,
                duration_ms=100,
                details="",
                reason="evidence-missing",
            )


# ---------------------------------------------------------------------------
# GateResult – scope is optional (was previously required for non-skipped/bypassed)
# ---------------------------------------------------------------------------


class TestGateResultScopeOptional:
    """Scope is no longer enforced by GateResult.__post_init__.

    Issue #5397 slice 1: the field was added so the type is stable.
    Enforcement (requiring scope for non-skipped/non-bypassed statuses) was
    removed — the docstring was updated and the ValueError in __post_init__
    that enforced it was removed. These tests prove that GateResult accepts
    any status without a scope, which was the behavioral change.
    """

    @pytest.mark.parametrize(
        "status",
        ["pass", "fail", "warn", "timeout", "skipped", "bypassed", "inconclusive", "command_not_found"],
    )
    def test_gate_result_accepts_any_status_without_scope(self, status: str) -> None:
        reason = "evidence-missing" if status == "inconclusive" else None
        result = GateResult(
            name="lint",
            status=status,
            required=True,
            blocked=False,
            cached=False,
            duration_ms=100,
            details="details here",
            reason=reason,
            scope=None,
        )
        assert result.name == "lint"
        assert result.status == status
        assert result.scope is None

    def test_scope_can_still_be_provided(self) -> None:
        scope = VerificationScope(oracle_id="mypy", kind="type_check", checked=("src/foo.py",), cannot_check=())
        result = GateResult(
            name="type_check",
            status="pass",
            required=True,
            blocked=False,
            cached=False,
            duration_ms=500,
            details="",
            scope=scope,
        )
        assert result.scope is scope

    def test_all_inconclusive_reasons_work(self) -> None:
        for reason in sorted(INCONCLUSIVE_REASONS):
            result = GateResult(
                name="lint",
                status="inconclusive",
                required=True,
                blocked=False,
                cached=False,
                duration_ms=100,
                details="",
                reason=reason,
                scope=None,
            )
            assert result.reason == reason


# ---------------------------------------------------------------------------
# GatePipelineStep
# ---------------------------------------------------------------------------


class TestGatePipelineStep:
    def test_basic_construction(self) -> None:
        step = GatePipelineStep(name="lint", required=True, condition="always")
        assert step.name == "lint"
        assert step.required is True
        assert step.condition == "always"

    def test_command_override_optional(self) -> None:
        step = GatePipelineStep(name="lint", required=False, command_override=None)
        assert step.command_override is None

    def test_command_override_set(self) -> None:
        step = GatePipelineStep(name="lint", required=True, command_override="ruff check .")
        assert step.command_override == "ruff check ."

    def test_is_frozen(self) -> None:
        step = GatePipelineStep(name="lint", required=True)
        with pytest.raises(AttributeError):
            step.name = "changed"


# ---------------------------------------------------------------------------
# GateReport
# ---------------------------------------------------------------------------


class TestGateReport:
    def test_basic_construction(self) -> None:
        report = GateReport(
            task_id="task-abc",
            overall_pass=True,
            total_duration_ms=5000,
            gates_run=["lint", "type_check"],
            results=[],
            changed_files=["src/main.py"],
            cache_hits=0,
        )
        assert report.task_id == "task-abc"
        assert report.overall_pass is True
        assert len(report.gates_run) == 2

    def test_results_list(self) -> None:
        result = GateResult(
            name="lint",
            status="pass",
            required=True,
            blocked=False,
            cached=False,
            duration_ms=100,
            details="",
        )
        report = GateReport(
            task_id="task-abc",
            overall_pass=True,
            total_duration_ms=100,
            gates_run=["lint"],
            results=[result],
            changed_files=[],
            cache_hits=0,
        )
        assert len(report.results) == 1
        assert report.results[0].name == "lint"


# ---------------------------------------------------------------------------
# build_default_pipeline
# ---------------------------------------------------------------------------


class TestBuildDefaultPipeline:
    def test_returns_list(self) -> None:
        class DummyConfig:
            lint = True

        steps = build_default_pipeline(DummyConfig())
        assert isinstance(steps, list)

    def test_empty_list_when_no_gates_enabled(self) -> None:
        class EmptyConfig:
            pass

        steps = build_default_pipeline(EmptyConfig())
        assert steps == []

    def test_nested_enabled_attribute(self) -> None:
        class NestedConfig:
            class LintGate:
                enabled = True

            lint = LintGate()

        steps = build_default_pipeline(NestedConfig())
        names = {s.name for s in steps}
        assert "lint" in names
