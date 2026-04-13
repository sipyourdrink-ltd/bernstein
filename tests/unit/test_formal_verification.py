"""Unit tests for the formal verification gateway.

Tests cover:
- Z3 property checking (via Python eval fallback when z3-solver absent)
- Lean4 verification (subprocess mock)
- run_formal_verification() orchestration
- load_formal_verification_config() YAML parsing
- seed.py formal_verification parsing
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.formal_verification import (
    FormalProperty,
    FormalVerificationConfig,
    PropertyViolation,
    _build_context,
    _verify_python_eval,
    load_formal_verification_config,
    run_formal_verification,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Minimal Task stub
# ---------------------------------------------------------------------------


@dataclass
class _FakeTask:
    """Minimal Task stub implementing the subset used by formal_verification."""

    id: str = "task-abc123"
    title: str = "Test task"
    result_summary: str | None = "Done"
    role: str = "backend"


def _fake_task(**kwargs: Any) -> Any:
    """Create a _FakeTask cast to Any to satisfy formal_verification signatures."""
    return _FakeTask(**kwargs)


# ---------------------------------------------------------------------------
# _build_context
# ---------------------------------------------------------------------------


class TestBuildContext:
    def test_defaults(self) -> None:
        task = _fake_task()
        ctx = _build_context(task)
        assert ctx["files_modified"] == 0
        assert ctx["test_passed"] is True
        assert ctx["has_result"] is True
        assert ctx["result_length"] == len("Done")
        assert ctx["title_length"] == len("Test task")

    def test_no_result_summary(self) -> None:
        task = _fake_task(result_summary=None)
        ctx = _build_context(task)
        assert ctx["has_result"] is False
        assert ctx["result_length"] == 0

    def test_files_modified_passed_through(self) -> None:
        task = _fake_task()
        ctx = _build_context(task, files_modified=7)
        assert ctx["files_modified"] == 7

    def test_test_passed_false(self) -> None:
        task = _fake_task()
        ctx = _build_context(task, test_passed=False)
        assert ctx["test_passed"] is False


# ---------------------------------------------------------------------------
# _verify_python_eval
# ---------------------------------------------------------------------------


class TestVerifyPythonEval:
    def _prop(self, invariant: str, name: str = "p") -> FormalProperty:
        return FormalProperty(name=name, invariant=invariant, checker="z3")

    def test_passing_invariant(self) -> None:
        prop = self._prop("files_modified > 0")
        context = {"files_modified": 5}
        result = _verify_python_eval(prop, context)
        assert result is None

    def test_violated_invariant(self) -> None:
        prop = self._prop("files_modified > 0")
        context = {"files_modified": 0}
        result = _verify_python_eval(prop, context)
        assert result is not None
        assert isinstance(result, PropertyViolation)
        assert result.property_name == "p"
        assert result.checker == "python_eval"

    def test_boolean_context(self) -> None:
        prop = self._prop("has_result")
        assert _verify_python_eval(prop, {"has_result": True}) is None
        violation = _verify_python_eval(prop, {"has_result": False})
        assert violation is not None

    def test_compound_expression(self) -> None:
        prop = self._prop("result_length > 0 and files_modified >= 0")
        ctx = {"result_length": 10, "files_modified": 0}
        assert _verify_python_eval(prop, ctx) is None

    def test_bad_expression_returns_none(self) -> None:
        # Syntax errors should not raise — return None (skip rather than block)
        prop = self._prop("this is not python")
        result = _verify_python_eval(prop, {})
        assert result is None

    def test_forbidden_names_unavailable(self) -> None:
        # __builtins__ is empty so builtins like open(), os, etc. are not accessible
        prop = self._prop("open")  # 'open' is not in context → NameError → returns None
        result = _verify_python_eval(prop, {})
        assert result is None


# ---------------------------------------------------------------------------
# run_formal_verification — disabled / empty
# ---------------------------------------------------------------------------


class TestRunFormalVerificationGatekeeping:
    def _task(self) -> _FakeTask:
        return _fake_task()

    def test_disabled_config_skips(self, tmp_path: Path) -> None:
        config = FormalVerificationConfig(enabled=False, properties=[])
        result = run_formal_verification(self._task(), tmp_path, config)  # type: ignore[arg-type]
        assert result.skipped is True
        assert result.passed is True

    def test_empty_properties_skips(self, tmp_path: Path) -> None:
        config = FormalVerificationConfig(enabled=True, properties=[])
        result = run_formal_verification(self._task(), tmp_path, config)  # type: ignore[arg-type]
        assert result.skipped is True
        assert result.passed is True

    def test_task_id_in_result(self, tmp_path: Path) -> None:
        config = FormalVerificationConfig(enabled=False)
        result = run_formal_verification(self._task(), tmp_path, config)  # type: ignore[arg-type]
        assert result.task_id == "task-abc123"


# ---------------------------------------------------------------------------
# run_formal_verification — Z3 path (via python_eval fallback)
# ---------------------------------------------------------------------------


class TestRunFormalVerificationZ3:
    """Tests use the Python eval fallback (z3-solver may not be installed in CI)."""

    def _config(self, invariants: list[str]) -> FormalVerificationConfig:
        return FormalVerificationConfig(
            enabled=True,
            properties=[FormalProperty(name=f"prop_{i}", invariant=inv) for i, inv in enumerate(invariants)],
            block_on_violation=True,
        )

    def test_all_pass(self, tmp_path: Path) -> None:
        task = _fake_task(result_summary="Done")
        config = self._config(["result_length > 0", "has_result"])
        result = run_formal_verification(task, tmp_path, config, files_modified=1)  # type: ignore[arg-type]
        assert result.passed is True
        assert result.violations == []
        assert result.properties_checked == 2

    def test_violation_detected(self, tmp_path: Path) -> None:
        task = _fake_task(result_summary="")
        config = self._config(["result_length > 0"])
        result = run_formal_verification(task, tmp_path, config)  # type: ignore[arg-type]
        assert result.passed is False
        assert len(result.violations) == 1
        assert result.violations[0].property_name == "prop_0"

    def test_partial_violation(self, tmp_path: Path) -> None:
        task = _fake_task(result_summary="x")
        # First property passes, second fails
        config = self._config(["result_length > 0", "files_modified > 0"])
        result = run_formal_verification(task, tmp_path, config, files_modified=0)  # type: ignore[arg-type]
        assert result.passed is False
        assert result.properties_checked == 2
        assert len(result.violations) == 1
        assert result.violations[0].property_name == "prop_1"

    def test_block_on_violation_false_still_records(self, tmp_path: Path) -> None:
        task = _fake_task(result_summary="")
        config = FormalVerificationConfig(
            enabled=True,
            properties=[FormalProperty(name="p", invariant="result_length > 0")],
            block_on_violation=False,  # warn-only
        )
        result = run_formal_verification(task, tmp_path, config)  # type: ignore[arg-type]
        # Violations are still recorded regardless of block_on_violation
        assert result.passed is False
        assert len(result.violations) == 1


# ---------------------------------------------------------------------------
# run_formal_verification — Lean4 path (subprocess mocked)
# ---------------------------------------------------------------------------


class TestRunFormalVerificationLean4:
    def _lean4_prop(self, invariant: str = "True") -> FormalProperty:
        return FormalProperty(name="lean_prop", invariant=invariant, checker="lean4")

    def test_lean4_pass_when_cli_succeeds(self, tmp_path: Path) -> None:
        task = _fake_task()
        config = FormalVerificationConfig(enabled=True, properties=[self._lean4_prop()])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_formal_verification(task, tmp_path, config)  # type: ignore[arg-type]
        assert result.passed is True

    def test_lean4_fail_when_cli_fails(self, tmp_path: Path) -> None:
        task = _fake_task()
        config = FormalVerificationConfig(enabled=True, properties=[self._lean4_prop("False")])
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error: tactic 'decide' failed")
            result = run_formal_verification(task, tmp_path, config)  # type: ignore[arg-type]
        assert result.passed is False
        assert len(result.violations) == 1
        assert result.violations[0].checker == "lean4"
        assert "tactic" in result.violations[0].counterexample

    def test_lean4_skipped_when_cli_not_found(self, tmp_path: Path) -> None:
        task = _fake_task()
        config = FormalVerificationConfig(enabled=True, properties=[self._lean4_prop()])
        with patch("subprocess.run", side_effect=FileNotFoundError("lean not found")):
            result = run_formal_verification(task, tmp_path, config)  # type: ignore[arg-type]
        # FileNotFoundError → non-fatal skip → passes
        assert result.passed is True
        assert result.properties_checked == 1

    def test_lean4_timeout_produces_violation(self, tmp_path: Path) -> None:
        import subprocess

        task = _fake_task()
        config = FormalVerificationConfig(enabled=True, properties=[self._lean4_prop()], timeout_s=1)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["lean"], 1)):
            result = run_formal_verification(task, tmp_path, config)  # type: ignore[arg-type]
        assert result.passed is False
        assert result.violations[0].counterexample == "(timeout)"


# ---------------------------------------------------------------------------
# load_formal_verification_config
# ---------------------------------------------------------------------------


class TestLoadFormalVerificationConfig:
    def test_returns_none_when_no_bernstein_yaml(self, tmp_path: Path) -> None:
        result = load_formal_verification_config(tmp_path)
        assert result is None

    def test_returns_none_when_no_formal_section(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text("goal: test\n")
        result = load_formal_verification_config(tmp_path)
        assert result is None

    def test_parses_minimal_config(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text(
            "goal: test\nformal_verification:\n  enabled: true\n  properties: []\n"
        )
        config = load_formal_verification_config(tmp_path)
        assert config is not None
        assert config.enabled is True
        assert config.properties == []

    def test_parses_properties(self, tmp_path: Path) -> None:
        yaml_content = """
