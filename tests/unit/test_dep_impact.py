"""Tests for the dependency impact analyser."""

from __future__ import annotations

import ast
from pathlib import Path

from bernstein.core.api_compat_checker import BreakingChange, ChangeType, CompatReport
from bernstein.core.dep_impact import (
    CallSiteImpact,
    DepImpactReport,
    _collect_imported_names,
    _collect_module_aliases,
    _find_call_impacts,
    _rel_path_to_module,
    find_call_site_impacts,
)

# ---------------------------------------------------------------------------
# _rel_path_to_module
# ---------------------------------------------------------------------------


class TestRelPathToModule:
    def test_strips_src_prefix(self) -> None:
        assert _rel_path_to_module("src/bernstein/core/foo.py") == "bernstein.core.foo"

    def test_no_src_prefix(self) -> None:
        assert _rel_path_to_module("bernstein/core/foo.py") == "bernstein.core.foo"

    def test_windows_backslash(self) -> None:
        assert _rel_path_to_module("src\\bernstein\\core\\foo.py") == "bernstein.core.foo"

    def test_single_file(self) -> None:
        assert _rel_path_to_module("mymod.py") == "mymod"


# ---------------------------------------------------------------------------
# _collect_imported_names
# ---------------------------------------------------------------------------


class TestCollectImportedNames:
    def _parse(self, source: str) -> ast.Module:
        return ast.parse(source)

    def test_from_import(self) -> None:
        tree = self._parse("from bernstein.core.foo import bar, baz")
        result = _collect_imported_names(tree, "bernstein.core.foo", {"bar"})
        assert result == {"bar": "bar"}

    def test_aliased_import(self) -> None:
        tree = self._parse("from bernstein.core.foo import bar as b")
        result = _collect_imported_names(tree, "bernstein.core.foo", {"bar"})
        assert result == {"b": "bar"}

    def test_unrelated_import_ignored(self) -> None:
        tree = self._parse("from bernstein.core.other import bar")
        result = _collect_imported_names(tree, "bernstein.core.foo", {"bar"})
        assert result == {}

    def test_broken_symbol_filter(self) -> None:
        tree = self._parse("from bernstein.core.foo import bar, safe_func")
        result = _collect_imported_names(tree, "bernstein.core.foo", {"bar"})
        assert "safe_func" not in result

    def test_wildcard_ignored(self) -> None:
        tree = self._parse("from bernstein.core.foo import *")
        result = _collect_imported_names(tree, "bernstein.core.foo", {"bar"})
        assert result == {}


# ---------------------------------------------------------------------------
# _collect_module_aliases
# ---------------------------------------------------------------------------


class TestCollectModuleAliases:
    def _parse(self, source: str) -> ast.Module:
        return ast.parse(source)

    def test_plain_import(self) -> None:
        tree = self._parse("import bernstein.core.foo")
        aliases = _collect_module_aliases(tree, "bernstein.core.foo")
        assert "foo" in aliases

    def test_aliased_import(self) -> None:
        tree = self._parse("import bernstein.core.foo as myfoo")
        aliases = _collect_module_aliases(tree, "bernstein.core.foo")
        assert "myfoo" in aliases

    def test_unrelated_import(self) -> None:
        tree = self._parse("import bernstein.core.other")
        aliases = _collect_module_aliases(tree, "bernstein.core.foo")
        assert not aliases


# ---------------------------------------------------------------------------
# _find_call_impacts — removed function
# ---------------------------------------------------------------------------


def _make_removed_func_bc(name: str, file: str = "mod.py") -> BreakingChange:
    return BreakingChange(
        file=file,
        name=name,
        change_type=ChangeType.REMOVED_FUNCTION,
        description=f"Public function '{name}' was removed",
        line=1,
    )


def _make_removed_param_bc(func: str, param: str, file: str = "mod.py") -> BreakingChange:
    return BreakingChange(
        file=file,
        name=func,
        change_type=ChangeType.REMOVED_PARAMETER,
        description=f"Parameter '{param}' was removed from '{func}'",
        line=1,
    )


def _parse_with_line(source: str) -> ast.Module:
    return ast.parse(source)


class TestFindCallImpactsRemovedFunction:
    def _impacts(
        self,
        source: str,
        bcs: list[BreakingChange],
        imported_names: dict[str, str] | None = None,
        module_aliases: set[str] | None = None,
    ) -> list[CallSiteImpact]:
        tree = _parse_with_line(source)
        return _find_call_impacts(
            tree,
            "caller.py",
            imported_names or {},
            module_aliases or set(),
            bcs,
        )

    def test_direct_call_to_removed_function_flagged(self) -> None:
        source = "check_compatibility(old, new, fname)"
        bcs = [_make_removed_func_bc("check_compatibility")]
        impacts = self._impacts(
            source,
            bcs,
            imported_names={"check_compatibility": "check_compatibility"},
        )
        assert len(impacts) == 1
        assert impacts[0].callee_qualified == "check_compatibility"
        assert "removed" in impacts[0].reason

    def test_no_call_to_removed_function_no_impact(self) -> None:
        source = "other_func(a, b)"
        bcs = [_make_removed_func_bc("check_compatibility")]
        impacts = self._impacts(
            source,
            bcs,
            imported_names={"check_compatibility": "check_compatibility"},
        )
        assert impacts == []

    def test_module_alias_call_flagged(self) -> None:
        source = "foo.removed_func(x)"
        bcs = [_make_removed_func_bc("removed_func")]
        impacts = self._impacts(
            source,
            bcs,
            imported_names={},
            module_aliases={"foo"},
        )
        assert len(impacts) == 1

    def test_call_not_in_imported_names_ignored(self) -> None:
        source = "check_compatibility(old, new, fname)"
        bcs = [_make_removed_func_bc("check_compatibility")]
        # Not in imported_names — should be ignored
        impacts = self._impacts(source, bcs, imported_names={})
        assert impacts == []


