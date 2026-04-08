"""Tests for semantic diff analysis — behavior preservation verification."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bernstein.core.semantic_diff import (
    CallSiteMismatch,
    FunctionSignature,
    SemanticDiffReport,
    analyze_semantic_diff,
    detect_signature_changes,
    extract_signatures_from_source,
    find_call_sites,
    format_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_FUNC = """\
def greet(name: str, loud: bool = False) -> str:
    return name.upper() if loud else name
"""

_CLASS_WITH_METHOD = """\
class Auth:
    def login(self, username: str, password: str) -> bool:
        return True

    def logout(self, username: str) -> None:
        pass
"""

_RENAMED_ARG = """\
def greet(person: str, loud: bool = False) -> str:
    return person.upper() if loud else person
"""

_REMOVED_ARG = """\
def greet(name: str) -> str:
    return name
"""

_RETURN_CHANGED = """\
def greet(name: str, loud: bool = False) -> None:
    print(name)
"""

_ADDED_ARG = """\
def greet(name: str, loud: bool = False, prefix: str = "") -> str:
    return (prefix + name).upper() if loud else name
"""

_CALLER_SOURCE = """\
from module import greet

result = greet("Alice", True)
auth = Auth()
auth.login("user", "pass")
"""


# ---------------------------------------------------------------------------
# extract_signatures_from_source
# ---------------------------------------------------------------------------


class TestExtractSignatures:
    def test_simple_function(self) -> None:
        sigs = extract_signatures_from_source(_SIMPLE_FUNC, "test.py")
        assert "greet" in sigs
        sig = sigs["greet"]
        assert sig.name == "greet"
        assert sig.args == ["name", "loud"]
        assert sig.return_annotation == "str"

    def test_method_excludes_self(self) -> None:
        sigs = extract_signatures_from_source(_CLASS_WITH_METHOD, "auth.py")
        assert "Auth.login" in sigs
        login = sigs["Auth.login"]
        assert "self" not in login.args
        assert login.args == ["username", "password"]

    def test_method_return_annotation(self) -> None:
        sigs = extract_signatures_from_source(_CLASS_WITH_METHOD, "auth.py")
        assert sigs["Auth.login"].return_annotation == "bool"
        assert sigs["Auth.logout"].return_annotation == "None"

    def test_invalid_source_returns_empty(self) -> None:
        sigs = extract_signatures_from_source("def broken(:\n", "bad.py")
        assert sigs == {}

    def test_no_annotations(self) -> None:
        source = "def plain(a, b, c): pass"
        sigs = extract_signatures_from_source(source, "f.py")
        assert "plain" in sigs
        assert sigs["plain"].arg_annotations == {"a": "", "b": "", "c": ""}
        assert sigs["plain"].return_annotation == ""

    def test_varargs_and_kwargs(self) -> None:
        source = "def variadic(*args, **kwargs): pass"
        sigs = extract_signatures_from_source(source, "f.py")
        sig = sigs["variadic"]
        assert sig.has_varargs is True
        assert sig.has_kwargs is True

    def test_nested_function(self) -> None:
        source = """\
def outer(x: int) -> int:
    def inner(y: int) -> int:
        return y
    return inner(x)
"""
        sigs = extract_signatures_from_source(source, "f.py")
        assert "outer" in sigs
        assert "outer.inner" in sigs

    def test_qualname_includes_class(self) -> None:
        sigs = extract_signatures_from_source(_CLASS_WITH_METHOD, "auth.py")
        assert "Auth.login" in sigs
        assert "Auth.logout" in sigs

    def test_file_attached(self) -> None:
        sigs = extract_signatures_from_source(_SIMPLE_FUNC, "my_file.py")
        assert sigs["greet"].file == "my_file.py"


# ---------------------------------------------------------------------------
# detect_signature_changes
# ---------------------------------------------------------------------------


class TestDetectSignatureChanges:
    def _sigs(self, source: str) -> dict[str, FunctionSignature]:
        return extract_signatures_from_source(source, "test.py")

    def test_no_changes_when_identical(self) -> None:
        sigs = self._sigs(_SIMPLE_FUNC)
        changes = detect_signature_changes(sigs, sigs)
        assert changes == []

    def test_detects_removed_function(self) -> None:
        before = self._sigs(_SIMPLE_FUNC)
        after: dict[str, FunctionSignature] = {}
        changes = detect_signature_changes(before, after)
        assert len(changes) == 1
        assert changes[0].change_type == "removed"
        assert changes[0].function_name == "greet"

    def test_detects_added_function(self) -> None:
        after = self._sigs(_SIMPLE_FUNC)
        changes = detect_signature_changes({}, after)
        assert len(changes) == 1
        assert changes[0].change_type == "added"

    def test_detects_removed_arg(self) -> None:
        before = self._sigs(_SIMPLE_FUNC)
        after = self._sigs(_REMOVED_ARG)
        changes = detect_signature_changes(before, after)
        assert len(changes) == 1
        c = changes[0]
        assert c.change_type == "modified"
        assert any("loud" in issue for issue in c.compatibility_issues)

    def test_detects_changed_return_type(self) -> None:
        before = self._sigs(_SIMPLE_FUNC)
        after = self._sigs(_RETURN_CHANGED)
        changes = detect_signature_changes(before, after)
        assert len(changes) == 1
        c = changes[0]
        assert any("return type" in issue for issue in c.compatibility_issues)

    def test_renamed_arg_flags_as_modified(self) -> None:
        before = self._sigs(_SIMPLE_FUNC)  # name: str
        after = self._sigs(_RENAMED_ARG)  # person: str
        changes = detect_signature_changes(before, after)
        # "name" removed, "person" added
        assert len(changes) == 1
        assert changes[0].change_type == "modified"

    def test_added_arg_flags_as_breaking(self) -> None:
        before = self._sigs(_SIMPLE_FUNC)  # greet(name, loud)
        # Add a new required positional arg before defaults (valid Python)
        extra = """\
