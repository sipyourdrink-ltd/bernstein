"""Unit tests for the shared test-impact analyzer."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.test_impact import TestImpactAnalyzer as ImpactAnalyzer


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
    assert analysis.coverage_pct == 100.0
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
    assert analysis.coverage_pct == 100.0
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
    assert analysis.coverage_pct == 100.0
    assert analysis.affected_tests == ["tests/unit/test_core.py"]
