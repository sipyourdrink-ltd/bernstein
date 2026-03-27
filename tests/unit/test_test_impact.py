"""Unit tests for scripts/test_impact.py — incremental test impact analysis."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

# Make scripts/ importable
_SCRIPTS = Path(__file__).parent.parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from test_impact import (  # noqa: E402
    _cache_is_fresh,
    _extract_bernstein_imports,
    build_dep_map,
    get_affected_tests,
)

# ---------------------------------------------------------------------------
# _path_to_module
# ---------------------------------------------------------------------------


class TestPathToModule:
    def test_simple_module(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        (src / "bernstein" / "core").mkdir(parents=True)
        f = src / "bernstein" / "core" / "models.py"
        f.touch()

        import test_impact as ti  # type: ignore[import]

        orig = ti.SRC_ROOT
        ti.SRC_ROOT = src
        try:
            assert ti._path_to_module(f) == "bernstein.core.models"
        finally:
            ti.SRC_ROOT = orig

    def test_init_module(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        (src / "bernstein" / "core").mkdir(parents=True)
        f = src / "bernstein" / "core" / "__init__.py"
        f.touch()

        import test_impact as ti  # type: ignore[import]

        orig = ti.SRC_ROOT
        ti.SRC_ROOT = src
        try:
            assert ti._path_to_module(f) == "bernstein.core"
        finally:
            ti.SRC_ROOT = orig


# ---------------------------------------------------------------------------
# _extract_bernstein_imports
# ---------------------------------------------------------------------------


class TestExtractBernsteinImports:
    def test_regular_import(self, tmp_path: Path) -> None:
        f = tmp_path / "test_x.py"
        f.write_text("import bernstein.core.models\n")
        # Regular 'import X.Y.Z' only records the full name; no parent expansion
        assert _extract_bernstein_imports(f) == {"bernstein.core.models"}

    def test_from_import(self, tmp_path: Path) -> None:
        f = tmp_path / "test_x.py"
        f.write_text("from bernstein.core.models import Task\n")
        result = _extract_bernstein_imports(f)
        assert "bernstein.core.models" in result
        # Parents are NOT expanded here; get_affected_tests handles parent matching
        assert "bernstein.core" not in result
        assert "bernstein" not in result

    def test_non_bernstein_ignored(self, tmp_path: Path) -> None:
        f = tmp_path / "test_x.py"
        f.write_text("import os\nfrom pathlib import Path\nimport pytest\n")
        assert _extract_bernstein_imports(f) == set()

    def test_mixed_imports(self, tmp_path: Path) -> None:
        f = tmp_path / "test_x.py"
        f.write_text(
            textwrap.dedent("""\
            import os
            from bernstein.core import orchestrator
            from bernstein.adapters.claude import ClaudeAdapter
            import pytest
            """)
        )
        result = _extract_bernstein_imports(f)
        # Exact module names only, no parent expansion
        assert "bernstein.core" in result
        assert "bernstein.adapters.claude" in result
        assert "bernstein" not in result
        assert "bernstein.adapters" not in result

    def test_syntax_error_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        f.write_text("def (:\n")
        assert _extract_bernstein_imports(f) == set()


# ---------------------------------------------------------------------------
# build_dep_map
# ---------------------------------------------------------------------------


class TestBuildDepMap:
    def _make_layout(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        """Create minimal src + tests layout."""
        src = tmp_path / "src"
        (src / "bernstein" / "core").mkdir(parents=True)
        models = src / "bernstein" / "core" / "models.py"
        models.write_text("class Task: pass\n")
        (src / "bernstein" / "core" / "__init__.py").touch()
        (src / "bernstein" / "__init__.py").touch()

        test_dir = tmp_path / "tests" / "unit"
        test_dir.mkdir(parents=True)
        test_models = test_dir / "test_models.py"
        test_models.write_text("from bernstein.core.models import Task\n")
        test_other = test_dir / "test_other.py"
        test_other.write_text("import os\n")

        return src, test_dir, models

    def test_basic_build(self, tmp_path: Path) -> None:
        import test_impact as ti  # type: ignore[import]

        src, test_dir, _ = self._make_layout(tmp_path)
        orig_src, orig_root = ti.SRC_ROOT, ti.ROOT
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        try:
            dep_map = build_dep_map(test_dirs=[test_dir])
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root

        assert dep_map["version"] == "1"
        assert any("test_models.py" in k for k in dep_map["test_deps"])

    def test_imports_captured(self, tmp_path: Path) -> None:
        import test_impact as ti  # type: ignore[import]

        src, test_dir, _ = self._make_layout(tmp_path)
        orig_src, orig_root = ti.SRC_ROOT, ti.ROOT
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        try:
            dep_map = build_dep_map(test_dirs=[test_dir])
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root

        test_key = next(k for k in dep_map["test_deps"] if "test_models.py" in k)
        assert "bernstein.core.models" in dep_map["test_deps"][test_key]["imports"]

    def test_no_imports_test(self, tmp_path: Path) -> None:
        import test_impact as ti  # type: ignore[import]

        src, test_dir, _ = self._make_layout(tmp_path)
        orig_src, orig_root = ti.SRC_ROOT, ti.ROOT
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        try:
            dep_map = build_dep_map(test_dirs=[test_dir])
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root

        test_key = next(k for k in dep_map["test_deps"] if "test_other.py" in k)
        assert dep_map["test_deps"][test_key]["imports"] == []

    def test_source_imports_captured(self, tmp_path: Path) -> None:
        import test_impact as ti  # type: ignore[import]

        src, test_dir, _models = self._make_layout(tmp_path)
        # Add a source file that imports models
        context = src / "bernstein" / "core" / "context.py"
        context.write_text("from bernstein.core.models import Task\n")

        orig_src, orig_root = ti.SRC_ROOT, ti.ROOT
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        try:
            dep_map = build_dep_map(test_dirs=[test_dir])
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root

        assert "bernstein.core.context" in dep_map["source_imports"]
        assert "bernstein.core.models" in dep_map["source_imports"]["bernstein.core.context"]["imports"]


# ---------------------------------------------------------------------------
# _cache_is_fresh
# ---------------------------------------------------------------------------


class TestCacheIsFresh:
    def test_fresh_with_matching_hashes(self, tmp_path: Path) -> None:
        import test_impact as ti  # type: ignore[import]

        src = tmp_path / "src"
        (src / "bernstein").mkdir(parents=True)
        (src / "bernstein" / "__init__.py").touch()

        test_dir = tmp_path / "tests" / "unit"
        test_dir.mkdir(parents=True)
        test_file = test_dir / "test_foo.py"
        test_file.write_text("import bernstein\n")

        orig_src, orig_root, orig_dirs = ti.SRC_ROOT, ti.ROOT, ti.TEST_DIRS
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        ti.TEST_DIRS = [test_dir]
        try:
            dep_map = build_dep_map(test_dirs=[test_dir])
            assert _cache_is_fresh(dep_map)
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root
            ti.TEST_DIRS = orig_dirs

    def test_stale_when_test_changed(self, tmp_path: Path) -> None:
        import test_impact as ti  # type: ignore[import]

        src = tmp_path / "src"
        (src / "bernstein").mkdir(parents=True)
        (src / "bernstein" / "__init__.py").touch()

        test_dir = tmp_path / "tests" / "unit"
        test_dir.mkdir(parents=True)
        test_file = test_dir / "test_foo.py"
        test_file.write_text("import bernstein\n")

        orig_src, orig_root, orig_dirs = ti.SRC_ROOT, ti.ROOT, ti.TEST_DIRS
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        ti.TEST_DIRS = [test_dir]
        try:
            dep_map = build_dep_map(test_dirs=[test_dir])
            # Mutate the test file
            test_file.write_text("import bernstein\nimport os\n")
            assert not _cache_is_fresh(dep_map)
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root
            ti.TEST_DIRS = orig_dirs

    def test_wrong_version_is_stale(self) -> None:
        assert not _cache_is_fresh({"version": "0", "test_deps": {}, "source_imports": {}})


# ---------------------------------------------------------------------------
# get_affected_tests
# ---------------------------------------------------------------------------


class TestGetAffectedTests:
    def _make_dep_map(
        self,
        tmp_path: Path,
        *,
        src: Path,
    ) -> dict:
        """Return a dep_map with test_models.py → models, test_other.py → nothing."""
        test_models = tmp_path / "tests" / "unit" / "test_models.py"
        test_other = tmp_path / "tests" / "unit" / "test_other.py"
        test_models.parent.mkdir(parents=True, exist_ok=True)
        test_models.write_text("from bernstein.core.models import Task\n")
        test_other.write_text("import os\n")

        (src / "bernstein" / "core").mkdir(parents=True, exist_ok=True)
        (src / "bernstein" / "__init__.py").touch()
        (src / "bernstein" / "core" / "__init__.py").touch()
        (src / "bernstein" / "core" / "models.py").write_text("class Task: pass\n")

        import test_impact as ti  # type: ignore[import]

        orig_src, orig_root = ti.SRC_ROOT, ti.ROOT
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        try:
            return build_dep_map(test_dirs=[test_models.parent])
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root

    def test_source_change_triggers_test(self, tmp_path: Path) -> None:
        import test_impact as ti  # type: ignore[import]

        src = tmp_path / "src"
        dep_map = self._make_dep_map(tmp_path, src=src)

        orig_src, orig_root = ti.SRC_ROOT, ti.ROOT
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        try:
            changed = [str((src / "bernstein" / "core" / "models.py").relative_to(tmp_path))]
            affected = get_affected_tests(changed, dep_map)
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root

        assert any("test_models.py" in str(p) for p in affected)
        assert not any("test_other.py" in str(p) for p in affected)

    def test_unrelated_source_change(self, tmp_path: Path) -> None:
        import test_impact as ti  # type: ignore[import]

        src = tmp_path / "src"
        dep_map = self._make_dep_map(tmp_path, src=src)

        # Create a source file not imported by any test
        other_src = src / "bernstein" / "core" / "orchestrator.py"
        other_src.write_text("pass\n")

        orig_src, orig_root = ti.SRC_ROOT, ti.ROOT
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        try:
            changed = [str(other_src.relative_to(tmp_path))]
            affected = get_affected_tests(changed, dep_map)
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root

        assert affected == []

    def test_conftest_change_runs_all(self, tmp_path: Path) -> None:
        import test_impact as ti  # type: ignore[import]

        src = tmp_path / "src"
        dep_map = self._make_dep_map(tmp_path, src=src)

        orig_src, orig_root = ti.SRC_ROOT, ti.ROOT
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        try:
            affected = get_affected_tests(["tests/conftest.py"], dep_map)
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root

        # All test files should be included
        assert len(affected) == len(dep_map["test_deps"])

    def test_test_file_change_includes_itself(self, tmp_path: Path) -> None:
        import test_impact as ti  # type: ignore[import]

        src = tmp_path / "src"
        dep_map = self._make_dep_map(tmp_path, src=src)

        orig_src, orig_root = ti.SRC_ROOT, ti.ROOT
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        try:
            changed = ["tests/unit/test_models.py"]
            affected = get_affected_tests(changed, dep_map)
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root

        assert any("test_models.py" in str(p) for p in affected)

    def test_transitive_dependency(self, tmp_path: Path) -> None:
        """If context.py imports models.py, tests of context should run when models changes."""
        import test_impact as ti  # type: ignore[import]

        src = tmp_path / "src"
        (src / "bernstein" / "core").mkdir(parents=True)
        (src / "bernstein" / "__init__.py").touch()
        (src / "bernstein" / "core" / "__init__.py").touch()
        models = src / "bernstein" / "core" / "models.py"
        models.write_text("class Task: pass\n")
        context = src / "bernstein" / "core" / "context.py"
        context.write_text("from bernstein.core.models import Task\n")

        test_dir = tmp_path / "tests" / "unit"
        test_dir.mkdir(parents=True)
        test_context = test_dir / "test_context.py"
        test_context.write_text("from bernstein.core.context import Task\n")

        orig_src, orig_root, orig_dirs = ti.SRC_ROOT, ti.ROOT, ti.TEST_DIRS
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        ti.TEST_DIRS = [test_dir]
        try:
            dep_map = build_dep_map(test_dirs=[test_dir])
            changed = [str(models.relative_to(tmp_path))]
            affected = get_affected_tests(changed, dep_map)
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root
            ti.TEST_DIRS = orig_dirs

        assert any("test_context.py" in str(p) for p in affected)

    def test_empty_changed_files(self, tmp_path: Path) -> None:
        import test_impact as ti  # type: ignore[import]

        src = tmp_path / "src"
        dep_map = self._make_dep_map(tmp_path, src=src)

        orig_src, orig_root = ti.SRC_ROOT, ti.ROOT
        ti.SRC_ROOT = src
        ti.ROOT = tmp_path
        try:
            affected = get_affected_tests([], dep_map)
        finally:
            ti.SRC_ROOT = orig_src
            ti.ROOT = orig_root

        assert affected == []
