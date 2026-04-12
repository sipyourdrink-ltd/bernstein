"""Unit tests for mutation testing module."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.quality.mutation_testing import (
    MutantResult,
    MutationReport,
    MutationTestConfig,
    generate_mutants,
    render_report,
    run_mutant,
    run_mutation_tests,
)

# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


class TestMutationTestConfig:
    def test_defaults(self) -> None:
        cfg = MutationTestConfig(
            target_modules=("mod.a",),
            test_command="pytest -x",
        )
        assert cfg.timeout_per_mutant_s == 30
        assert cfg.min_score == pytest.approx(0.80)

    def test_frozen(self) -> None:
        cfg = MutationTestConfig(target_modules=("m",), test_command="t")
        with pytest.raises(AttributeError):
            cfg.min_score = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# generate_mutants
# ---------------------------------------------------------------------------


class TestGenerateMutants:
    def test_compare_swap(self) -> None:
        source = textwrap.dedent("""\
            def f(x):
                if x == 1:
                    return True
        """)
        mutants = generate_mutants(source, "test_mod")
        compare_mutants = [m for m in mutants if m.mutation_type.startswith("compare_swap")]
        assert len(compare_mutants) >= 1
        # The swapped source should contain NotEq (!=)
        assert any("!=" in m.mutated_source for m in compare_mutants)

    def test_binop_swap(self) -> None:
        source = textwrap.dedent("""\
            def add(a, b):
                return a + b
        """)
        mutants = generate_mutants(source, "test_mod")
        binop_mutants = [m for m in mutants if m.mutation_type.startswith("binop_swap")]
        assert len(binop_mutants) >= 1
        assert any("-" in m.mutated_source for m in binop_mutants)

    def test_bool_negate(self) -> None:
        source = textwrap.dedent("""\
            def check():
                return True
        """)
        mutants = generate_mutants(source, "test_mod")
        bool_mutants = [m for m in mutants if m.mutation_type.startswith("bool_negate")]
        assert len(bool_mutants) >= 1
        assert any("False" in m.mutated_source for m in bool_mutants)

    def test_return_none(self) -> None:
        source = textwrap.dedent("""\
            def compute(x):
                return x * 2
        """)
        mutants = generate_mutants(source, "test_mod")
        return_mutants = [m for m in mutants if m.mutation_type == "return_none"]
        assert len(return_mutants) >= 1
        assert any("None" in m.mutated_source for m in return_mutants)

    def test_no_mutants_from_empty_source(self) -> None:
        mutants = generate_mutants("", "empty")
        assert mutants == []

    def test_invalid_syntax_returns_empty(self) -> None:
        mutants = generate_mutants("def broken(", "bad")
        assert mutants == []

    def test_mutant_line_numbers_are_positive(self) -> None:
        source = textwrap.dedent("""\
            def f():
                x = 1 + 2
                return x == 3
        """)
        mutants = generate_mutants(source, "mod")
        for m in mutants:
            assert m.line > 0

    def test_multiple_mutation_types_from_rich_source(self) -> None:
        source = textwrap.dedent("""\
            def example(a, b):
                if a == b:
                    return a + b
                return False
        """)
        mutants = generate_mutants(source, "rich")
        types = {m.mutation_type.split("(")[0] for m in mutants}
        assert "compare_swap" in types
        assert "binop_swap" in types
        assert "bool_negate" in types
        assert "return_none" in types


# ---------------------------------------------------------------------------
# run_mutant (mocked subprocess)
# ---------------------------------------------------------------------------


class TestRunMutant:
    def test_killed_when_tests_fail(self, tmp_path: Path) -> None:
        src = tmp_path / "module.py"
        src.write_text("original", encoding="utf-8")

        with patch("bernstein.core.quality.mutation_testing.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = "FAILED"
            mock_run.return_value.stderr = ""

            killed, output = run_mutant(src, "mutated", "pytest", timeout=10)

        assert killed is True
        assert "FAILED" in output
        # Original restored
        assert src.read_text(encoding="utf-8") == "original"

    def test_survived_when_tests_pass(self, tmp_path: Path) -> None:
        src = tmp_path / "module.py"
        src.write_text("original", encoding="utf-8")

        with patch("bernstein.core.quality.mutation_testing.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "ok"
            mock_run.return_value.stderr = ""

            killed, _output = run_mutant(src, "mutated", "pytest", timeout=10)

        assert killed is False
        assert src.read_text(encoding="utf-8") == "original"

    def test_killed_on_timeout(self, tmp_path: Path) -> None:
        src = tmp_path / "module.py"
        src.write_text("original", encoding="utf-8")

        import subprocess as sp

        with patch(
            "bernstein.core.quality.mutation_testing.subprocess.run",
            side_effect=sp.TimeoutExpired("cmd", 5),
        ):
            killed, output = run_mutant(src, "mutated", "pytest", timeout=5)

        assert killed is True
        assert output == "timeout"
        assert src.read_text(encoding="utf-8") == "original"

    def test_restores_original_on_os_error(self, tmp_path: Path) -> None:
        src = tmp_path / "module.py"
        src.write_text("original", encoding="utf-8")

        with patch(
            "bernstein.core.quality.mutation_testing.subprocess.run",
            side_effect=OSError("disk full"),
        ):
            killed, output = run_mutant(src, "mutated", "pytest", timeout=5)

        assert killed is True
        assert "disk full" in output
        assert src.read_text(encoding="utf-8") == "original"


# ---------------------------------------------------------------------------
# run_mutation_tests (integration, mocked subprocess)
# ---------------------------------------------------------------------------


class TestRunMutationTests:
    def test_full_run_scores_correctly(self, tmp_path: Path) -> None:
        # Create a source file with a simple function
        src_dir = tmp_path / "src" / "pkg"
        src_dir.mkdir(parents=True)
        (src_dir / "mod.py").write_text(
            textwrap.dedent("""\
                def add(a, b):
                    return a + b
            """),
            encoding="utf-8",
        )

        config = MutationTestConfig(
            target_modules=("pkg.mod",),
            test_command="pytest -x",
            timeout_per_mutant_s=5,
        )

        # All mutants killed
        with patch("bernstein.core.quality.mutation_testing.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = "FAILED"
            mock_run.return_value.stderr = ""

            report = run_mutation_tests(config, tmp_path)

        assert report.total_mutants > 0
        assert report.killed == report.total_mutants
        assert report.survived == 0
        assert report.score == pytest.approx(1.0)

    def test_skips_missing_module(self, tmp_path: Path) -> None:
        config = MutationTestConfig(
            target_modules=("nonexistent.module",),
            test_command="pytest -x",
        )

        report = run_mutation_tests(config, tmp_path)

        assert report.total_mutants == 0
        assert report.score == pytest.approx(1.0)

    def test_partial_kill_score(self, tmp_path: Path) -> None:
        src_dir = tmp_path / "src" / "pkg"
        src_dir.mkdir(parents=True)
        (src_dir / "logic.py").write_text(
            textwrap.dedent("""\
                def check(x):
                    if x == 1:
                        return True
                    return False
            """),
            encoding="utf-8",
        )

        config = MutationTestConfig(
            target_modules=("pkg.logic",),
            test_command="pytest -x",
            timeout_per_mutant_s=5,
        )

        call_count = 0

        def alternating_run(*args: object, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1

            class FakeResult:
                # Even calls killed, odd survived
                returncode = 1 if call_count % 2 == 1 else 0
                stdout = "out"
                stderr = ""

            return FakeResult()

        with patch(
            "bernstein.core.quality.mutation_testing.subprocess.run",
            side_effect=alternating_run,
        ):
            report = run_mutation_tests(config, tmp_path)

        assert report.total_mutants > 0
        assert 0 < report.score < 1.0
        assert report.killed + report.survived == report.total_mutants


# ---------------------------------------------------------------------------
# render_report
# ---------------------------------------------------------------------------


class TestRenderReport:
    def _make_report(
        self,
        *,
        score: float = 0.75,
        min_score: float = 0.80,
        killed: int = 3,
        survived: int = 1,
    ) -> MutationReport:
        config = MutationTestConfig(
            target_modules=("mod",),
            test_command="pytest",
            min_score=min_score,
        )
        results: list[MutantResult] = []
        for i in range(killed):
            results.append(
                MutantResult(
                    module="mod",
                    line=i + 1,
                    mutation_type="compare_swap(Eq->NotEq)",
                    killed=True,
                    test_output="FAILED",
                )
            )
        for i in range(survived):
            results.append(
                MutantResult(
                    module="mod",
                    line=100 + i,
                    mutation_type="return_none",
                    killed=False,
                    test_output="ok",
                )
            )
        return MutationReport(
            config=config,
            total_mutants=killed + survived,
            killed=killed,
            survived=survived,
            score=score,
            results=tuple(results),
            duration_s=1.5,
        )

    def test_contains_score(self) -> None:
        md = render_report(self._make_report())
        assert "75%" in md

    def test_shows_pass_when_above_threshold(self) -> None:
        md = render_report(self._make_report(score=0.90, min_score=0.80))
        assert "PASS" in md

    def test_shows_fail_when_below_threshold(self) -> None:
        md = render_report(self._make_report(score=0.50, min_score=0.80))
        assert "FAIL" in md

    def test_surviving_mutants_section(self) -> None:
        md = render_report(self._make_report(survived=2))
        assert "Surviving Mutants" in md
        assert "return_none" in md

    def test_killed_mutants_section(self) -> None:
        md = render_report(self._make_report(killed=3))
        assert "Killed Mutants" in md

    def test_no_surviving_section_when_all_killed(self) -> None:
        md = render_report(self._make_report(killed=4, survived=0, score=1.0))
        assert "Surviving Mutants" not in md
