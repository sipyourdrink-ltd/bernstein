"""Collection-completeness guard for the test tree.

A test file that no CI configuration ever hands to pytest produces the same
signal as a passing one: the suite is green because the file was never opened.
``scripts/check_test_collection.py`` derives the collected set from the
workflow definitions and the shard runner's own constants; this module runs
that derivation against the repository and pins the parser behaviour it
depends on.

The repository-level assertions are the guard proper:

- every test file under ``tests/`` is either collected by some workflow or
  carries an allowlist entry explaining why it is not;
- no allowlist entry outlives the file it excuses;
- with the allowlist emptied the report is non-empty, so a parser that
  silently started collecting everything cannot pass this file.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_test_collection.py"


@pytest.fixture(scope="module")
def check_module() -> ModuleType:
    """Load ``scripts/check_test_collection.py`` as an importable module."""
    spec = importlib.util.spec_from_file_location("check_test_collection_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def report(check_module: ModuleType) -> object:
    """Build the collection report once for the repository-level assertions."""
    return check_module.build_report()


# ---------------------------------------------------------------------------
# Repository-level guard
# ---------------------------------------------------------------------------


def test_every_test_file_is_collected_or_allowlisted(report: object) -> None:
    """No test file may sit outside both the collected set and the allowlist."""
    uncollected = report.uncollected  # type: ignore[attr-defined]
    assert uncollected == [], (
        "These test files are never collected by any CI configuration, so they "
        "cannot fail a build. Move each one under a collected directory "
        "(tests/unit/ or tests/integration/), name it in a workflow, or add an "
        "entry with a reason to ALLOWLIST in scripts/check_test_collection.py:\n  " + "\n  ".join(uncollected)
    )


def test_allowlist_entries_are_all_live(report: object) -> None:
    """An allowlist entry that matches no file must be removed."""
    stale = report.stale_allowlist  # type: ignore[attr-defined]
    assert stale == [], (
        "These ALLOWLIST entries in scripts/check_test_collection.py no longer "
        "match an uncollected test file and must be deleted:\n  " + "\n  ".join(stale)
    )


def test_allowlist_entries_carry_a_reason(check_module: ModuleType) -> None:
    """Every excused path states why it is excused."""
    for key, reason in check_module.ALLOWLIST.items():
        assert reason.strip(), f"ALLOWLIST entry {key!r} has no reason"
        assert len(reason.strip()) >= 30, f"ALLOWLIST entry {key!r} needs a reason a reader can act on"


def test_guard_fails_when_the_allowlist_is_empty(check_module: ModuleType) -> None:
    """The derivation must discriminate: without excuses, gaps are reported."""
    unexcused = check_module.build_report(allowlist={})
    assert unexcused.uncollected, "with an empty allowlist the report must list the uncollected files"
    gaps = set(unexcused.uncollected)
    for key in check_module.ALLOWLIST:
        matched = any(path.startswith(key) for path in gaps) if key.endswith("/") else key in gaps
        assert matched, f"allowlist entry {key!r} should surface as uncollected once the allowlist is dropped"


def test_stale_entries_are_detected(check_module: ModuleType) -> None:
    """An entry pointing at a collected file is reported as stale."""
    fabricated = {"tests/unit/test_does_not_exist_anywhere.py": "x" * 40}
    stale_report = check_module.build_report(allowlist=fabricated)
    assert "tests/unit/test_does_not_exist_anywhere.py" in stale_report.stale_allowlist


def test_collected_set_discriminates(report: object, check_module: ModuleType) -> None:
    """The shard directory is collected; an excused suite is not."""
    collected = report.collected  # type: ignore[attr-defined]
    assert "tests/unit/scripts/test_check_test_collection.py" in collected, (
        "this guard must itself be collected by the shards"
    )
    assert not any(path.startswith("tests/chaos/") for path in collected)
    assert check_module.default_test_dir() == "tests/unit"


def test_shard_directory_comes_from_the_runner_constant(check_module: ModuleType) -> None:
    """The default shard directory is read from the runner, never duplicated."""
    run_tests = check_module._import_script("run_tests")
    assert check_module.default_test_dir() == run_tests.DEFAULT_TEST_DIR


def test_affected_universe_comes_from_the_impact_analyser(check_module: ModuleType) -> None:
    """The affected-run universe is read from the impact analyser's own list."""
    dirs = check_module.affected_test_dirs()
    assert "tests/unit" in dirs
    assert "tests/integration" in dirs


# ---------------------------------------------------------------------------
# Parser behaviour
# ---------------------------------------------------------------------------


