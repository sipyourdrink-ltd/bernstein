"""Tests for the dead_code_detector module."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.dead_code_detector import (
    DeadCodeReport,
    _check_unreachable_branches,
    _check_unused_imports,
    _extract_added_names,
    _extract_removed_names,
    analyse,
)


# ---------------------------------------------------------------------------
# _extract_removed_names
# ---------------------------------------------------------------------------


class TestExtractRemovedNames:
    def test_detects_removed_def(self) -> None:
        diff = "-def old_function(x):\n+def new_function(x):\n"
        assert "old_function" in _extract_removed_names(diff)

    def test_detects_removed_async_def(self) -> None:
        diff = "-async def old_handler():\n"
        assert "old_handler" in _extract_removed_names(diff)

    def test_detects_removed_class(self) -> None:
        diff = "-class OldClass:\n"
        assert "OldClass" in _extract_removed_names(diff)

    def test_ignores_added_lines(self) -> None:
        diff = "+def new_func():\n"
        assert "new_func" not in _extract_removed_names(diff)

    def test_ignores_dunder_methods(self) -> None:
        diff = "-def __init__(self):\n-def __repr__(self):\n"
        names = _extract_removed_names(diff)
        assert "__init__" not in names
        assert "__repr__" not in names

    def test_empty_diff(self) -> None:
        assert _extract_removed_names("") == set()


# ---------------------------------------------------------------------------
# _extract_added_names
# ---------------------------------------------------------------------------


class TestExtractAddedNames:
    def test_detects_added_def(self) -> None:
        diff = "+def new_function(x):\n"
        assert "new_function" in _extract_added_names(diff)

    def test_detects_added_class(self) -> None:
        diff = "+class NewClass:\n"
        assert "NewClass" in _extract_added_names(diff)

    def test_ignores_removed_lines(self) -> None:
        diff = "-def old_func():\n"
        assert "old_func" not in _extract_added_names(diff)


# ---------------------------------------------------------------------------
# _check_unused_imports
# ---------------------------------------------------------------------------


class TestCheckUnusedImports:
    def test_used_import_ok(self) -> None:
        source = "import os\nprint(os.getcwd())\n"
        issues = _check_unused_imports(source, "test.py")
        assert not issues

    def test_unused_import_flagged(self) -> None:
        source = "import os\nx = 1\n"
        issues = _check_unused_imports(source, "test.py")
        assert any(i.name == "os" and i.kind == "unused_import" for i in issues)

    def test_unused_from_import_flagged(self) -> None:
        source = "from pathlib import Path\nx = 1\n"
        issues = _check_unused_imports(source, "test.py")
        assert any(i.name == "Path" and i.kind == "unused_import" for i in issues)

    def test_asname_used(self) -> None:
        source = "import numpy as np\narr = np.array([1])\n"
        issues = _check_unused_imports(source, "test.py")
        assert not any(i.name == "np" for i in issues)

    def test_star_import_not_flagged(self) -> None:
        source = "from os.path import *\nx = join('a', 'b')\n"
        issues = _check_unused_imports(source, "test.py")
        # star imports should not produce individual issues
        assert not any(i.name == "*" for i in issues)

    def test_private_import_not_flagged(self) -> None:
        # Imports starting with _ are ignored
        source = "from foo import _internal\nx = 1\n"
        issues = _check_unused_imports(source, "test.py")
        assert not any(i.name == "_internal" for i in issues)

    def test_syntax_error_returns_empty(self) -> None:
        source = "def (broken syntax:\n"
        issues = _check_unused_imports(source, "bad.py")
        assert issues == []


# ---------------------------------------------------------------------------
# _check_unreachable_branches
# ---------------------------------------------------------------------------


class TestCheckUnreachableBranches:
    def test_if_false_flagged(self) -> None:
        source = "if False:\n    x = 1\n"
        issues = _check_unreachable_branches(source, "test.py")
        assert any(i.kind == "unreachable_branch" and "always False" in i.detail for i in issues)

    def test_if_true_else_flagged(self) -> None:
        source = "if True:\n    x = 1\nelse:\n    x = 2\n"
        issues = _check_unreachable_branches(source, "test.py")
        assert any(i.kind == "unreachable_branch" and "always True" in i.detail for i in issues)

    def test_code_after_return_flagged(self) -> None:
        source = "def foo():\n    return 1\n    x = 2\n"
        issues = _check_unreachable_branches(source, "test.py")
        assert any(i.kind == "unreachable_branch" and "return" in i.detail for i in issues)

    def test_code_after_raise_flagged(self) -> None:
        source = "def foo():\n    raise ValueError()\n    x = 2\n"
        issues = _check_unreachable_branches(source, "test.py")
        assert any(i.kind == "unreachable_branch" and "raise" in i.detail for i in issues)

    def test_normal_if_not_flagged(self) -> None:
        source = "x = 1\nif x > 0:\n    y = 1\nelse:\n    y = -1\n"
        issues = _check_unreachable_branches(source, "test.py")
        assert not issues

    def test_docstring_after_return_not_flagged(self) -> None:
        # A lone docstring after return is a common pattern for type checkers
        source = 'def foo():\n    return 1\n    "This is fine"\n'
        issues = _check_unreachable_branches(source, "test.py")
        assert not any("unreachable" in i.detail and "return" in i.detail for i in issues)

    def test_syntax_error_returns_empty(self) -> None:
        source = "def (broken:\n"
        issues = _check_unreachable_branches(source, "bad.py")
        assert issues == []


# ---------------------------------------------------------------------------
# DeadCodeReport
# ---------------------------------------------------------------------------


class TestDeadCodeReport:
    def test_passed_when_no_issues(self) -> None:
        report = DeadCodeReport()
        assert report.passed

    def test_not_passed_with_issues(self) -> None:
        from bernstein.core.dead_code_detector import DeadCodeIssue

        report = DeadCodeReport()
        report.issues.append(DeadCodeIssue(kind="unused_import", name="os", file="x.py", detail="unused"))
        assert not report.passed

    def test_summary_no_issues(self) -> None:
        report = DeadCodeReport(searched_files=10)
        report.checked_files = ["a.py"]
        assert "No dead code found" in report.summary()

    def test_summary_with_issues(self) -> None:
        from bernstein.core.dead_code_detector import DeadCodeIssue

        report = DeadCodeReport()
        report.issues.append(DeadCodeIssue(kind="lost_caller", name="foo", file="x.py", detail="d"))
        assert "lost_caller" in report.summary()


# ---------------------------------------------------------------------------
# analyse() integration test (no real git / subprocess)
# ---------------------------------------------------------------------------


class TestAnalyse:
    def test_detects_unused_import_in_changed_file(self, tmp_path: Path) -> None:
        src = tmp_path / "mymod.py"
        src.write_text("import os\nx = 1\n")
        report = analyse(
            ["mymod.py"],
            tmp_path,
            check_unused_imports=True,
            check_unreachable=False,
            check_lost_callers=False,
        )
        assert any(i.kind == "unused_import" and i.name == "os" for i in report.issues)

    def test_detects_unreachable_in_changed_file(self, tmp_path: Path) -> None:
        src = tmp_path / "mymod.py"
        src.write_text("def foo():\n    return 1\n    x = 2\n")
        report = analyse(
            ["mymod.py"],
            tmp_path,
            check_unused_imports=False,
            check_unreachable=True,
            check_lost_callers=False,
        )
        assert any(i.kind == "unreachable_branch" for i in report.issues)

    def test_passes_when_no_issues(self, tmp_path: Path) -> None:
        src = tmp_path / "mymod.py"
        src.write_text("import os\nprint(os.getcwd())\n")
        report = analyse(
            ["mymod.py"],
            tmp_path,
            check_unused_imports=True,
            check_unreachable=True,
            check_lost_callers=False,
        )
        assert report.passed

    def test_missing_file_skipped_gracefully(self, tmp_path: Path) -> None:
        report = analyse(
            ["nonexistent.py"],
            tmp_path,
            check_unused_imports=True,
            check_unreachable=True,
            check_lost_callers=False,
        )
        # No crash, just empty results
        assert isinstance(report, DeadCodeReport)
