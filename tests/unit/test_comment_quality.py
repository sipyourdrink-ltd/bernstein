"""Tests for the comment_quality module."""

from __future__ import annotations

import ast
from pathlib import Path

from bernstein.core.comment_quality import (
    CommentQualityReport,
    _check_completeness,
    _check_param_accuracy,
    _detect_style,
    _extract_documented_params,
    _extract_documented_params_google,
    _extract_documented_params_numpy,
    _extract_documented_params_rest,
    _has_raises_doc,
    _has_return_doc,
    _is_redundant,
    analyse,
    analyse_file,
)


def _func_node(source: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Parse source and return the first function definition."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    raise ValueError("No function found in source")


# ---------------------------------------------------------------------------
# _detect_style
# ---------------------------------------------------------------------------


class TestDetectStyle:
    def test_google_args_section(self) -> None:
        doc = "Do something.\n\nArgs:\n    x: The value.\n"
        assert _detect_style(doc) == "google"

    def test_numpy_section(self) -> None:
        doc = "Do something.\n\nParameters\n----------\nx : int\n    The value.\n"
        assert _detect_style(doc) == "numpy"

    def test_rest_param(self) -> None:
        doc = "Do something.\n\n:param x: The value.\n:returns: Result.\n"
        assert _detect_style(doc) == "rest"

    def test_default_google_for_plain(self) -> None:
        doc = "Just a description with no sections."
        assert _detect_style(doc) == "google"


# ---------------------------------------------------------------------------
# _extract_documented_params
# ---------------------------------------------------------------------------


class TestExtractDocumentedParams:
    def test_google_extracts_params(self) -> None:
        doc = "Do something.\n\nArgs:\n    x: The value.\n    y: Another.\n"
        params = _extract_documented_params_google(doc)
        assert "x" in params
        assert "y" in params

    def test_numpy_extracts_params(self) -> None:
        doc = "Do something.\n\nParameters\n----------\nx : int\n    The value.\ny : str\n"
        params = _extract_documented_params_numpy(doc)
        assert "x" in params
        assert "y" in params

    def test_rest_extracts_params(self) -> None:
        doc = ":param x: The x value.\n:param y: The y value.\n"
        params = _extract_documented_params_rest(doc)
        assert "x" in params
        assert "y" in params

    def test_empty_docstring_returns_empty_set(self) -> None:
        assert _extract_documented_params("", "google") == set()


# ---------------------------------------------------------------------------
# _has_return_doc / _has_raises_doc
# ---------------------------------------------------------------------------


class TestHasReturnRaisesDoc:
    def test_google_has_returns(self) -> None:
        doc = "Do something.\n\nReturns:\n    int: The result.\n"
        assert _has_return_doc(doc, "google")

    def test_google_no_returns(self) -> None:
        doc = "Do something.\n\nArgs:\n    x: value.\n"
        assert not _has_return_doc(doc, "google")

    def test_numpy_has_returns(self) -> None:
        doc = "Do something.\n\nReturns\n-------\nint\n"
        assert _has_return_doc(doc, "numpy")

    def test_rest_has_returns(self) -> None:
        doc = ":returns: The result.\n"
        assert _has_return_doc(doc, "rest")

    def test_google_has_raises(self) -> None:
        doc = "Do something.\n\nRaises:\n    ValueError: On bad input.\n"
        assert _has_raises_doc(doc, "google")

    def test_google_no_raises(self) -> None:
        doc = "Do something.\n\nArgs:\n    x: value.\n"
        assert not _has_raises_doc(doc, "google")


# ---------------------------------------------------------------------------
# _is_redundant
# ---------------------------------------------------------------------------


class TestIsRedundant:
    def test_redundant_get_function(self) -> None:
        assert _is_redundant("get_user", "Get user.")

    def test_non_redundant_docstring(self) -> None:
        assert not _is_redundant("get_user", "Fetch the user record from the database by ID.")

    def test_empty_docstring_not_redundant(self) -> None:
        assert not _is_redundant("foo", "")


# ---------------------------------------------------------------------------
# _check_param_accuracy
# ---------------------------------------------------------------------------


class TestCheckParamAccuracy:
    def test_phantom_param_flagged(self) -> None:
        source = "def foo(x: int) -> None:\n    pass\n"
        node = _func_node(source)
        doc = "Do thing.\n\nArgs:\n    x: The value.\n    nonexistent: Ghost param.\n"
        issues = _check_param_accuracy(node, doc, "google", "test.py", "foo")
        assert any(i.kind == "inaccurate" and "nonexistent" in i.detail for i in issues)

    def test_correct_params_no_issues(self) -> None:
        source = "def foo(x: int, y: str) -> None:\n    pass\n"
        node = _func_node(source)
        doc = "Do thing.\n\nArgs:\n    x: The x.\n    y: The y.\n"
        issues = _check_param_accuracy(node, doc, "google", "test.py", "foo")
        assert not issues

    def test_self_excluded_from_check(self) -> None:
        source = "def foo(self, x: int) -> None:\n    pass\n"
        node = _func_node(source)
        doc = "Do thing.\n\nArgs:\n    x: The x.\n"
        issues = _check_param_accuracy(node, doc, "google", "test.py", "MyClass.foo")
        assert not issues


# ---------------------------------------------------------------------------
# _check_completeness
# ---------------------------------------------------------------------------


class TestCheckCompleteness:
    def test_missing_param_in_docstring(self) -> None:
        source = "def foo(x: int, y: str) -> None:\n    pass\n"
        node = _func_node(source)
        doc = "Do thing.\n\nArgs:\n    x: The x.\n"
        issues = _check_completeness(node, doc, "google", "test.py", "foo")
        assert any(i.kind == "incomplete" and "y" in i.detail for i in issues)

    def test_missing_return_doc(self) -> None:
        source = "def foo() -> int:\n    return 1\n"
        node = _func_node(source)
        doc = "Do thing.\n"
        issues = _check_completeness(node, doc, "google", "test.py", "foo")
        assert any(i.kind == "incomplete" and "return" in i.detail.lower() for i in issues)

    def test_missing_raises_doc(self) -> None:
        source = "def foo() -> None:\n    raise ValueError('bad')\n"
        node = _func_node(source)
        doc = "Do thing.\n"
        issues = _check_completeness(node, doc, "google", "test.py", "foo")
        assert any(i.kind == "incomplete" and "raise" in i.detail.lower() for i in issues)

    def test_complete_docstring_no_issues(self) -> None:
        source = "def foo(x: int) -> int:\n    return x\n"
        node = _func_node(source)
        doc = "Do thing.\n\nArgs:\n    x: The x.\n\nReturns:\n    int: Result.\n"
        issues = _check_completeness(node, doc, "google", "test.py", "foo")
        assert not issues


# ---------------------------------------------------------------------------
# analyse_file
# ---------------------------------------------------------------------------


class TestAnalyseFile:
    def test_no_issues_on_clean_file(self) -> None:
        source = (
            "def add(x: int, y: int) -> int:\n"
            '    """Add two integers.\n\n'
            "    Args:\n"
            "        x: First number.\n"
            "        y: Second number.\n\n"
            "    Returns:\n"
            "        int: The sum.\n"
            '    """\n'
            "    return x + y\n"
        )
        issues = analyse_file(source, "math_utils.py", docstyle="google")
        assert not issues

    def test_public_function_without_docstring_flagged(self) -> None:
        source = "def public_func(x: int) -> None:\n    pass\n"
        issues = analyse_file(source, "test.py", docstyle="auto")
        assert any(i.kind == "incomplete" and "no docstring" in i.detail.lower() for i in issues)

    def test_private_function_without_docstring_not_flagged(self) -> None:
        source = "def _private_func(x: int) -> None:\n    pass\n"
        issues = analyse_file(source, "test.py", docstyle="auto")
        # Private functions are not required to have docstrings
        assert not any(i.kind == "incomplete" for i in issues)

    def test_wrong_style_flagged(self) -> None:
        # Google-style doc with numpy expected
        source = 'def foo(x: int) -> None:\n    """Do something.\n\n    Args:\n        x: value.\n    """\n    pass\n'
        issues = analyse_file(source, "test.py", docstyle="numpy")
        assert any(i.kind == "wrong_style" for i in issues)

    def test_syntax_error_returns_empty(self) -> None:
        source = "def (broken syntax:\n"
        issues = analyse_file(source, "bad.py")
        assert issues == []


# ---------------------------------------------------------------------------
# analyse() integration
# ---------------------------------------------------------------------------


class TestAnalyse:
    def test_clean_file_passes(self, tmp_path: Path) -> None:
        src = tmp_path / "clean.py"
        src.write_text(
            "def greet(name: str) -> str:\n"
            '    """Greet a person by name.\n\n'
            "    Args:\n"
            "        name: The person's name.\n\n"
            "    Returns:\n"
            "        str: Greeting message.\n"
            '    """\n'
            '    return f"Hello, {name}!"\n'
        )
        report = analyse(["clean.py"], tmp_path, docstyle="google")
        assert report.passed

    def test_incomplete_docstring_caught(self, tmp_path: Path) -> None:
        src = tmp_path / "incomplete.py"
        src.write_text("def foo(x: int) -> int:\n    '''Short.'''\n    return x\n")
        report = analyse(["incomplete.py"], tmp_path, docstyle="google")
        assert not report.passed or report.issues  # issues found

    def test_missing_file_skipped_gracefully(self, tmp_path: Path) -> None:
        report = analyse(["nonexistent.py"], tmp_path)
        assert isinstance(report, CommentQualityReport)

    def test_report_summary_no_issues(self, tmp_path: Path) -> None:
        src = tmp_path / "good.py"
        src.write_text(
            "def foo(x: int) -> int:\n"
            '    """Add one.\n\n'
            "    Args:\n"
            "        x: Input.\n\n"
            "    Returns:\n"
            "        int: Result.\n"
            '    """\n'
            "    return x + 1\n"
        )
        report = analyse(["good.py"], tmp_path, docstyle="google")
        assert "OK" in report.summary() or "issue" not in report.summary().lower()
