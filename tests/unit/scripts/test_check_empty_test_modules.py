"""Guard: in-scope test modules under ``tests/`` must collect at least one item.

``scripts/check_empty_test_modules.py`` fails when a ``test_*.py`` / ``*_test.py``
module collects zero items (issue #4834). These tests run that guard against
fixture modules — an emptied file must be red; a file with a real test must
be green — so the failure path is executed, not merely agreed with.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_empty_test_modules.py"


@pytest.fixture(scope="module")
def check_module() -> ModuleType:
    """Load ``scripts/check_empty_test_modules.py`` as an importable module."""
    spec = importlib.util.spec_from_file_location("check_empty_test_modules_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


def test_emptied_fixture_module_is_reported_empty(check_module: ModuleType, tmp_path: Path) -> None:
    """A test module whose body was gutted must fail the guard."""
    root = tmp_path / "repo"
    tests = root / "tests" / "unit"
    emptied = _write(
        tests / "test_emptied_by_cleanup.py",
        '''
        """Formerly held tests; cleanup left the module collect-empty."""

        from __future__ import annotations
        ''',
    )
    report = check_module.build_report(root=root, tests_dir=tests)
    rel = emptied.relative_to(root).as_posix()
    assert rel in report.empty
    assert report.counts[rel] == 0
    assert "0 collected" in check_module.format_report(report)


def test_fixture_module_with_a_test_is_green(check_module: ModuleType, tmp_path: Path) -> None:
    """A module that still defines a collected test must pass."""
    root = tmp_path / "repo"
    tests = root / "tests" / "unit"
    _write(
        tests / "test_still_has_coverage.py",
        """
        def test_answer() -> None:
            assert 1 + 1 == 2
        """,
    )
    report = check_module.build_report(root=root, tests_dir=tests)
    assert report.empty == []
    assert report.counts["tests/unit/test_still_has_coverage.py"] >= 1


def test_conftest_and_helpers_are_out_of_scope(check_module: ModuleType, tmp_path: Path) -> None:
    """Naming convention excludes legitimately test-free modules."""
    root = tmp_path / "repo"
    tests = root / "tests" / "unit"
    _write(tests / "conftest.py", "import pytest\n")
    _write(tests / "__init__.py", "")
    _write(tests / "helpers.py", "def make_client():\n    return object()\n")
    _write(tests / "support_utils.py", "VALUE = 1\n")
    report = check_module.build_report(root=root, tests_dir=tests)
    assert report.checked == 0
    assert report.empty == []


def test_allowlist_excuses_an_empty_module(check_module: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    tests = root / "tests" / "unit"
    _write(tests / "test_intentional_placeholder.py", "from __future__ import annotations\n")
    report = check_module.build_report(
        root=root,
        tests_dir=tests,
        allowlist={
            "tests/unit/test_intentional_placeholder.py": "x" * 40,
        },
    )
    assert report.empty == []
    assert report.stale_allowlist == []


def test_stale_allowlist_entry_is_reported(check_module: ModuleType, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    tests = root / "tests" / "unit"
    _write(
        tests / "test_not_empty.py",
        "def test_ok() -> None:\n    assert True\n",
    )
    report = check_module.build_report(
        root=root,
        tests_dir=tests,
        allowlist={"tests/unit/test_not_empty.py": "x" * 40},
    )
    assert "tests/unit/test_not_empty.py" in report.stale_allowlist


def test_main_exits_one_when_empty_modules_exist(
    check_module: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "repo"
    tests = root / "tests" / "unit"
    _write(tests / "test_gutted.py", "# empty\n")
    monkeypatch.setattr(check_module, "REPO_ROOT", root)
    assert check_module.main(["--root", str(root), "--tests-dir", str(tests)]) == 1
    out = capsys.readouterr().out
    assert "tests/unit/test_gutted.py: 0 collected" in out


def test_repository_has_no_unexcused_empty_test_modules(check_module: ModuleType) -> None:
    """Live suite: every in-scope module collects ≥1, or is allowlisted."""
    report = check_module.build_report()
    assert report.stale_allowlist == [], (
        "Stale ALLOWLIST entries in scripts/check_empty_test_modules.py:\n  " + "\n  ".join(report.stale_allowlist)
    )
    assert report.empty == [], (
        "These test modules collect zero items. Restore tests, rename the file "
        "out of pytest's python_files patterns, or add a reasoned ALLOWLIST "
        "entry:\n  " + "\n  ".join(f"{rel}: 0 collected" for rel in report.empty)
    )
