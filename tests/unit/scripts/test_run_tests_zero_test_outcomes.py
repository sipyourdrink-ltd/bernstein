"""A file that ran no tests must not be reported as a passing file.

``scripts/run_tests.py`` is the shard runner behind the required ``CI gate``
context, and its ``Files: N passed`` line is what a reviewer reads as evidence
that a suite executed. Three distinct outcomes used to collapse into that one
number:

- a file whose tests all skipped at runtime (missing credential, absent
  optional dependency) exited 0 and was counted as passed;
- a file pytest collected nothing from (exit code 5) was counted as passed;
- a file whose pytest process was replaced mid-run (a test reaching
  ``os.execv``) exited 0 with no pytest summary at all and was counted as
  passed.

The first two are legitimate states that must stay non-fatal but must be
counted separately. The third is a file that stopped protecting anything
without saying so, and is a hard failure.

These tests pin the classification and the totals line.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_tests.py"


@pytest.fixture
def run_tests_module() -> Generator[ModuleType, None, None]:
    """Load scripts/run_tests.py as an importable module."""
    spec = importlib.util.spec_from_file_location("run_tests_zero_outcomes", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


# --- summarize_pytest_counts ----------------------------------------------


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("5 passed in 0.12s", {"passed": 5}),
        ("1 failed, 2 passed in 0.30s", {"failed": 1, "passed": 2}),
        ("13 skipped in 3.10s", {"skipped": 13}),
        ("5 passed, 1 warning in 0.12s", {"passed": 5}),
        ("2 xfailed, 1 xpassed in 0.20s", {"xfailed": 2, "xpassed": 1}),
        ("no tests ran in 0.01s", {}),
        ("3 passed, 2 deselected in 0.10s", {"passed": 3, "deselected": 2}),
    ],
)
def test_summarize_pytest_counts_reads_the_terminal_summary(
    run_tests_module: ModuleType, output: str, expected: dict[str, int]
) -> None:
    """Every pytest terminal-summary shape yields its per-outcome counts."""
    assert run_tests_module.summarize_pytest_counts(output) == expected


def test_summarize_pytest_counts_ignores_leading_progress_output(run_tests_module: ModuleType) -> None:
    """The summary is found even behind progress dots and captured stdout."""
    output = "some captured print\n.....                        [100%]\n5 passed in 0.12s\n"
    assert run_tests_module.summarize_pytest_counts(output) == {"passed": 5}


@pytest.mark.parametrize(
    "output",
    [
        "",
        "   \n\n",
        "Bernstein starting\nOrchestrator ready\n",
    ],
)
def test_summarize_pytest_counts_is_none_without_a_summary(run_tests_module: ModuleType, output: str) -> None:
    """Output that carries no pytest terminal summary is reported as absent.

    This is the ``os.execv`` shape: the pytest process was replaced, so the
    output belongs to some other program entirely.
    """
    assert run_tests_module.summarize_pytest_counts(output) is None


# --- classify_file_outcome -------------------------------------------------


@pytest.mark.parametrize(
    ("code", "output", "expected"),
    [
        (0, "5 passed in 0.12s", "passed"),
        (0, "3 passed, 2 skipped in 0.20s", "passed"),
        (0, "2 xfailed, 1 xpassed in 0.20s", "passed"),
        # Every test in the file skipped: the file executed nothing.
        (0, "13 skipped in 3.10s", "no-tests"),
        # pytest exit 5: nothing was collected.
        (5, "no tests ran in 0.01s", "no-tests"),
        (1, "1 failed, 2 passed in 0.30s", "failed"),
        (2, "ERROR: usage error", "failed"),
        # Process replaced mid-run: exit 0, no pytest summary anywhere.
        (0, "", "failed"),
        (0, "Bernstein starting\nOrchestrator ready\n", "failed"),
    ],
)
def test_classify_file_outcome(run_tests_module: ModuleType, code: int, output: str, expected: str) -> None:
    """A file's outcome separates passed, ran-nothing, and failed."""
    assert run_tests_module.classify_file_outcome(code, output) == expected


def test_outcome_constants_are_distinct(run_tests_module: ModuleType) -> None:
    """The three outcomes are three values, not two."""
    outcomes = {
        run_tests_module.OUTCOME_PASSED,
        run_tests_module.OUTCOME_NO_TESTS,
        run_tests_module.OUTCOME_FAILED,
    }
    assert len(outcomes) == 3


# --- reporting -------------------------------------------------------------


