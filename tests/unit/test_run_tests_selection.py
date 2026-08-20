"""Unit tests for scripts/run_tests.py - positional target selection."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Make scripts/ importable
_SCRIPTS = Path(__file__).parent.parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from run_tests import dedupe_paths, split_test_targets

_RUNNER = _SCRIPTS / "run_tests.py"


def _write_suite(root: Path) -> tuple[Path, Path]:
    """Write two trivially passing test files and return their paths."""
    first = root / "test_first.py"
    second = root / "test_second.py"
    first.write_text("def test_first() -> None:\n    assert True\n", encoding="utf-8")
    second.write_text("def test_second() -> None:\n    assert True\n", encoding="utf-8")
    return first, second


class TestSplitTestTargets:
    def test_existing_file_is_a_target(self, tmp_path: Path) -> None:
        path = tmp_path / "test_one.py"
        path.touch()

        targets, passthrough, missing = split_test_targets([str(path)])

        assert targets == [path]
        assert passthrough == []
        assert missing == []

    def test_directory_expands_to_its_test_files(self, tmp_path: Path) -> None:
        first, second = _write_suite(tmp_path)
        (tmp_path / "helper.py").touch()

        targets, _passthrough, missing = split_test_targets([str(tmp_path)])

        assert targets == [first, second]
        assert missing == []

    def test_node_id_keeps_its_suffix(self, tmp_path: Path) -> None:
        path = tmp_path / "test_one.py"
        path.touch()
        entry = f"{path}::test_case"

        targets, _passthrough, missing = split_test_targets([entry])

        assert [str(t) for t in targets] == [entry]
        assert missing == []

    def test_pytest_arguments_pass_through(self, tmp_path: Path) -> None:
        path = tmp_path / "test_one.py"
        path.touch()

        targets, passthrough, missing = split_test_targets(["-p", "no:cacheprovider", str(path)])

        assert targets == [path]
        assert passthrough == ["-p", "no:cacheprovider"]
        assert missing == []

    def test_mistyped_path_is_reported_not_forwarded(self) -> None:
        targets, passthrough, missing = split_test_targets(["tests/unit/test_absent.py"])

        assert targets == []
        assert passthrough == []
        assert missing == ["tests/unit/test_absent.py"]


class TestDedupePaths:
    def test_preserves_first_seen_order(self) -> None:
        a, b = Path("a.py"), Path("b.py")

        assert dedupe_paths([b, a, b]) == [b, a]


class TestPositionalPathRunsOnlyThatFile:
    def test_single_file_argument_runs_that_file_alone(self, tmp_path: Path) -> None:
        """A positional test path selects that file instead of the whole suite.

        The regression this pins: the path used to be appended to pytest's
        argument list for every discovered file, so an intended one-file run
        silently became a full-suite run.
        """
        first, _second = _write_suite(tmp_path)

        result = subprocess.run(
            [sys.executable, str(_RUNNER), str(first), "--test-dir", str(tmp_path), "--parallel", "1"],
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 test files" in result.stdout
        assert "test_first.py" in result.stdout
        assert "test_second.py" not in result.stdout
        assert "1 passed, 0 failed, 0 ran no tests, 1 total" in result.stdout

    def test_bare_invocation_still_discovers_the_directory(self, tmp_path: Path) -> None:
        _write_suite(tmp_path)

        result = subprocess.run(
            [sys.executable, str(_RUNNER), "--test-dir", str(tmp_path), "--parallel", "1"],
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed, 0 failed, 0 ran no tests, 2 total" in result.stdout

    def test_mistyped_path_fails_instead_of_running_everything(self, tmp_path: Path) -> None:
        _write_suite(tmp_path)

        result = subprocess.run(
            [sys.executable, str(_RUNNER), str(tmp_path / "test_absent.py"), "--test-dir", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert result.returncode == 2
        assert "Test path not found" in result.stdout
        assert "passed" not in result.stdout