goal: test
formal_verification:
  enabled: true
  block_on_violation: false
  timeout_s: 30
  properties:
    - name: output_non_empty
      invariant: "result_length > 0"
      checker: z3
    - name: lean_theorem
      invariant: "True"
      checker: lean4
      lemmas_file: proofs/lemmas.lean
"""
        (tmp_path / "bernstein.yaml").write_text(yaml_content)
        config = load_formal_verification_config(tmp_path)
        assert config is not None
        assert config.block_on_violation is False
        assert config.timeout_s == 30
        assert len(config.properties) == 2
        p0, p1 = config.properties
        assert p0.name == "output_non_empty"
        assert p0.invariant == "result_length > 0"
        assert p0.checker == "z3"
        assert p1.checker == "lean4"
        assert p1.lemmas_file == "proofs/lemmas.lean"

    def test_unknown_checker_defaults_to_z3(self, tmp_path: Path) -> None:
        yaml_content = """
goal: test
formal_verification:
  enabled: true
  properties:
    - name: p
      invariant: "True"
      checker: unknown_solver
"""
        (tmp_path / "bernstein.yaml").write_text(yaml_content)
        config = load_formal_verification_config(tmp_path)
        assert config is not None
        assert config.properties[0].checker == "z3"

    def test_invalid_yaml_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "bernstein.yaml").write_text("formal_verification: not-a-dict\n")
        # load_formal_verification_config logs a warning and returns None
        result = load_formal_verification_config(tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# seed.py integration
# ---------------------------------------------------------------------------


class TestSeedFormalVerificationParsing:
    def test_seed_parses_formal_verification(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        yaml_content = """