def test_report_file_result_labels_a_file_that_ran_nothing(
    run_tests_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """An all-skipped file is not printed as PASS."""
    outcome = run_tests_module._report_file_result("[1/1] test_x.py", 0, 3.1, "13 skipped in 3.10s")
    captured = capsys.readouterr().out
    assert outcome == run_tests_module.OUTCOME_NO_TESTS
    assert "PASS" not in captured
    assert "NO TESTS" in captured


def test_report_file_result_fails_a_replaced_process(
    run_tests_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 0 with no pytest summary is reported as a failure, not a pass."""
    outcome = run_tests_module._report_file_result("[1/1] test_x.py", 0, 0.4, "")
    captured = capsys.readouterr().out
    assert outcome == run_tests_module.OUTCOME_FAILED
    assert "FAIL" in captured
    assert "PASS" not in captured


def _stub_run_file(results: dict[str, tuple[int, str]]) -> Any:
    """Build a ``run_file`` replacement returning canned per-file results."""

    def _run_file(path: Path, extra_args: list[str], coverage: bool = False) -> tuple[Path, int, float, str]:
        code, output = results[path.name]
        return path, code, 0.1, output

    return _run_file


def test_sequential_totals_count_ran_nothing_separately(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The totals line reports passed, failed, and ran-nothing as three numbers."""
    files = [Path("tests/unit/test_a.py"), Path("tests/unit/test_b.py"), Path("tests/unit/test_c.py")]
    monkeypatch.setattr(
        run_tests_module,
        "run_file",
        _stub_run_file(
            {
                "test_a.py": (0, "4 passed in 0.10s"),
                "test_b.py": (0, "13 skipped in 3.10s"),
                "test_c.py": (5, "no tests ran in 0.01s"),
            }
        ),
    )

    code = run_tests_module.run_sequential(files, [], fail_fast=False)

    captured = capsys.readouterr().out
    assert "Files: 1 passed, 0 failed, 2 ran no tests, 3 total" in captured
    # Ran-nothing stays non-fatal: impact-based selection and credential-gated
    # suites legitimately execute nothing on some lanes.
    assert code == 0


def test_sequential_totals_name_the_files_that_ran_nothing(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each ran-nothing file is named, so the count can be checked against paths.

    The per-file lines only appear for failures, so a shard that reports
    "1 ran no tests" out of several hundred gives a reader no way to tell
    which file executed nothing -- or whether the file they care about was
    among the ones that ran at all.
    """
    files = [Path("tests/unit/test_a.py"), Path("tests/unit/test_b.py"), Path("tests/unit/test_c.py")]
    monkeypatch.setattr(
        run_tests_module,
        "run_file",
        _stub_run_file(
            {
                "test_a.py": (0, "4 passed in 0.10s"),
                "test_b.py": (0, "13 skipped in 3.10s"),
                "test_c.py": (5, "no tests ran in 0.01s"),
            }
        ),
    )

    run_tests_module.run_sequential(files, [], fail_fast=False)

    captured = capsys.readouterr().out
    assert "ran no tests: tests/unit/test_b.py" in captured
    assert "ran no tests: tests/unit/test_c.py" in captured
    assert "ran no tests: tests/unit/test_a.py" not in captured


def test_totals_name_the_files_in_a_deterministic_order(
    run_tests_module: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both runners report through this one function, so it is where the naming lives.

    The parallel runner completes files in whatever order the pool returns
    them; sorting here keeps two runs of the same shard byte-identical.
    """
    run_tests_module._print_totals(
        1,
        0,
        [Path("tests/unit/test_z.py"), Path("tests/unit/test_b.py")],
        3,
    )

    captured = capsys.readouterr().out
    assert "Files: 1 passed, 0 failed, 2 ran no tests, 3 total" in captured
    assert captured.index("test_b.py") < captured.index("test_z.py")


def test_sequential_totals_fail_on_a_replaced_process(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A file whose process vanished mid-run makes the run non-zero."""
    files = [Path("tests/unit/test_a.py"), Path("tests/unit/test_b.py")]
    monkeypatch.setattr(
        run_tests_module,
        "run_file",
        _stub_run_file(
            {
                "test_a.py": (0, "4 passed in 0.10s"),
                "test_b.py": (0, "Bernstein starting\n"),
            }
        ),
    )

    code = run_tests_module.run_sequential(files, [], fail_fast=False)

    captured = capsys.readouterr().out
    assert "Files: 1 passed, 1 failed, 0 ran no tests, 2 total" in captured
    assert code == 1
