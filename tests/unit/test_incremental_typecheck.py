"""Tests for incremental type-checking with dependency tracing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.quality.incremental_typecheck import (
    ImportGraph,
    TypeCheckResult,
    TypeCheckScope,
    _extract_imports_from_file,
    _filepath_to_dotted,
    _is_project_import,
    _module_to_filepath,
    build_import_graph,
    compute_typecheck_scope,
    find_dependents,
    render_scope_summary,
    run_incremental_typecheck,
)

# ---------------------------------------------------------------------------
# ImportGraph dataclass
# ---------------------------------------------------------------------------


class TestImportGraph:
    def test_default_empty(self) -> None:
        graph = ImportGraph()
        assert graph.modules == {}

    def test_modules_mutable(self) -> None:
        graph = ImportGraph()
        graph.modules["a.py"] = {"b.py"}
        assert "a.py" in graph.modules
        assert graph.modules["a.py"] == {"b.py"}


# ---------------------------------------------------------------------------
# TypeCheckScope frozen dataclass
# ---------------------------------------------------------------------------


class TestTypeCheckScope:
    def test_frozen(self) -> None:
        scope = TypeCheckScope(
            changed_files=("a.py",),
            dependent_files=("b.py",),
            total_files=10,
            reduction_pct=80.0,
        )
        with pytest.raises(AttributeError):
            scope.total_files = 5  # type: ignore[misc]

    def test_fields(self) -> None:
        scope = TypeCheckScope(
            changed_files=("x.py",),
            dependent_files=("y.py", "z.py"),
            total_files=100,
            reduction_pct=97.0,
        )
        assert scope.changed_files == ("x.py",)
        assert scope.dependent_files == ("y.py", "z.py")
        assert scope.total_files == 100
        assert scope.reduction_pct == 97.0


# ---------------------------------------------------------------------------
# TypeCheckResult frozen dataclass
# ---------------------------------------------------------------------------


class TestTypeCheckResult:
    def test_frozen(self) -> None:
        scope = TypeCheckScope((), (), 0, 0.0)
        result = TypeCheckResult(scope=scope, errors=(), passed=True, duration_s=0.1)
        with pytest.raises(AttributeError):
            result.passed = False  # type: ignore[misc]

    def test_fields(self) -> None:
        scope = TypeCheckScope(("a.py",), (), 5, 80.0)
        result = TypeCheckResult(
            scope=scope,
            errors=("error: foo",),
            passed=False,
            duration_s=1.5,
        )
        assert result.errors == ("error: foo",)
        assert result.passed is False
        assert result.duration_s == 1.5


# ---------------------------------------------------------------------------
# _is_project_import
# ---------------------------------------------------------------------------


class TestIsProjectImport:
    def test_stdlib_rejected(self) -> None:
        assert _is_project_import("os") is False
        assert _is_project_import("sys") is False
        assert _is_project_import("json") is False
        assert _is_project_import("pathlib") is False

    def test_stdlib_submodule_rejected(self) -> None:
        assert _is_project_import("os.path") is False
        assert _is_project_import("collections.abc") is False

    def test_project_import_accepted(self) -> None:
        assert _is_project_import("bernstein") is True
        assert _is_project_import("bernstein.core.foo") is True

    def test_third_party_accepted(self) -> None:
        # Third-party that isn't in stdlib list is treated as project-level
        # (will fail to resolve later, which is fine).
        assert _is_project_import("requests") is True

    def test_future_rejected(self) -> None:
        assert _is_project_import("__future__") is False


# ---------------------------------------------------------------------------
# _extract_imports_from_file
# ---------------------------------------------------------------------------


class TestExtractImportsFromFile:
    def test_import_statement(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        f.write_text("import bernstein.core.foo\n")
        result = _extract_imports_from_file(f)
        assert "bernstein.core.foo" in result

    def test_from_import(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        f.write_text("from bernstein.core import bar\n")
        result = _extract_imports_from_file(f)
        assert "bernstein.core" in result

    def test_stdlib_excluded(self, tmp_path: Path) -> None:
        f = tmp_path / "mod.py"
        f.write_text("import os\nimport json\n")
        result = _extract_imports_from_file(f)
        assert result == set()

    def test_syntax_error_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text("def foo(:\n")
        result = _extract_imports_from_file(f)
        assert result == set()

    def test_nonexistent_file_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "nonexistent.py"
        result = _extract_imports_from_file(f)
        assert result == set()


# ---------------------------------------------------------------------------
# _filepath_to_dotted
# ---------------------------------------------------------------------------


class TestFilepathToDotted:
    def test_src_prefix_stripped(self) -> None:
        assert _filepath_to_dotted("src/bernstein/core/foo.py") == "bernstein.core.foo"

    def test_no_src_prefix(self) -> None:
        assert _filepath_to_dotted("bernstein/core/foo.py") == "bernstein.core.foo"

    def test_init_py(self) -> None:
        assert _filepath_to_dotted("src/bernstein/core/__init__.py") == "bernstein.core"

    def test_backslash(self) -> None:
        assert _filepath_to_dotted("src\\bernstein\\core\\foo.py") == "bernstein.core.foo"


# ---------------------------------------------------------------------------
# _module_to_filepath
# ---------------------------------------------------------------------------


class TestModuleToFilepath:
    def test_resolves_regular_module(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        target = tmp_path / "src" / "pkg" / "mod.py"
        target.write_text("")
        result = _module_to_filepath("pkg.mod", tmp_path)
        assert result is not None
        assert result.endswith("pkg/mod.py")

    def test_resolves_init(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
        result = _module_to_filepath("pkg", tmp_path)
        assert result is not None
        assert "__init__.py" in result

    def test_returns_none_for_missing(self, tmp_path: Path) -> None:
        result = _module_to_filepath("nonexistent.module", tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# build_import_graph
# ---------------------------------------------------------------------------


class TestBuildImportGraph:
    def test_simple_graph(self, tmp_path: Path) -> None:
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        (src / "__init__.py").write_text("")
        (src / "a.py").write_text("from pkg import b\n")
        (src / "b.py").write_text("")
        graph = build_import_graph(tmp_path)
        assert "src/pkg/a.py" in graph.modules
        assert "src/pkg/b.py" in graph.modules.get("src/pkg/a.py", set()) or "src/pkg/__init__.py" in graph.modules.get(
            "src/pkg/a.py", set()
        )

    def test_empty_project(self, tmp_path: Path) -> None:
        graph = build_import_graph(tmp_path)
        assert graph.modules == {}

    def test_skips_pycache(self, tmp_path: Path) -> None:
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "mod.py").write_text("import bernstein")
        graph = build_import_graph(tmp_path)
        assert not any("__pycache__" in k for k in graph.modules)

    def test_skips_hidden_dirs(self, tmp_path: Path) -> None:
        hidden = tmp_path / ".venv"
        hidden.mkdir()
        (hidden / "mod.py").write_text("import bernstein")
        graph = build_import_graph(tmp_path)
        assert not any(".venv" in k for k in graph.modules)


# ---------------------------------------------------------------------------
# find_dependents
# ---------------------------------------------------------------------------


class TestFindDependents:
    def _make_graph(self) -> ImportGraph:
        """Build a graph: c -> b -> a, d -> a (d also depends on a)."""
        return ImportGraph(
            modules={
                "a.py": set(),
                "b.py": {"a.py"},
                "c.py": {"b.py"},
                "d.py": {"a.py"},
            }
        )

    def test_direct_dependents(self) -> None:
        graph = self._make_graph()
        deps = find_dependents(["a.py"], graph)
        assert "b.py" in deps
        assert "d.py" in deps

    def test_transitive_dependents(self) -> None:
        graph = self._make_graph()
        deps = find_dependents(["a.py"], graph)
        # c.py imports b.py which imports a.py => c.py is transitively dependent
        assert "c.py" in deps

    def test_changed_files_excluded(self) -> None:
        graph = self._make_graph()
        deps = find_dependents(["a.py"], graph)
        assert "a.py" not in deps

    def test_no_dependents(self) -> None:
        graph = self._make_graph()
        deps = find_dependents(["c.py"], graph)
        assert deps == []

    def test_empty_changed(self) -> None:
        graph = self._make_graph()
        deps = find_dependents([], graph)
        assert deps == []

    def test_empty_graph(self) -> None:
        graph = ImportGraph()
        deps = find_dependents(["a.py"], graph)
        assert deps == []

    def test_sorted_output(self) -> None:
        graph = ImportGraph(
            modules={
                "a.py": set(),
                "z.py": {"a.py"},
                "m.py": {"a.py"},
            }
        )
        deps = find_dependents(["a.py"], graph)
        assert deps == sorted(deps)

    def test_diamond_dependency(self) -> None:
        """Diamond: d -> b -> a, d -> c -> a.  Changing a should find b, c, d."""
        graph = ImportGraph(
            modules={
                "a.py": set(),
                "b.py": {"a.py"},
                "c.py": {"a.py"},
                "d.py": {"b.py", "c.py"},
            }
        )
        deps = find_dependents(["a.py"], graph)
        assert set(deps) == {"b.py", "c.py", "d.py"}


# ---------------------------------------------------------------------------
# compute_typecheck_scope
# ---------------------------------------------------------------------------


class TestComputeTypecheckScope:
    def test_reduction_percentage(self, tmp_path: Path) -> None:
        # Create 10 standalone files, change 1 => 90% reduction
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        for i in range(10):
            (src / f"mod{i}.py").write_text("")
        scope = compute_typecheck_scope(["src/pkg/mod0.py"], tmp_path)
        assert scope.total_files == 10
        assert scope.reduction_pct == 90.0
        assert scope.changed_files == ("src/pkg/mod0.py",)

    def test_empty_changed(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("")
        scope = compute_typecheck_scope([], tmp_path)
        assert scope.changed_files == ()
        assert scope.dependent_files == ()
        assert scope.reduction_pct >= 0.0

    def test_no_reduction_when_all_changed(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        scope = compute_typecheck_scope(["a.py", "b.py"], tmp_path)
        assert scope.reduction_pct == 0.0


# ---------------------------------------------------------------------------
# run_incremental_typecheck
# ---------------------------------------------------------------------------


class TestRunIncrementalTypecheck:
    def _scope(self, changed: tuple[str, ...] = ("a.py",)) -> TypeCheckScope:
        return TypeCheckScope(
            changed_files=changed,
            dependent_files=(),
            total_files=10,
            reduction_pct=90.0,
        )

    @patch("bernstein.core.quality.incremental_typecheck.subprocess.run")
    def test_passes_when_returncode_zero(self, mock_run: patch) -> None:  # type: ignore[type-arg]
        mock_run.return_value = subprocess.CompletedProcess(
            args=["pyright", "a.py"],
            returncode=0,
            stdout="",
            stderr="",
        )
        result = run_incremental_typecheck(self._scope(), Path("."))
        assert result.passed is True
        assert result.errors == ()

    @patch("bernstein.core.quality.incremental_typecheck.subprocess.run")
    def test_fails_when_returncode_nonzero(self, mock_run: patch) -> None:  # type: ignore[type-arg]
        mock_run.return_value = subprocess.CompletedProcess(
            args=["pyright", "a.py"],
            returncode=1,
            stdout="a.py:10:5 - error: Type mismatch\n",
            stderr="",
        )
        result = run_incremental_typecheck(self._scope(), Path("."))
        assert result.passed is False
        assert len(result.errors) == 1

    @patch("bernstein.core.quality.incremental_typecheck.subprocess.run")
    def test_custom_command(self, mock_run: patch) -> None:  # type: ignore[type-arg]
        mock_run.return_value = subprocess.CompletedProcess(
            args=["mypy", "a.py"],
            returncode=0,
            stdout="",
            stderr="",
        )
        run_incremental_typecheck(self._scope(), Path("."), command="mypy")
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "mypy"

    def test_empty_scope_passes(self) -> None:
        scope = TypeCheckScope(
            changed_files=(),
            dependent_files=(),
            total_files=10,
            reduction_pct=100.0,
        )
        result = run_incremental_typecheck(scope, Path("."))
        assert result.passed is True
        assert result.duration_s == 0.0

    @patch("bernstein.core.quality.incremental_typecheck.subprocess.run")
    def test_command_not_found(self, mock_run: patch) -> None:  # type: ignore[type-arg]
        mock_run.side_effect = FileNotFoundError("not found")
        result = run_incremental_typecheck(self._scope(), Path("."), command="nonexistent")
        assert result.passed is False
        assert "not found" in result.errors[0].lower()

    @patch("bernstein.core.quality.incremental_typecheck.subprocess.run")
    def test_timeout(self, mock_run: patch) -> None:  # type: ignore[type-arg]
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="pyright", timeout=300)
        result = run_incremental_typecheck(self._scope(), Path("."))
        assert result.passed is False
        assert "timed out" in result.errors[0].lower()

    @patch("bernstein.core.quality.incremental_typecheck.subprocess.run")
    def test_includes_dependent_files(self, mock_run: patch) -> None:  # type: ignore[type-arg]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        scope = TypeCheckScope(
            changed_files=("a.py",),
            dependent_files=("b.py", "c.py"),
            total_files=10,
            reduction_pct=70.0,
        )
        run_incremental_typecheck(scope, Path("."))
        call_args = mock_run.call_args[0][0]
        assert "a.py" in call_args
        assert "b.py" in call_args
        assert "c.py" in call_args

    @patch("bernstein.core.quality.incremental_typecheck.subprocess.run")
    def test_extracts_error_lines(self, mock_run: patch) -> None:  # type: ignore[type-arg]
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="src/a.py:1:1 - error: Missing return\nsrc/a.py:5:3 - error: Bad type\ninfo: found 2 errors\n",
            stderr="",
        )
        result = run_incremental_typecheck(self._scope(), Path("."))
        # "error" appears in all 3 lines (including the info line)
        assert len(result.errors) >= 2


# ---------------------------------------------------------------------------
# render_scope_summary
# ---------------------------------------------------------------------------


class TestRenderScopeSummary:
    def test_contains_reduction(self) -> None:
        scope = TypeCheckScope(
            changed_files=("a.py",),
            dependent_files=("b.py",),
            total_files=100,
            reduction_pct=98.0,
        )
        md = render_scope_summary(scope)
        assert "98.0% reduction" in md

    def test_lists_changed_files(self) -> None:
        scope = TypeCheckScope(
            changed_files=("src/foo.py",),
            dependent_files=(),
            total_files=50,
            reduction_pct=98.0,
        )
        md = render_scope_summary(scope)
        assert "`src/foo.py`" in md

    def test_lists_dependent_files(self) -> None:
        scope = TypeCheckScope(
            changed_files=("a.py",),
            dependent_files=("b.py", "c.py"),
            total_files=50,
            reduction_pct=94.0,
        )
        md = render_scope_summary(scope)
        assert "`b.py`" in md
        assert "`c.py`" in md
        assert "Dependents (2)" in md

    def test_empty_scope(self) -> None:
        scope = TypeCheckScope(
            changed_files=(),
            dependent_files=(),
            total_files=50,
            reduction_pct=100.0,
        )
        md = render_scope_summary(scope)
        assert "No files in scope" in md

    def test_heading(self) -> None:
        scope = TypeCheckScope(("a.py",), (), 10, 90.0)
        md = render_scope_summary(scope)
        assert "## Incremental Type-Check Scope" in md

    def test_scoped_count(self) -> None:
        scope = TypeCheckScope(
            changed_files=("a.py",),
            dependent_files=("b.py", "c.py"),
            total_files=20,
            reduction_pct=85.0,
        )
        md = render_scope_summary(scope)
        assert "3 / 20" in md
