"""Tests for ``scripts/check_test_count_drop.py`` (#4873)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_test_count_drop.py"


@pytest.fixture(scope="module")
def check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_test_count_drop_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "checkout", "-b", "main")


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def test_parse_overrides(check_module: ModuleType) -> None:
    body = """
## Summary
test-count-drop: tests/unit/test_foo.py -3
test-count-drop: tests/unit/test_bar.py -1
"""
    assert check_module.parse_overrides(body) == {
        "tests/unit/test_foo.py": 3,
        "tests/unit/test_bar.py": 1,
    }


def test_not_run_without_base_is_not_ok(check_module: ModuleType, tmp_path: Path) -> None:
    """An unrun guard must not share the OK outcome word."""
    report = check_module.build_report(tmp_path, base=None)
    assert report.not_run is not None
    text = check_module.format_report(report)
    assert text.startswith("NOT_RUN:")
    assert not text.startswith("OK:")
    assert check_module.main(["--root", str(tmp_path)]) == 0


def test_drop_turns_red(check_module: ModuleType, tmp_path: Path) -> None:
    """Fixture module that loses cases turns the check red (failure path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    test_dir = repo / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_sample.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n\ndef test_c():\n    assert True\n",
        encoding="utf-8",
    )
    base = _commit_all(repo, "base with three tests")

    (test_dir / "test_sample.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n",
        encoding="utf-8",
    )
    _commit_all(repo, "drop one test")

    report = check_module.build_report(repo, base=base, python=sys.executable)
    assert report.drops == [("tests/unit/test_sample.py", 3, 2, "count_drop")]
    assert check_module.main(["--root", str(repo), "--base", base]) == 1
    assert "cause=count_drop" in check_module.format_report(report)


def test_parametrize_consolidation_stays_green(check_module: ModuleType, tmp_path: Path) -> None:
    """N cases folded into one parametrize table keep collected count stable."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    test_dir = repo / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_sample.py").write_text(
        "def test_a():\n    assert 1 == 1\n\ndef test_b():\n    assert 2 == 2\n\ndef test_c():\n    assert 3 == 3\n",
        encoding="utf-8",
    )
    base = _commit_all(repo, "three separate tests")

    (test_dir / "test_sample.py").write_text(
        "import pytest\n\n@pytest.mark.parametrize('n', [1, 2, 3])\ndef test_n(n):\n    assert n == n\n",
        encoding="utf-8",
    )
    _commit_all(repo, "consolidate to parametrize")

    report = check_module.build_report(repo, base=base, python=sys.executable)
    assert report.drops == []
    assert report.stale_overrides == []
    assert check_module.format_report(report).startswith("OK:")
    assert check_module.main(["--root", str(repo), "--base", base]) == 0


def test_import_error_named_in_drop_message(check_module: ModuleType, tmp_path: Path) -> None:
    """A module that stops importing is a drop with cause=import_error, not silent zero."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    test_dir = repo / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_sample.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n",
        encoding="utf-8",
    )
    base = _commit_all(repo, "two tests")

    (test_dir / "test_sample.py").write_text(
        "import definitely_not_a_real_module_4873  # noqa: F401\n\ndef test_a():\n    assert True\n",
        encoding="utf-8",
    )
    _commit_all(repo, "break import")

    report = check_module.build_report(repo, base=base, python=sys.executable)
    assert len(report.drops) == 1
    rel, base_n, head_n, cause = report.drops[0]
    assert rel == "tests/unit/test_sample.py"
    assert base_n == 2
    assert head_n == 0
    assert cause == "import_error"
    assert "cause=import_error" in check_module.format_report(report)


