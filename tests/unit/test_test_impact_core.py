"""Unit tests for the shared test-impact analyzer."""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.test_impact import TestImpactAnalyzer as ImpactAnalyzer

from bernstein.core.quality.test_impact import compat_get_affected_tests

REPO_ROOT = Path(__file__).resolve().parents[2]

# Guards in this repository that parse workflow YAML but are named after the
# workflow they pin rather than after the word "workflow". Each one has to be
# selected by a workflow-only change; a name-only predicate misses all three.
_WORKFLOW_GUARDS_WITHOUT_WORKFLOW_IN_NAME = (
    "tests/unit/test_bot_pull_request_tokens_yaml.py",
    "tests/unit/test_merge_queue_gate_coverage_yaml.py",
    "tests/unit/test_post_ci_dispatcher_yaml.py",
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_analyze_expands_transitive_source_dependencies(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "models.py", "class Model: pass\n")
    _write(
        tmp_path / "src" / "demo" / "service.py",
        "from demo.models import Model\n\ndef use() -> Model:\n    return Model()\n",
    )
    _write(
        tmp_path / "tests" / "unit" / "test_models.py",
        "from demo.models import Model\n\ndef test_model() -> None:\n    assert Model\n",
    )
    _write(
        tmp_path / "tests" / "unit" / "test_service.py",
        "from demo.service import use\n\ndef test_use() -> None:\n    assert use()\n",
    )

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze(["src/demo/models.py"])

    assert analysis.fallback_used is False
    assert analysis.coverage_pct == pytest.approx(100.0)
    assert analysis.affected_tests == [
        "tests/unit/test_models.py",
        "tests/unit/test_service.py",
    ]


def test_analyze_uses_name_based_mapping_when_import_graph_is_empty(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "helper.py", "def helper() -> int:\n    return 1\n")
    _write(tmp_path / "tests" / "unit" / "test_helper.py", "def test_helper() -> None:\n    assert True\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze(["src/demo/helper.py"])

    assert analysis.fallback_used is False
    assert analysis.affected_tests == ["tests/unit/test_helper.py"]
    assert analysis.mappings[0].test_files == ["tests/unit/test_helper.py"]


def test_conftest_change_falls_back_to_all_tests(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "logic.py", "def run() -> int:\n    return 1\n")
    _write(tmp_path / "tests" / "conftest.py", "import pytest\n")
    _write(tmp_path / "tests" / "unit" / "test_one.py", "def test_one() -> None:\n    assert True\n")
    _write(tmp_path / "tests" / "unit" / "test_two.py", "def test_two() -> None:\n    assert True\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests", tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze(["tests/conftest.py"])

    assert analysis.fallback_used is True
    assert analysis.coverage_pct == pytest.approx(100.0)
    assert analysis.affected_tests == [
        "tests/unit/test_one.py",
        "tests/unit/test_two.py",
    ]


def test_unmapped_source_change_falls_back_to_all_tests(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "orphan.py", "def orphan() -> int:\n    return 1\n")
    _write(tmp_path / "tests" / "unit" / "test_alpha.py", "def test_alpha() -> None:\n    assert True\n")
    _write(tmp_path / "tests" / "unit" / "test_beta.py", "def test_beta() -> None:\n    assert True\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze(["src/demo/orphan.py"])

    assert analysis.fallback_used is True
    assert analysis.affected_tests == [
        "tests/unit/test_alpha.py",
        "tests/unit/test_beta.py",
    ]


def test_direct_test_file_change_is_always_selected(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "core.py", "def run() -> int:\n    return 1\n")
    _write(tmp_path / "tests" / "unit" / "test_core.py", "def test_core() -> None:\n    assert True\n")
    _write(tmp_path / "tests" / "unit" / "test_other.py", "def test_other() -> None:\n    assert True\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze(["tests/unit/test_core.py"])

    assert analysis.fallback_used is False
    assert analysis.coverage_pct == pytest.approx(100.0)
    assert analysis.affected_tests == ["tests/unit/test_core.py"]


def test_workflow_change_selects_workflow_yaml_tests(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / ".github" / "workflows" / "ci.yml", "name: CI\n")
    _write(tmp_path / "tests" / "unit" / "test_ci_workflow_yaml.py", "def test_ci() -> None:\n    assert True\n")
    _write(
        tmp_path / "tests" / "unit" / "test_autoheal_workflow_yaml.py",
        "def test_autoheal() -> None:\n    assert True\n",
    )
    _write(tmp_path / "tests" / "unit" / "test_models.py", "def test_model() -> None:\n    assert True\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze([".github/workflows/ci.yml"])

    assert analysis.fallback_used is False
    assert analysis.coverage_pct == pytest.approx(100.0)
    assert analysis.affected_tests == [
        "tests/unit/test_autoheal_workflow_yaml.py",
        "tests/unit/test_ci_workflow_yaml.py",
    ]


def test_workflow_change_selects_guards_that_read_workflow_yaml(tmp_path: Path) -> None:
    """A guard that reads workflow YAML is selected whatever its file is called.

    Selecting on the substring ``workflow`` in the test file name only finds
    guards that happen to follow that naming convention. A guard named after
    the workflow it pins rather than after the word ``workflow`` reads the same
    YAML and breaks on the same edit, so it belongs in the same selection.
    """
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / ".github" / "workflows" / "post-ci-dispatcher.yml", "name: Dispatcher\n")
    _write(
        tmp_path / "tests" / "unit" / "test_post_ci_dispatcher_yaml.py",
        'from pathlib import Path\n\nWORKFLOW = Path(".github") / "workflows" / "post-ci-dispatcher.yml"\n',
    )
    _write(
        tmp_path / "tests" / "unit" / "test_tokens_yaml.py",
        'WORKFLOWS = ".github/workflows"\n\ndef test_tokens() -> None:\n    assert WORKFLOWS\n',
    )
    _write(tmp_path / "tests" / "unit" / "test_models.py", "def test_model() -> None:\n    assert True\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze([".github/workflows/post-ci-dispatcher.yml"])

    assert analysis.fallback_used is False
    assert analysis.affected_tests == [
        "tests/unit/test_post_ci_dispatcher_yaml.py",
        "tests/unit/test_tokens_yaml.py",
    ]


def test_dispatcher_workflow_change_selects_the_real_dispatcher_guard() -> None:
    """A change to post-ci-dispatcher.yml selects this repository's own guard.

    The synthetic cases above pin the rule; this one pins the rule against the
    files that actually ship. ``test_post_ci_dispatcher_yaml.py`` asserts the
    secrets each dispatched child job receives, so an edit to that workflow is
    exactly the edit it exists to catch, and the pull-request lane has to run
    it before the merge rather than after.
    """
    dep_map = {
        "test_deps": {guard: {"hash": "", "imports": []} for guard in _WORKFLOW_GUARDS_WITHOUT_WORKFLOW_IN_NAME},
        "source_imports": {},
    }

    affected = compat_get_affected_tests(
        [".github/workflows/post-ci-dispatcher.yml"],
        dep_map,
        root=REPO_ROOT,
        src_root=REPO_ROOT / "src",
    )

    selected = [path.relative_to(REPO_ROOT).as_posix() for path in affected]
    assert "tests/unit/test_post_ci_dispatcher_yaml.py" in selected
    assert selected == sorted(_WORKFLOW_GUARDS_WITHOUT_WORKFLOW_IN_NAME)


def test_workflow_change_leaves_tests_that_never_read_workflows(tmp_path: Path) -> None:
    """Widening the predicate must not degrade into selecting the whole suite."""
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / ".github" / "workflows" / "ci.yml", "name: CI\n")
    _write(tmp_path / "tests" / "unit" / "test_models.py", "def test_model() -> None:\n    assert True\n")
    _write(
        tmp_path / "tests" / "unit" / "test_yaml_loader.py",
        'import yaml\n\ndef test_load() -> None:\n    assert yaml.safe_load("a: 1")\n',
    )

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze([".github/workflows/ci.yml"])

    assert analysis.fallback_used is False
    assert analysis.affected_tests == []


def test_version_bump_falls_back_to_all_tests(tmp_path: Path) -> None:
    """A pyproject.toml-only change must run the full suite, not select zero tests.

    This is the regression guard for the dominant red-main mechanism: version
    bump PRs matched no selection rule, selected no tests, and merged green.
    """
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "logic.py", "def run() -> int:\n    return 1\n")
    _write(tmp_path / "pyproject.toml", '[project]\nname = "demo"\nversion = "1.0.0"\n')
    _write(tmp_path / "tests" / "unit" / "test_alpha.py", "def test_alpha() -> None:\n    assert True\n")
    _write(tmp_path / "tests" / "unit" / "test_beta.py", "def test_beta() -> None:\n    assert True\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze(["pyproject.toml"])

    assert analysis.fallback_used is True
    assert analysis.affected_tests == [
        "tests/unit/test_alpha.py",
        "tests/unit/test_beta.py",
    ]


def test_docs_only_change_falls_back_to_all_tests(tmp_path: Path) -> None:
    """A docs-only change matches no selection rule and fails open to all tests."""
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "docs" / "x.md", "# doc\n")
    _write(tmp_path / "tests" / "unit" / "test_alpha.py", "def test_alpha() -> None:\n    assert True\n")
    _write(tmp_path / "tests" / "unit" / "test_beta.py", "def test_beta() -> None:\n    assert True\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze(["docs/x.md"])

    assert analysis.fallback_used is True
    assert analysis.affected_tests == [
        "tests/unit/test_alpha.py",
        "tests/unit/test_beta.py",
    ]


def test_allowlisted_root_file_does_not_force_fallback(tmp_path: Path) -> None:
    """An inert allowlisted file (LICENSE) selects nothing without a full fallback."""
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "LICENSE", "Apache-2.0\n")
    _write(tmp_path / "tests" / "unit" / "test_alpha.py", "def test_alpha() -> None:\n    assert True\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze(["LICENSE"])

    assert analysis.fallback_used is False
    assert analysis.affected_tests == []


def test_partially_unmapped_source_change_falls_back_to_all_tests(tmp_path: Path) -> None:
    """When one changed source maps to a test but another does not, run everything.

    Old behavior only fell back when nothing mapped; a partially-unmapped set
    would run just the mapped subset and skip the tests guarding the rest.
    """
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "mapped.py", "def mapped() -> int:\n    return 1\n")
    _write(tmp_path / "src" / "demo" / "orphan.py", "def orphan() -> int:\n    return 2\n")
    _write(tmp_path / "tests" / "unit" / "test_mapped.py", "def test_mapped() -> None:\n    assert True\n")
    _write(tmp_path / "tests" / "unit" / "test_other.py", "def test_other() -> None:\n    assert True\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests" / "unit"])
    analysis = analyzer.analyze(["src/demo/mapped.py", "src/demo/orphan.py"])

    assert analysis.fallback_used is True
    assert analysis.affected_tests == [
        "tests/unit/test_mapped.py",
        "tests/unit/test_other.py",
    ]


# ---------------------------------------------------------------------------
# get_dependent_source_files
# ---------------------------------------------------------------------------


def test_get_dependent_source_files_includes_direct_importer(tmp_path: Path) -> None:
    """A signature change in models.py should also type-check service.py (its importer)."""
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "models.py", "class Model:\n    name: str\n")
    _write(
        tmp_path / "src" / "demo" / "service.py",
        "from demo.models import Model\n\ndef use() -> Model:\n    return Model()\n",
    )

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests"])
    result = analyzer.get_dependent_source_files(["src/demo/models.py"])

    assert "src/demo/models.py" in result
    assert "src/demo/service.py" in result


def test_get_dependent_source_files_includes_transitive_importer(tmp_path: Path) -> None:
    """Transitive importers are also included: models → service → api → all checked."""
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "models.py", "class Model: pass\n")
    _write(
        tmp_path / "src" / "demo" / "service.py",
        "from demo.models import Model\n\ndef svc() -> Model:\n    return Model()\n",
    )
    _write(
        tmp_path / "src" / "demo" / "api.py",
        "from demo.service import svc\n\ndef endpoint() -> None:\n    svc()\n",
    )

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests"])
    result = analyzer.get_dependent_source_files(["src/demo/models.py"])

    assert "src/demo/models.py" in result
    assert "src/demo/service.py" in result
    assert "src/demo/api.py" in result


def test_get_dependent_source_files_leaf_module_returns_itself(tmp_path: Path) -> None:
    """A module with no importers returns only itself."""
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "utils.py", "def helper() -> int:\n    return 1\n")
    _write(tmp_path / "src" / "demo" / "other.py", "def thing() -> int:\n    return 2\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests"])
    result = analyzer.get_dependent_source_files(["src/demo/utils.py"])

    assert result == ["src/demo/utils.py"]


def test_get_dependent_source_files_non_source_files_pass_through(tmp_path: Path) -> None:
    """Non-Python files are returned unchanged; non-src files are not expanded."""
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "models.py", "class Model: pass\n")

    analyzer = ImpactAnalyzer(tmp_path, test_dirs=[tmp_path / "tests"])
    result = analyzer.get_dependent_source_files(["README.md", "pyproject.toml"])

    # Non-python files are passed through without modification
    assert "README.md" in result
    assert "pyproject.toml" in result