goal: test goal
formal_verification:
  enabled: true
  block_on_violation: true
  timeout_s: 45
  properties:
    - name: has_output
      invariant: "result_length > 0"
      checker: z3
"""
        seed_file = tmp_path / "bernstein.yaml"
        seed_file.write_text(yaml_content)
        seed = parse_seed(seed_file)
        assert seed.formal_verification is not None
        assert seed.formal_verification.enabled is True
        assert seed.formal_verification.timeout_s == 45
        assert len(seed.formal_verification.properties) == 1
        assert seed.formal_verification.properties[0].name == "has_output"

    def test_seed_formal_verification_absent_is_none(self, tmp_path: Path) -> None:
        from bernstein.core.seed import parse_seed

        (tmp_path / "bernstein.yaml").write_text("goal: minimal\n")
        seed = parse_seed(tmp_path / "bernstein.yaml")
        assert seed.formal_verification is None

    def test_seed_rejects_non_mapping(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        (tmp_path / "bernstein.yaml").write_text("goal: x\nformal_verification: bad_string\n")
        with pytest.raises(SeedError, match="formal_verification must be a mapping"):
            parse_seed(tmp_path / "bernstein.yaml")

    def test_seed_rejects_invalid_checker(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        yaml_content = """
goal: x
formal_verification:
  properties:
    - name: p
      invariant: "True"
      checker: bad_checker
"""
        (tmp_path / "bernstein.yaml").write_text(yaml_content)
        with pytest.raises(SeedError, match="checker must be 'z3' or 'lean4'"):
            parse_seed(tmp_path / "bernstein.yaml")

    def test_seed_rejects_non_bool_enabled(self, tmp_path: Path) -> None:
        from bernstein.core.seed import SeedError, parse_seed

        (tmp_path / "bernstein.yaml").write_text("goal: x\nformal_verification:\n  enabled: maybe\n")
        with pytest.raises(SeedError, match="enabled must be a bool"):
            parse_seed(tmp_path / "bernstein.yaml")