class TestFindCallImpactsRemovedParameter:
    def _impacts(
        self,
        source: str,
        bcs: list[BreakingChange],
    ) -> list[CallSiteImpact]:
        tree = _parse_with_line(source)
        return _find_call_impacts(
            tree,
            "caller.py",
            {"connect": "connect"},
            set(),
            bcs,
        )

    def test_keyword_arg_removed_param_flagged(self) -> None:
        source = "connect(host='localhost', timeout=5)"
        bcs = [_make_removed_param_bc("connect", "timeout")]
        impacts = self._impacts(source, bcs)
        assert len(impacts) == 1
        assert "timeout" in impacts[0].reason

    def test_non_removed_keyword_not_flagged(self) -> None:
        source = "connect(host='localhost', port=5432)"
        bcs = [_make_removed_param_bc("connect", "timeout")]
        impacts = self._impacts(source, bcs)
        assert impacts == []

    def test_positional_call_not_flagged_for_removed_param(self) -> None:
        source = "connect('localhost', 5432)"
        bcs = [_make_removed_param_bc("connect", "timeout")]
        impacts = self._impacts(source, bcs)
        assert impacts == []


class TestFindCallImpactsChangedParamPosition:
    def _impacts(self, source: str) -> list[CallSiteImpact]:
        bc = BreakingChange(
            file="mod.py",
            name="connect",
            change_type=ChangeType.CHANGED_PARAM_POSITION,
            description="Required parameter 'port' moved from position 1 to 0",
            line=1,
        )
        tree = _parse_with_line(source)
        return _find_call_impacts(tree, "caller.py", {"connect": "connect"}, set(), [bc])

    def test_positional_call_with_multiple_args_flagged(self) -> None:
        impacts = self._impacts("connect('host', 5432)")
        assert len(impacts) == 1
        assert "positional" in impacts[0].reason

    def test_single_positional_arg_not_flagged(self) -> None:
        # Only one arg — position reorder doesn't matter
        impacts = self._impacts("connect('host')")
        assert impacts == []


# ---------------------------------------------------------------------------
# find_call_site_impacts — integration test with real files
# ---------------------------------------------------------------------------


class TestFindCallSiteImpacts:
    def test_no_breaking_changes_returns_empty(self, tmp_path: Path) -> None:
        report = CompatReport()
        impacts = find_call_site_impacts(tmp_path, report, [])
        assert impacts == []

    def test_detects_broken_call_in_dependent_file(self, tmp_path: Path) -> None:
        # Create a "changed" module with a known breaking change
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (tmp_path / "__init__.py").write_text("")
        (pkg / "__init__.py").write_text("")
        (pkg / "api.py").write_text("def greet(name: str) -> str: ...")

        # Create a caller that imports and uses the changed function
        caller = tmp_path / "caller.py"
        caller.write_text("from mypkg.api import greet\n\nresult = greet(name='Alice')\n")

        bc = BreakingChange(
            file="mypkg/api.py",
            name="greet",
            change_type=ChangeType.REMOVED_PARAMETER,
            description="Parameter 'name' was removed from 'greet'",
            line=1,
        )
        compat = CompatReport(breaking_changes=[bc])
        impacts = find_call_site_impacts(tmp_path, compat, ["mypkg/api.py"])

        assert len(impacts) == 1
        assert impacts[0].caller_file == "caller.py"
        assert "name" in impacts[0].reason

    def test_changed_file_excluded_from_scan(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        # The changed file itself calls the function internally
        changed = pkg / "api.py"
        changed.write_text("def greet(name: str) -> str: ...\nfrom mypkg.api import greet\ngreet(name='hi')\n")
        bc = BreakingChange(
            file="mypkg/api.py",
            name="greet",
            change_type=ChangeType.REMOVED_PARAMETER,
            description="Parameter 'name' was removed from 'greet'",
            line=1,
        )
        compat = CompatReport(breaking_changes=[bc])
        # "mypkg/api.py" is in changed_files → excluded
        impacts = find_call_site_impacts(tmp_path, compat, ["mypkg/api.py"])
        assert impacts == []


# ---------------------------------------------------------------------------
# DepImpactReport
# ---------------------------------------------------------------------------


class TestDepImpactReport:
    def test_empty_report_is_safe(self) -> None:
        report = DepImpactReport()
        assert not report.blocks_merge

    def test_api_breaking_blocks_merge(self) -> None:
        bc = _make_removed_func_bc("foo")
        report = DepImpactReport(api_breaking=[bc])
        assert report.blocks_merge

    def test_call_site_impact_blocks_merge(self) -> None:
        ci = CallSiteImpact(
            caller_file="caller.py",
            caller_line=10,
            callee_qualified="foo",
            reason="calls removed symbol",
        )
        report = DepImpactReport(call_site_impacts=[ci])
        assert report.blocks_merge