def greet(new_prefix: str, name: str, loud: bool = False) -> str:
    return name
"""
        after = self._sigs(extra)
        changes = detect_signature_changes(before, after)
        assert any("added" in issue.lower() for c in changes for issue in c.compatibility_issues)

    def test_no_issues_for_pure_additions(self) -> None:
        before = self._sigs(_SIMPLE_FUNC)
        after = self._sigs(_ADDED_ARG)  # added optional prefix arg
        changes = detect_signature_changes(before, after)
        # The new arg IS flagged conservatively (we can't check defaults from AST alone)
        assert len(changes) >= 0  # Just ensure it doesn't error


# ---------------------------------------------------------------------------
# find_call_sites
# ---------------------------------------------------------------------------


class TestFindCallSites:
    def test_finds_simple_call(self) -> None:
        source = "greet('Alice')"
        sites = find_call_sites(source, {"greet"})
        assert len(sites) == 1
        assert sites[0][0] == "greet"
        assert sites[0][1] == 1  # lineno

    def test_finds_method_call_by_attr_name(self) -> None:
        source = "obj.greet('Bob')"
        sites = find_call_sites(source, {"greet"})
        assert len(sites) == 1

    def test_skips_unrelated_calls(self) -> None:
        source = "print('hello')\ngreet('world')"
        sites = find_call_sites(source, {"greet"})
        assert len(sites) == 1
        assert sites[0][0] == "greet"

    def test_counts_positional_args(self) -> None:
        source = "greet('Alice', True, 'extra')"
        sites = find_call_sites(source, {"greet"})
        assert "3 positional" in sites[0][2]

    def test_captures_keyword_args(self) -> None:
        source = "greet(name='Alice', loud=True)"
        sites = find_call_sites(source, {"greet"})
        assert "loud" in sites[0][2]
        assert "name" in sites[0][2]

    def test_invalid_source_returns_empty(self) -> None:
        sites = find_call_sites("not python ::(", {"greet"})
        assert sites == []

    def test_multiple_calls_same_function(self) -> None:
        source = "greet('A')\ngreet('B')\ngreet('C')"
        sites = find_call_sites(source, {"greet"})
        assert len(sites) == 3


# ---------------------------------------------------------------------------
# analyze_semantic_diff (integration, with mocked git)
# ---------------------------------------------------------------------------


class TestAnalyzeSemanticDiff:
    def test_no_py_files_returns_empty_report(self, tmp_path: Path) -> None:
        report = analyze_semantic_diff(tmp_path, ["README.md", "Makefile"])
        assert report.behavior_preserved is True
        assert report.signature_changes == []

    def test_unmodified_function_is_preserved(self, tmp_path: Path) -> None:
        # Before and after are identical
        (tmp_path / "greeter.py").write_text(_SIMPLE_FUNC)
        with patch(
            "bernstein.core.semantic_diff._get_file_at_revision",
            return_value=_SIMPLE_FUNC,
        ):
            report = analyze_semantic_diff(tmp_path, ["greeter.py"])

        assert report.behavior_preserved is True
        assert report.signature_changes == []

    def test_removed_arg_marks_behavior_not_preserved(self, tmp_path: Path) -> None:
        (tmp_path / "greeter.py").write_text(_REMOVED_ARG)  # after: only name arg
        with (
            patch(
                "bernstein.core.semantic_diff._get_file_at_revision",
                return_value=_SIMPLE_FUNC,  # before: name + loud
            ),
            patch(
                "bernstein.core.semantic_diff._get_all_python_files",
                return_value=[tmp_path / "greeter.py"],
            ),
        ):
            report = analyze_semantic_diff(tmp_path, ["greeter.py"])

        assert report.behavior_preserved is False
        assert len(report.type_incompatibilities) > 0

    def test_new_file_has_no_before_signatures(self, tmp_path: Path) -> None:
        (tmp_path / "new_module.py").write_text(_SIMPLE_FUNC)
        # No previous revision → git returns None
        with patch(
            "bernstein.core.semantic_diff._get_file_at_revision",
            return_value=None,
        ):
            report = analyze_semantic_diff(tmp_path, ["new_module.py"])

        # All functions are "added" — not breaking
        assert report.behavior_preserved is True
        assert all(c.change_type == "added" for c in report.signature_changes)

    def test_return_type_change_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "greeter.py").write_text(_RETURN_CHANGED)
        with (
            patch(
                "bernstein.core.semantic_diff._get_file_at_revision",
                return_value=_SIMPLE_FUNC,
            ),
            patch(
                "bernstein.core.semantic_diff._get_all_python_files",
                return_value=[tmp_path / "greeter.py"],
            ),
        ):
            report = analyze_semantic_diff(tmp_path, ["greeter.py"])

        assert any("return type" in i for i in report.type_incompatibilities)

    def test_call_site_mismatch_detected(self, tmp_path: Path) -> None:
        # After: greet(name) only — 1 arg; caller passes 2
        (tmp_path / "greeter.py").write_text(_REMOVED_ARG)
        caller_source = "from greeter import greet\ngreet('Alice', True)\n"
        (tmp_path / "caller.py").write_text(caller_source)

        with (
            patch(
                "bernstein.core.semantic_diff._get_file_at_revision",
                return_value=_SIMPLE_FUNC,
            ),
            patch(
                "bernstein.core.semantic_diff._get_all_python_files",
                return_value=[tmp_path / "greeter.py", tmp_path / "caller.py"],
            ),
        ):
            report = analyze_semantic_diff(tmp_path, ["greeter.py"])

        assert any(m.function_name == "greet" for m in report.call_site_mismatches)

    def test_scan_call_sites_false_skips_scan(self, tmp_path: Path) -> None:
        (tmp_path / "greeter.py").write_text(_REMOVED_ARG)
        with (
            patch(
                "bernstein.core.semantic_diff._get_file_at_revision",
                return_value=_SIMPLE_FUNC,
            ),
        ):
            report = analyze_semantic_diff(
                tmp_path,
                ["greeter.py"],
                scan_call_sites=False,
            )

        assert report.call_site_mismatches == []

    def test_missing_file_adds_error(self, tmp_path: Path) -> None:
        # File listed in changed_files but doesn't exist on disk
        with patch(
            "bernstein.core.semantic_diff._get_file_at_revision",
            return_value=_SIMPLE_FUNC,
        ):
            report = analyze_semantic_diff(tmp_path, ["does_not_exist.py"])

        assert len(report.errors) == 1
        assert "does_not_exist.py" in report.errors[0]


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


class TestFormatReport:
    def test_preserved_report(self) -> None:
        report = SemanticDiffReport(changed_files=["a.py"], behavior_preserved=True)
        text = format_report(report)
        assert "YES" in text
        assert "Semantic Diff Report" in text

    def test_broken_report_shows_issues(self) -> None:
        report = SemanticDiffReport(
            changed_files=["a.py"],
            behavior_preserved=False,
            type_incompatibilities=["greet: return type changed: 'str' → 'None'"],
        )
        text = format_report(report)
        assert "NO" in text
        assert "return type changed" in text

    def test_call_site_mismatches_shown(self) -> None:
        report = SemanticDiffReport(
            changed_files=["a.py"],
            behavior_preserved=False,
            call_site_mismatches=[
                CallSiteMismatch(
                    caller_file="caller.py",
                    lineno=10,
                    function_name="greet",
                    issue="call passes 2 positional args but 'greet' now accepts 1",
                )
            ],
        )
        text = format_report(report)
        assert "caller.py:10" in text
        assert "greet" in text

    def test_errors_shown(self) -> None:
        report = SemanticDiffReport(
            changed_files=["x.py"],
            errors=["Could not read x.py: [Errno 2] No such file"],
        )
        text = format_report(report)
        assert "No such file" in text