def test_run_tests_without_flags_collects_the_default_directory(check_module: ModuleType) -> None:
    """A bare shard invocation collects the runner's default directory."""
    invocation = check_module.parse_command("ci.yml", "uv run python scripts/run_tests.py --parallel 4 --shard 1/4")
    assert invocation is not None
    assert invocation.kind == "run_tests"
    assert invocation.paths == ("tests/unit",)
    assert invocation.patterns == ("test_*.py",)


def test_run_tests_honours_an_explicit_test_dir(check_module: ModuleType) -> None:
    """``--test-dir`` overrides the default directory."""
    invocation = check_module.parse_command("ci.yml", "python scripts/run_tests.py --test-dir tests/integration")
    assert invocation is not None
    assert invocation.paths == ("tests/integration",)

    equals_form = check_module.parse_command("ci.yml", "python scripts/run_tests.py --test-dir=tests/property")
    assert equals_form is not None
    assert equals_form.paths == ("tests/property",)


def test_affected_run_adds_the_impact_universe(check_module: ModuleType) -> None:
    """``--affected`` can select any file the impact analyser knows about."""
    invocation = check_module.parse_command(
        "ci.yml", "uv run python scripts/run_tests.py --parallel 4 --affected refs/remotes/origin/main"
    )
    assert invocation is not None
    assert "tests/unit" in invocation.paths
    assert "tests/integration" in invocation.paths


def test_keyword_filtered_runs_are_not_credited(check_module: ModuleType) -> None:
    """A ``-k`` expression runs an unknown subset, so it credits nothing."""
    assert check_module.parse_command("publish.yml", "uv run python scripts/run_tests.py -k release -x") is None
    assert check_module.parse_command("ci.yml", "uv run pytest tests/unit -k adapter") is None


def test_marker_filtered_runs_are_credited(check_module: ModuleType) -> None:
    """A ``-m`` marker deselects after collection, so the directory counts."""
    invocation = check_module.parse_command("nightly.yml", "uv run pytest tests/stress -m stress --no-cov -q")
    assert invocation is not None
    assert invocation.paths == ("tests/stress",)


def test_pytest_paths_are_extracted(check_module: ModuleType) -> None:
    """Explicit pytest path arguments are collected with pytest's patterns."""
    invocation = check_module.parse_command(
        "pentest.yml", "uv run pytest tests/pentest/test_api_security.py -v --timeout=120"
    )
    assert invocation is not None
    assert invocation.kind == "pytest"
    assert invocation.paths == ("tests/pentest/test_api_security.py",)
    assert invocation.patterns == ("test_*.py", "*_test.py")


def test_non_test_commands_are_ignored(check_module: ModuleType) -> None:
    """Commands that run no tests contribute nothing."""
    assert check_module.parse_command("ci.yml", "uv run ruff check src tests") is None
    assert check_module.parse_command("ci.yml", 'echo "pytest_exit_code=$rc"') is None
    assert check_module.parse_command("ci.yml", "uv run pytest --version") is None


def test_line_continuations_are_joined(check_module: ModuleType) -> None:
    """POSIX and PowerShell continuations keep a command in one piece."""
    posix = check_module._split_commands("uv run pytest \\\n  tests/integration/test_airgap_wheelhouse.py \\\n  -x")
    assert len(posix) == 1
    invocation = check_module.parse_command("airgap.yml", posix[0])
    assert invocation is not None
    assert invocation.paths == ("tests/integration/test_airgap_wheelhouse.py",)

    pwsh = check_module._split_commands("uv run pytest `\n  tests/unit/test_worktree_isolation_windows.py `\n  -x -q")
    assert len(pwsh) == 1
    windows = check_module.parse_command("ci.yml", pwsh[0])
    assert windows is not None
    assert windows.paths == ("tests/unit/test_worktree_isolation_windows.py",)


def test_commands_are_split_on_shell_separators(check_module: ModuleType) -> None:
    """Chained commands are inspected individually."""
    parts = check_module._split_commands("set -euo pipefail\nuv run pytest tests/snapshot/ -q | tee out.log")
    assert any("tests/snapshot/" in part for part in parts)
    invocation = next(
        result for result in (check_module.parse_command("ci.yml", part) for part in parts) if result is not None
    )
    assert invocation.paths == ("tests/snapshot",)


def test_allowlist_matching_supports_files_and_directories(check_module: ModuleType) -> None:
    """A trailing slash excuses a whole suite; a plain path excuses one file."""
    allowlist = {"tests/chaos/": "y" * 40, "tests/test_server.py": "z" * 40}
    assert check_module._allowlist_reason("tests/chaos/test_disk_full.py", allowlist) == "y" * 40
    assert check_module._allowlist_reason("tests/test_server.py", allowlist) == "z" * 40
    assert check_module._allowlist_reason("tests/unit/test_anything.py", allowlist) is None
