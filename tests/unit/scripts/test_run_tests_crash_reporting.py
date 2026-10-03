"""A test file whose process dies must be reported by its exit status.

``scripts/run_tests.py`` runs every file uncaptured (``-s``), so the tail of a
file's output is whatever test happened to print last. For a failed assertion
that does not matter -- the FAILURES section and the short test summary are
there to quote. When the process *dies* instead, pytest prints neither, and the
old fallback dumped thirty lines of unrelated debug output with no exit code
anywhere, leaving a red shard with nothing to read (issue #6039).
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_tests.py"

# The shape from the issue: a file that printed a lot as it ran, then died.
NOISY_STDOUT = "\n".join(f"[SPAWNER-DEBUG] worker {i} reaped pid {1000 + i}" for i in range(40))
CRASH_STDERR = "Fatal Python error: Segmentation fault\nCurrent thread 0x00007f (most recent call first):"

ASSERTION_FAILURE_OUTPUT = """\
=================================== FAILURES ===================================
_________________________________ test_router __________________________________
E   AssertionError: assert 1 == 2
=========================== short test summary info ============================
FAILED tests/unit/test_router.py::test_router - AssertionError: assert 1 == 2
1 failed in 0.30s
"""


@pytest.fixture
def run_tests_module() -> Generator[ModuleType, None, None]:
    """Load scripts/run_tests.py as an importable module."""
    spec = importlib.util.spec_from_file_location("run_tests_crash_reporting", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


# --- classification --------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "output"),
    [
        (-11, NOISY_STDOUT),
        (1, ""),
        (134, "Bernstein starting\nOrchestrator ready\n"),
        (2, "ERROR: usage error"),
    ],
)
def test_non_zero_exit_without_a_summary_is_a_crash(run_tests_module: ModuleType, code: int, output: str) -> None:
    """A process that died before pytest could report is its own outcome."""
    assert run_tests_module.classify_file_outcome(code, output) == run_tests_module.OUTCOME_CRASHED


@pytest.mark.parametrize(
    ("code", "output"),
    [
        (1, "1 failed, 2 passed in 0.30s"),
        (1, ASSERTION_FAILURE_OUTPUT),
    ],
)
def test_a_reported_failure_is_still_a_failure(run_tests_module: ModuleType, code: int, output: str) -> None:
    """pytest reaching its terminal summary means the file failed, not crashed."""
    assert run_tests_module.classify_file_outcome(code, output) == run_tests_module.OUTCOME_FAILED


# --- exit status rendering -------------------------------------------------


def test_format_exit_status_describes_both_ways_a_process_can_end(run_tests_module: ModuleType) -> None:
    """A chosen exit code is quoted; a negative returncode is named as its signal."""
    assert run_tests_module._format_exit_status(134) == "exit code 134"
    status = run_tests_module._format_exit_status(-11)
    assert "signal 11" in status
    assert "SIGSEGV" in status


# --- reporting -------------------------------------------------------------


def test_crash_report_names_the_exit_status_instead_of_trailing_stdout(
    run_tests_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reason the shard is red is in the report, not thirty unrelated lines."""
    outcome = run_tests_module._report_file_result(
        "[24/41] tests/unit/test_orchestrator.py",
        -11,
        53.0,
        NOISY_STDOUT + "\n" + CRASH_STDERR,
        CRASH_STDERR,
    )

    captured = capsys.readouterr().out
    assert outcome == run_tests_module.OUTCOME_CRASHED
    assert "CRASH" in captured
    assert "signal 11" in captured
    assert "SIGSEGV" in captured
    assert "Fatal Python error: Segmentation fault" in captured
    assert "[SPAWNER-DEBUG]" not in captured


def test_crash_report_says_so_when_there_was_no_stderr(
    run_tests_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """A process killed outright writes nothing; the exit status is then the whole story."""
    run_tests_module._report_file_result("[1/1] tests/unit/test_x.py", -9, 12.0, NOISY_STDOUT, "")

    captured = capsys.readouterr().out
    assert "signal 9" in captured
    assert "stderr" in captured
    assert "[SPAWNER-DEBUG]" not in captured


def test_an_assertion_failure_is_reported_exactly_as_before(
    run_tests_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """The FAILURES section and short summary still come through unchanged."""
    outcome = run_tests_module._report_file_result(
        "[1/1] tests/unit/test_router.py", 1, 0.3, ASSERTION_FAILURE_OUTPUT, ""
    )

    captured = capsys.readouterr().out
    assert outcome == run_tests_module.OUTCOME_FAILED
    assert "FAIL [1/1] tests/unit/test_router.py (0.3s)" in captured
    assert "CRASH" not in captured
    assert "AssertionError: assert 1 == 2" in captured
    assert "short test summary info" in captured


def test_print_failure_summary_keeps_its_tail_fallback_without_an_exit_status(
    run_tests_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Callers that report no crash still get the old trailing-lines fallback."""
    run_tests_module._print_failure_summary("first line\nlast line")

    assert "last line" in capsys.readouterr().out


# --- totals ----------------------------------------------------------------


def test_a_crashed_file_still_fails_the_run(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Naming the crash separately must not make it any less fatal."""
    files = [Path("tests/unit/test_a.py"), Path("tests/unit/test_orchestrator.py")]

    def fake_run_file(path: Path, *_: object, **__: object) -> tuple[Path, int, float, str, str]:
        if path.name == "test_a.py":
            return path, 0, 0.1, "4 passed in 0.10s", ""
        return path, -11, 53.0, NOISY_STDOUT + "\n" + CRASH_STDERR, CRASH_STDERR

    monkeypatch.setattr(run_tests_module, "run_file", fake_run_file)

    code = run_tests_module.run_sequential(files, [], fail_fast=False)

    captured = capsys.readouterr().out
    assert code == 1
    assert "Files: 1 passed, 1 failed, 0 ran no tests, 2 total" in captured
    assert "CRASH [2/2] tests/unit/test_orchestrator.py" in captured
    assert "Fatal Python error: Segmentation fault" in captured
