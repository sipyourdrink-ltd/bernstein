"""Unit tests for regression test suite auto-expansion."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.quality.regression_expansion import (
    ExpansionResult,
    TestGap,
    _assign_priority,
    _count_test_functions,
    _extract_functions,
    _extract_test_references,
    analyze_function_coverage,
    detect_test_gaps,
    generate_test_stubs,
    match_test_file,
    render_expansion_report,
)

# ---------------------------------------------------------------------------
# TestGap — frozen dataclass
# ---------------------------------------------------------------------------


class TestTestGap:
    def test_fields_present(self) -> None:
        gap = TestGap(
            file_path="src/foo.py",
            function_name="do_stuff",
            reason="no test file exists",
            priority="high",
        )
        assert gap.file_path == "src/foo.py"
        assert gap.function_name == "do_stuff"
        assert gap.reason == "no test file exists"
        assert gap.priority == "high"

    def test_frozen(self) -> None:
        gap = TestGap(file_path="a.py", function_name="f", reason="r", priority="low")
        with pytest.raises(AttributeError):
            gap.priority = "high"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = TestGap(file_path="x.py", function_name="fn", reason="r", priority="medium")
        b = TestGap(file_path="x.py", function_name="fn", reason="r", priority="medium")
        assert a == b

    def test_hash(self) -> None:
        gap = TestGap(file_path="x.py", function_name="fn", reason="r", priority="high")
        assert hash(gap) == hash(gap)


# ---------------------------------------------------------------------------
# ExpansionResult — frozen dataclass
# ---------------------------------------------------------------------------


class TestExpansionResult:
    def test_fields_present(self) -> None:
        result = ExpansionResult(
            gaps=(),
            existing_test_count=5,
            suggested_test_count=0,
            coverage_before=1.0,
            coverage_after_estimate=1.0,
        )
        assert result.gaps == ()
        assert result.existing_test_count == 5
        assert result.suggested_test_count == 0

    def test_frozen(self) -> None:
        result = ExpansionResult(
            gaps=(),
            existing_test_count=0,
            suggested_test_count=0,
            coverage_before=0.5,
            coverage_after_estimate=0.8,
        )
        with pytest.raises(AttributeError):
            result.coverage_before = 0.9  # type: ignore[misc]

    def test_gaps_is_tuple(self) -> None:
        gap = TestGap(file_path="a.py", function_name="f", reason="r", priority="low")
        result = ExpansionResult(
            gaps=(gap,),
            existing_test_count=0,
            suggested_test_count=1,
            coverage_before=0.0,
            coverage_after_estimate=1.0,
        )
        assert isinstance(result.gaps, tuple)
        assert len(result.gaps) == 1


# ---------------------------------------------------------------------------
# _extract_functions
# ---------------------------------------------------------------------------


class TestExtractFunctions:
    def test_top_level_function(self) -> None:
        src = "def foo():\n    pass\n"
        assert _extract_functions(src) == ["foo"]

    def test_async_function(self) -> None:
        src = "async def bar():\n    pass\n"
        assert _extract_functions(src) == ["bar"]

    def test_class_method(self) -> None:
        src = "class C:\n    def method(self):\n        pass\n"
        assert _extract_functions(src) == ["method"]

    def test_skips_dunder(self) -> None:
        src = "class C:\n    def __init__(self):\n        pass\n    def run(self):\n        pass\n"
        assert _extract_functions(src) == ["run"]

    def test_syntax_error_returns_empty(self) -> None:
        assert _extract_functions("def !!!invalid") == []

    def test_multiple_functions(self) -> None:
        src = "def alpha():\n    pass\ndef beta():\n    pass\ndef gamma():\n    pass\n"
        assert _extract_functions(src) == ["alpha", "beta", "gamma"]

    def test_nested_functions_excluded(self) -> None:
        src = "def outer():\n    def inner():\n        pass\n    pass\n"
        # Only top-level and class methods; inner is nested so excluded.
        assert _extract_functions(src) == ["outer"]


# ---------------------------------------------------------------------------
# _extract_test_references
# ---------------------------------------------------------------------------


class TestExtractTestReferences:
    def test_name_references(self) -> None:
        src = "def test_foo():\n    result = foo()\n    assert result == 42\n"
        refs = _extract_test_references(src)
        assert "foo" in refs
        assert "result" in refs

    def test_attribute_references(self) -> None:
        src = "def test_bar():\n    obj.bar()\n"
        refs = _extract_test_references(src)
        assert "bar" in refs

    def test_ignores_non_test_functions(self) -> None:
        src = "def helper():\n    some_func()\n\ndef test_x():\n    pass\n"
        refs = _extract_test_references(src)
        assert "some_func" not in refs

    def test_syntax_error_returns_empty(self) -> None:
        refs = _extract_test_references("def !!!invalid")
        assert refs == set()


# ---------------------------------------------------------------------------
# _count_test_functions
# ---------------------------------------------------------------------------


class TestCountTestFunctions:
    def test_counts_top_level(self) -> None:
        src = "def test_a():\n    pass\ndef test_b():\n    pass\n"
        assert _count_test_functions(src) == 2

    def test_counts_class_methods(self) -> None:
        src = "class TestFoo:\n    def test_x(self):\n        pass\n"
        assert _count_test_functions(src) == 1

    def test_ignores_non_test(self) -> None:
        src = "def helper():\n    pass\ndef test_one():\n    pass\n"
        assert _count_test_functions(src) == 1

    def test_syntax_error_returns_zero(self) -> None:
        assert _count_test_functions("def !!!") == 0


# ---------------------------------------------------------------------------
# _assign_priority
# ---------------------------------------------------------------------------


class TestAssignPriority:
    def test_public(self) -> None:
        assert _assign_priority("process") == "high"

    def test_private(self) -> None:
        assert _assign_priority("_helper") == "medium"

    def test_mangled(self) -> None:
        assert _assign_priority("__internal") == "low"


# ---------------------------------------------------------------------------
# match_test_file
# ---------------------------------------------------------------------------


class TestMatchTestFile:
    def test_finds_test_file(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "tests" / "unit"
        test_dir.mkdir(parents=True)
        test_file = test_dir / "test_widget.py"
        test_file.write_text("def test_x(): pass\n", encoding="utf-8")

        result = match_test_file("src/pkg/widget.py", tmp_path / "tests")
        assert result is not None
        assert result.name == "test_widget.py"

    def test_returns_none_when_missing(self, tmp_path: Path) -> None:
        test_dir = tmp_path / "tests"
        test_dir.mkdir()
        assert match_test_file("src/pkg/nope.py", test_dir) is None

    def test_returns_none_for_nonexistent_dir(self, tmp_path: Path) -> None:
        assert match_test_file("foo.py", tmp_path / "no_such_dir") is None


# ---------------------------------------------------------------------------
# analyze_function_coverage
# ---------------------------------------------------------------------------


class TestAnalyzeFunctionCoverage:
    def test_full_coverage(self, tmp_path: Path) -> None:
        src = tmp_path / "module.py"
        src.write_text("def alpha():\n    pass\ndef beta():\n    pass\n", encoding="utf-8")
        tst = tmp_path / "test_module.py"
        tst.write_text("def test_alpha():\n    alpha()\ndef test_beta():\n    beta()\n", encoding="utf-8")

        covered, uncovered = analyze_function_coverage(src, tst)
        assert set(covered) == {"alpha", "beta"}
        assert uncovered == []

    def test_partial_coverage(self, tmp_path: Path) -> None:
        src = tmp_path / "module.py"
        src.write_text("def alpha():\n    pass\ndef beta():\n    pass\n", encoding="utf-8")
        tst = tmp_path / "test_module.py"
        tst.write_text("def test_alpha():\n    alpha()\n", encoding="utf-8")

        covered, uncovered = analyze_function_coverage(src, tst)
        assert covered == ["alpha"]
        assert uncovered == ["beta"]

    def test_no_test_file(self, tmp_path: Path) -> None:
        src = tmp_path / "module.py"
        src.write_text("def only_func():\n    pass\n", encoding="utf-8")

        covered, uncovered = analyze_function_coverage(src, tmp_path / "nonexistent.py")
        assert covered == []
        assert uncovered == ["only_func"]

    def test_missing_source_file(self, tmp_path: Path) -> None:
        covered, uncovered = analyze_function_coverage(tmp_path / "nope.py", tmp_path / "test_nope.py")
        assert covered == []
        assert uncovered == []


# ---------------------------------------------------------------------------
# detect_test_gaps
# ---------------------------------------------------------------------------


class TestDetectTestGaps:
    def _setup_project(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a minimal project layout."""
        src_dir = tmp_path / "src" / "pkg"
        src_dir.mkdir(parents=True)
        test_dir = tmp_path / "tests" / "unit"
        test_dir.mkdir(parents=True)
        return src_dir, test_dir

    def test_detects_uncovered_functions(self, tmp_path: Path) -> None:
        src_dir, test_dir = self._setup_project(tmp_path)
        (src_dir / "engine.py").write_text("def run():\n    pass\ndef stop():\n    pass\n", encoding="utf-8")
        (test_dir / "test_engine.py").write_text("def test_run():\n    run()\n", encoding="utf-8")

        result = detect_test_gaps(
            ["src/pkg/engine.py"],
            "tests",
            tmp_path,
        )
        assert result.suggested_test_count == 1
        assert result.gaps[0].function_name == "stop"
        assert result.gaps[0].priority == "high"

    def test_no_gaps_when_fully_covered(self, tmp_path: Path) -> None:
        src_dir, test_dir = self._setup_project(tmp_path)
        (src_dir / "util.py").write_text("def helper():\n    pass\n", encoding="utf-8")
        (test_dir / "test_util.py").write_text("def test_h():\n    helper()\n", encoding="utf-8")

        result = detect_test_gaps(["src/pkg/util.py"], "tests", tmp_path)
        assert result.suggested_test_count == 0
        assert result.gaps == ()

    def test_skips_non_python(self, tmp_path: Path) -> None:
        src_dir, _test_dir = self._setup_project(tmp_path)
        (src_dir / "readme.md").write_text("# hello\n", encoding="utf-8")

        result = detect_test_gaps(["src/pkg/readme.md"], "tests", tmp_path)
        assert result.gaps == ()

    def test_skips_test_files(self, tmp_path: Path) -> None:
        _, test_dir = self._setup_project(tmp_path)
        (test_dir / "test_something.py").write_text("def test_x(): pass\n", encoding="utf-8")

        result = detect_test_gaps(["tests/unit/test_something.py"], "tests", tmp_path)
        assert result.gaps == ()

    def test_skips_init_and_conftest(self, tmp_path: Path) -> None:
        src_dir, _ = self._setup_project(tmp_path)
        (src_dir / "__init__.py").write_text("", encoding="utf-8")
        (src_dir / "conftest.py").write_text("", encoding="utf-8")

        result = detect_test_gaps(["src/pkg/__init__.py", "src/pkg/conftest.py"], "tests", tmp_path)
        assert result.gaps == ()

    def test_no_test_file_marks_all_uncovered(self, tmp_path: Path) -> None:
        src_dir, _ = self._setup_project(tmp_path)
        (src_dir / "orphan.py").write_text("def a():\n    pass\ndef b():\n    pass\n", encoding="utf-8")

        result = detect_test_gaps(["src/pkg/orphan.py"], "tests", tmp_path)
        assert result.suggested_test_count == 2
        assert all(g.reason == "no test file exists" for g in result.gaps)

    def test_coverage_ratios(self, tmp_path: Path) -> None:
        src_dir, test_dir = self._setup_project(tmp_path)
        (src_dir / "mod.py").write_text("def covered():\n    pass\ndef uncovered():\n    pass\n", encoding="utf-8")
        (test_dir / "test_mod.py").write_text("def test_c():\n    covered()\n", encoding="utf-8")

        result = detect_test_gaps(["src/pkg/mod.py"], "tests", tmp_path)
        assert result.coverage_before == pytest.approx(0.5)
        assert result.coverage_after_estimate == pytest.approx(1.0)

    def test_nonexistent_source_file_skipped(self, tmp_path: Path) -> None:
        self._setup_project(tmp_path)
        result = detect_test_gaps(["src/pkg/gone.py"], "tests", tmp_path)
        assert result.gaps == ()

    def test_multiple_files(self, tmp_path: Path) -> None:
        src_dir, _test_dir = self._setup_project(tmp_path)
        (src_dir / "a.py").write_text("def fa():\n    pass\n", encoding="utf-8")
        (src_dir / "b.py").write_text("def fb():\n    pass\n", encoding="utf-8")

        result = detect_test_gaps(["src/pkg/a.py", "src/pkg/b.py"], "tests", tmp_path)
        assert result.suggested_test_count == 2
        names = {g.function_name for g in result.gaps}
        assert names == {"fa", "fb"}

    def test_existing_test_count(self, tmp_path: Path) -> None:
        src_dir, test_dir = self._setup_project(tmp_path)
        (src_dir / "svc.py").write_text("def start():\n    pass\ndef status():\n    pass\n", encoding="utf-8")
        (test_dir / "test_svc.py").write_text(
            "def test_start():\n    start()\ndef test_ping():\n    pass\n",
            encoding="utf-8",
        )

        result = detect_test_gaps(["src/pkg/svc.py"], "tests", tmp_path)
        assert result.existing_test_count == 2


