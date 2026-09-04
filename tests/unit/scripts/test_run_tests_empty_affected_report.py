"""Issue #5111: an empty affected set says which commits it compared.

A green run and a run that tested nothing look identical in the GitHub Actions
UI -- both are a checkmark. The empty-selection message did not name the base or
head being compared, which makes "nothing was affected" unfalsifiable from the
outside: a reader cannot tell a correct no-op from a diff computed against the
wrong base, without opening the log and reconstructing the invocation.

These tests pin the sentence, the annotation that surfaces it on the run
summary, and the one comparison that must not be printed as a commit range.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_tests.py"


@pytest.fixture
def run_tests_module() -> Generator[ModuleType, None, None]:
    """Load scripts/run_tests.py as an importable module."""
    spec = importlib.util.spec_from_file_location("run_tests_empty_report_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def test_the_empty_affected_message_names_the_compared_commits(
    run_tests_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    run_tests_module._report_empty_selection(None, "affected ", base="origin/main")
    out = capsys.readouterr().out
    assert "No affected tests found - nothing to run" in out
    assert "compared origin/main" in out
    assert "..." in out, "the two ends of the comparison must both appear"


def test_a_working_tree_comparison_is_not_printed_as_a_commit_range(run_tests_module: ModuleType) -> None:
    """``--affected HEAD`` compares the working tree against HEAD, not two commits.

    Printing ``HEAD...HEAD`` would be a range that is empty by definition, and
    would read as a bug in the runner rather than as the local-diff mode it is.
    """
    described = run_tests_module._compared_range("HEAD")
    assert described.startswith("working tree vs HEAD")
    assert "..." not in described


def test_a_resolvable_rev_carries_its_short_sha(run_tests_module: ModuleType) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert run_tests_module._describe_rev("HEAD") == f"HEAD ({head})"


def test_an_unresolvable_rev_degrades_to_its_name(run_tests_module: ModuleType) -> None:
    """Best-effort: a missing ref costs the sha and nothing else."""
    assert run_tests_module._describe_rev("no-such-ref-5111") == "no-such-ref-5111"


def test_the_message_is_annotated_on_github_actions(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The annotation is what puts it on the run summary instead of inside a log."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    run_tests_module._report_empty_selection(None, "affected ", base="origin/main")
    out = capsys.readouterr().out
    assert "::notice title=Nothing to run::" in out
    assert "compared origin/main" in out.split("::notice title=Nothing to run::", 1)[1]


def test_no_annotation_off_github_actions(
    run_tests_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    run_tests_module._report_empty_selection(None, "affected ", base="origin/main")
    assert "::notice" not in capsys.readouterr().out


def test_an_empty_shard_still_says_it_is_an_empty_shard(
    run_tests_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """The legitimate no-op keeps its own wording; the range is added, not swapped in."""
    run_tests_module._report_empty_selection((7, 8), "affected ", base="origin/main")
    out = capsys.readouterr().out
    assert "shard 7/8" in out
    assert "empty shard" in out
    assert "compared origin/main" in out


def test_a_full_discovery_run_reports_no_range(
    run_tests_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``--affected`` nothing was compared, so nothing is claimed."""
    run_tests_module._report_empty_selection(None, "")
    out = capsys.readouterr().out
    assert "No test files found - nothing to run" in out
    assert "compared" not in out
