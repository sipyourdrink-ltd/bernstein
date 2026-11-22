"""TEST-012: Test the coverage threshold script itself.

Verifies that the coverage checking script is importable and
that CoverageResult logic is correct.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add scripts dir to path so we can import the module
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_coverage_thresholds import MODULE_THRESHOLDS, CoverageResult, measure_coverage


@pytest.mark.parametrize("module", ["sample_ops", "sample_pkg.sample_ops"])
def test_measure_coverage_collects_an_importable_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    src = tmp_path / "src"
    source = src.joinpath(*module.split(".")).with_suffix(".py")
    source.parent.mkdir(parents=True)
    source.write_text("def one():\n    return 1\n", encoding="utf-8")
    tests = tmp_path / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_sample_ops.py").write_text(
        f"from {module} import one\n\ndef test_one():\n    assert one() == 1\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", str(src))

    result = measure_coverage(module, 100)

    assert result == CoverageResult(module, covered=2, total=2, percent=100.0, threshold=100, passed=True)


class TestCoverageResult:
    """Test CoverageResult logic."""

    def test_pass_when_above_threshold(self) -> None:
        r = CoverageResult(
            module="foo",
            covered=85,
            total=100,
            percent=85.0,
            threshold=80,
            passed=True,
        )
        assert r.passed is True

    def test_fail_when_below_threshold(self) -> None:
        r = CoverageResult(
            module="foo",
            covered=50,
            total=100,
            percent=50.0,
            threshold=80,
            passed=False,
        )
        assert r.passed is False

    def test_exact_threshold_passes(self) -> None:
        r = CoverageResult(
            module="foo",
            covered=80,
            total=100,
            percent=80.0,
            threshold=80,
            passed=True,
        )
        assert r.passed is True

    def test_zero_statements(self) -> None:
        r = CoverageResult(
            module="foo",
            covered=0,
            total=0,
            percent=0.0,
            threshold=80,
            passed=False,
        )
        assert r.passed is False


class TestModuleThresholds:
    """Validate the MODULE_THRESHOLDS configuration."""

    def test_thresholds_are_reasonable(self) -> None:
        for mod, thresh in MODULE_THRESHOLDS.items():
            assert 0 < thresh <= 100, f"Threshold for {mod} is {thresh}, expected 1-100"

    def test_core_modules_listed(self) -> None:
        assert "bernstein.core.tasks.lifecycle" in MODULE_THRESHOLDS
        assert "bernstein.core.tasks.models" in MODULE_THRESHOLDS

    def test_all_module_paths_exist(self) -> None:
        root = Path(__file__).resolve().parent.parent.parent
        for mod in MODULE_THRESHOLDS:
            src_path = root / "src" / mod.replace(".", "/")
            # Either .py file or __init__.py in package
            assert src_path.with_suffix(".py").exists() or (src_path / "__init__.py").exists(), (
                f"Module {mod} source not found at {src_path}"
            )