# ---------------------------------------------------------------------------
# generate_test_stubs
# ---------------------------------------------------------------------------


class TestGenerateTestStubs:
    def test_empty_gaps_empty_string(self) -> None:
        assert generate_test_stubs(()) == ""

    def test_generates_stubs(self) -> None:
        gaps = (TestGap(file_path="src/a.py", function_name="do_x", reason="r", priority="high"),)
        stubs = generate_test_stubs(gaps)
        assert "def test_do_x() -> None:" in stubs
        assert "NotImplementedError" in stubs
        assert "import pytest" in stubs

    def test_strips_leading_underscores(self) -> None:
        gaps = (TestGap(file_path="x.py", function_name="_private", reason="r", priority="medium"),)
        stubs = generate_test_stubs(gaps)
        assert "def test_private() -> None:" in stubs

    def test_grouped_by_file(self) -> None:
        gaps = (
            TestGap(file_path="a.py", function_name="f1", reason="r", priority="high"),
            TestGap(file_path="b.py", function_name="f2", reason="r", priority="low"),
        )
        stubs = generate_test_stubs(gaps)
        assert "Stubs for a.py" in stubs
        assert "Stubs for b.py" in stubs

    def test_accepts_list(self) -> None:
        gaps = [
            TestGap(file_path="a.py", function_name="fn", reason="r", priority="high"),
        ]
        stubs = generate_test_stubs(gaps)
        assert "def test_fn() -> None:" in stubs