def test_override_excuses_drop_and_stale_fails(check_module: ModuleType, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    test_dir = repo / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_sample.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n",
        encoding="utf-8",
    )
    base = _commit_all(repo, "two tests")

    (test_dir / "test_sample.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    _commit_all(repo, "drop one")

    body = "test-count-drop: tests/unit/test_sample.py -1\n"
    report = check_module.build_report(repo, base=base, pr_body=body, python=sys.executable)
    assert report.drops == []
    assert report.excused == [("tests/unit/test_sample.py", 2, 1, 1)]

    stale_body = body + "test-count-drop: tests/unit/test_other.py -2\n"
    stale = check_module.build_report(repo, base=base, pr_body=stale_body, python=sys.executable)
    assert stale.stale_overrides
    assert check_module.main(["--root", str(repo), "--base", base, "--pr-body", stale_body]) == 1


def test_deleted_test_with_subject_is_carved_out(check_module: ModuleType, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    src = repo / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "widget.py").write_text("X = 1\n", encoding="utf-8")
    test_dir = repo / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_widget.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    base = _commit_all(repo, "widget + test")

    (src / "widget.py").unlink()
    (test_dir / "test_widget.py").unlink()
    _commit_all(repo, "delete widget and its test")

    report = check_module.build_report(repo, base=base, python=sys.executable)
    assert report.carved == ["tests/unit/test_widget.py"]
    assert report.drops == []


def test_deleted_test_without_subject_fails(check_module: ModuleType, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    src = repo / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "widget.py").write_text("X = 1\n", encoding="utf-8")
    test_dir = repo / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_widget.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    base = _commit_all(repo, "widget + test")

    (test_dir / "test_widget.py").unlink()
    _commit_all(repo, "delete test only")

    report = check_module.build_report(repo, base=base, python=sys.executable)
    assert report.drops == [("tests/unit/test_widget.py", 1, 0, "missing")]


def test_guard_discriminates_when_a_drop_exists(check_module: ModuleType, tmp_path: Path) -> None:
    """Counterfactual: with a real drop the report is non-empty (not vacuously OK)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    test_dir = repo / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_sample.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n",
        encoding="utf-8",
    )
    base = _commit_all(repo, "two")
    (test_dir / "test_sample.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    _commit_all(repo, "one")
    report = check_module.build_report(repo, base=base, python=sys.executable)
    assert report.drops, "a real drop must surface; otherwise the guard is vacuous"


def test_sibling_helper_import_does_not_false_positive(check_module: ModuleType, tmp_path: Path) -> None:
    """A touched module that imports a sibling tests/ helper must still collect (#5575, #5565).

    ``collect_pair`` materialises only the touched file into an isolated tempdir and
    collects it from there. Real repos route shared test setup through helper modules
    imported by repo-relative dotted path (``from tests.unit._helper import ...`), the
    same way ``tests/unit/_adapter_test_helpers.py`` is imported by ~20 files today. The
    isolated tempdir has no ``tests`` package on its import path, so that import fails
    and the guard misreads a stable refactor as every case in the file disappearing.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    unit_dir = repo / "tests" / "unit"
    unit_dir.mkdir(parents=True)
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (unit_dir / "__init__.py").write_text("", encoding="utf-8")
    (unit_dir / "test_uses_helper.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n\ndef test_c():\n    assert True\n",
        encoding="utf-8",
    )
    base = _commit_all(repo, "three inline tests, no shared helper yet")

    # Refactor to share case data via a sibling helper module - collected count
    # stays 3, but only if the import resolves.
    (unit_dir / "_helper.py").write_text("CASES = [1, 2, 3]\n", encoding="utf-8")
    (unit_dir / "test_uses_helper.py").write_text(
        "import pytest\n\n"
        "from tests.unit._helper import CASES\n\n\n"
        "@pytest.mark.parametrize('n', CASES)\n"
        "def test_uses_shared_case(n):\n"
        "    assert n in CASES\n",
        encoding="utf-8",
    )
    _commit_all(repo, "share case data through tests.unit._helper")

    report = check_module.build_report(repo, base=base, python=sys.executable)
    assert report.drops == [], check_module.format_report(report)
    assert report.checked == 1
    assert check_module.format_report(report).startswith("OK:")
    assert check_module.main(["--root", str(repo), "--base", base]) == 0


def test_override_in_a_commit_message_is_read_when_the_pr_body_is_empty(
    check_module: ModuleType, tmp_path: Path
) -> None:
    """The merge-queue shape: no PR body exists, so the commits must carry the override.

    A ``merge_group`` build has no ``pull_request`` payload; ``PR_BODY`` is
    empty. Before the commit-message channel existed, a body-only override
    passed the PR lane and then failed the queue build, taking every entry
    stacked behind it with it. The counterfactual at the end of this test is
    that pre-fix behaviour.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    test_dir = repo / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_sample.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n",
        encoding="utf-8",
    )
    base = _commit_all(repo, "two tests")

    (test_dir / "test_sample.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    _commit_all(
        repo,
        "drop one case\n\ntest-count-drop: tests/unit/test_sample.py -1\n",
    )

    report = check_module.build_report(repo, base=base, pr_body="", python=sys.executable)
    assert report.drops == []
    assert report.excused == [("tests/unit/test_sample.py", 2, 1, 1)]
    assert check_module.main(["--root", str(repo), "--base", base]) == 0

    # Counterfactual: with the commit-message channel removed — the pre-fix
    # script, which read the PR body alone — the same commit turns the guard red.
    original = check_module.commit_message_text
    check_module.commit_message_text = lambda *a, **k: ""  # type: ignore[assignment]
    try:
        pre_fix = check_module.build_report(repo, base=base, pr_body="", python=sys.executable)
    finally:
        check_module.commit_message_text = original  # type: ignore[assignment]
    assert pre_fix.drops == [("tests/unit/test_sample.py", 2, 1, "count_drop")], (
        "the counterfactual must fail; otherwise this test proves nothing about the new channel"
    )


def test_a_stale_override_in_a_commit_message_still_fails(check_module: ModuleType, tmp_path: Path) -> None:
    """The new channel is a second door to the same room, not a free pass."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    test_dir = repo / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_sample.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    base = _commit_all(repo, "one test")

    (test_dir / "test_sample.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n",
        encoding="utf-8",
    )
    _commit_all(repo, "add a case\n\ntest-count-drop: tests/unit/test_sample.py -2\n")

    report = check_module.build_report(repo, base=base, pr_body="", python=sys.executable)
    assert report.stale_overrides == ["tests/unit/test_sample.py -2"]
    assert check_module.main(["--root", str(repo), "--base", base]) == 1


def test_commit_messages_outside_the_compared_range_are_not_read(check_module: ModuleType, tmp_path: Path) -> None:
    """An override spent on an earlier merge must not excuse a later drop.

    ``base..head`` is the range the guard compares; reading the whole history
    would let one declaration, written once, silently cover every future drop
    in that module.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    test_dir = repo / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_sample.py").write_text(
        "def test_a():\n    assert True\n\ndef test_b():\n    assert True\n",
        encoding="utf-8",
    )
    _commit_all(repo, "two tests\n\ntest-count-drop: tests/unit/test_sample.py -1\n")
    base = _git(repo, "rev-parse", "HEAD").strip()

    (test_dir / "test_sample.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    _commit_all(repo, "drop one, declaring nothing")

    report = check_module.build_report(repo, base=base, pr_body="", python=sys.executable)
    assert report.drops == [("tests/unit/test_sample.py", 2, 1, "count_drop")]