# ---------------------------------------------------------------------------
# render_expansion_report
# ---------------------------------------------------------------------------


class TestRenderExpansionReport:
    def test_no_gaps_report(self) -> None:
        result = ExpansionResult(
            gaps=(),
            existing_test_count=10,
            suggested_test_count=0,
            coverage_before=1.0,
            coverage_after_estimate=1.0,
        )
        report = render_expansion_report(result)
        assert "No test gaps detected" in report
        assert "# Test Expansion Report" in report

    def test_report_contains_summary(self) -> None:
        gap = TestGap(file_path="x.py", function_name="fn", reason="r", priority="high")
        result = ExpansionResult(
            gaps=(gap,),
            existing_test_count=3,
            suggested_test_count=1,
            coverage_before=0.75,
            coverage_after_estimate=1.0,
        )
        report = render_expansion_report(result)
        assert "Existing tests: **3**" in report
        assert "Suggested new tests: **1**" in report
        assert "75.0%" in report

    def test_report_contains_gap_table(self) -> None:
        gap = TestGap(file_path="mod.py", function_name="run", reason="no test file exists", priority="high")
        result = ExpansionResult(
            gaps=(gap,),
            existing_test_count=0,
            suggested_test_count=1,
            coverage_before=0.0,
            coverage_after_estimate=1.0,
        )
        report = render_expansion_report(result)
        assert "| `mod.py`" in report
        assert "| `run`" in report
        assert "| high |" in report

    def test_report_contains_stubs(self) -> None:
        gap = TestGap(file_path="mod.py", function_name="compute", reason="r", priority="high")
        result = ExpansionResult(
            gaps=(gap,),
            existing_test_count=0,
            suggested_test_count=1,
            coverage_before=0.0,
            coverage_after_estimate=1.0,
        )
        report = render_expansion_report(result)
        assert "```python" in report
        assert "def test_compute" in report

    def test_report_is_markdown(self) -> None:
        result = ExpansionResult(
            gaps=(),
            existing_test_count=0,
            suggested_test_count=0,
            coverage_before=1.0,
            coverage_after_estimate=1.0,
        )
        report = render_expansion_report(result)
        assert report.startswith("# Test Expansion Report")
