"""Unit tests for per-file code health scoring."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.quality.code_health import (
    FileHealthScore,
    HealthDelta,
    check_health_delta,
    compute_file_health,
    get_unhealthiest_files,
)

# ---------------------------------------------------------------------------
# FileHealthScore dataclass
# ---------------------------------------------------------------------------


def test_file_health_score_is_frozen() -> None:
    score = FileHealthScore(
        file_path="src/foo.py",
        overall_score=0.85,
        complexity_score=0.9,
        bug_density_score=0.8,
        test_coverage_score=1.0,
        churn_score=0.7,
        coupling_score=0.9,
        last_updated="2026-01-01T00:00:00+00:00",
    )
    assert score.overall_score == pytest.approx(0.85)
    try:
        score.overall_score = 0.5  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")
    except AttributeError:
        pass


def test_health_delta_is_frozen() -> None:
    delta = HealthDelta(file_path="src/foo.py", before=0.8, after=0.6, delta=-0.2, degraded=True)
    assert delta.degraded is True
    try:
        delta.degraded = False  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# Complexity scoring
# ---------------------------------------------------------------------------


def test_complexity_low_for_simple_file(tmp_path: Path) -> None:
    src = tmp_path / "src" / "bernstein"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    target = src / "simple.py"
    target.write_text(
        textwrap.dedent("""\
        def add(a: int, b: int) -> int:
            return a + b
        """),
        encoding="utf-8",
    )

    _stub_git(tmp_path)
    score = compute_file_health("src/bernstein/simple.py", tmp_path)
    # Simple function → high complexity score (low complexity)
    assert score.complexity_score >= 0.9


def test_complexity_low_for_complex_file(tmp_path: Path) -> None:
    src = tmp_path / "src" / "bernstein"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    target = src / "complex.py"
    # Generate many branching statements
    lines = ["def f(x):\n"]
    for i in range(50):
        lines.append(f"    if x == {i}:\n        pass\n")
    target.write_text("".join(lines), encoding="utf-8")

    _stub_git(tmp_path)
    score = compute_file_health("src/bernstein/complex.py", tmp_path)
    # Many branches → low complexity score
    assert score.complexity_score < 0.5


# ---------------------------------------------------------------------------
# Bug density scoring
# ---------------------------------------------------------------------------


def test_bug_density_perfect_without_archive(tmp_path: Path) -> None:
    _make_source(tmp_path, "src/bernstein/clean.py", "x = 1\n")
    _stub_git(tmp_path)

    score = compute_file_health("src/bernstein/clean.py", tmp_path)
    assert score.bug_density_score == pytest.approx(1.0)


def test_bug_density_degrades_with_failures(tmp_path: Path) -> None:
    _make_source(tmp_path, "src/bernstein/buggy.py", "x = 1\n")
    archive = tmp_path / ".sdd" / "archive"
    archive.mkdir(parents=True)

    for i in range(5):
        (archive / f"task-{i}.json").write_text(
            json.dumps({"status": "failed", "files_changed": ["src/bernstein/buggy.py"]}),
            encoding="utf-8",
        )

    _stub_git(tmp_path)
    score = compute_file_health("src/bernstein/buggy.py", tmp_path)
    assert score.bug_density_score == pytest.approx(0.75)  # 1.0 - 5/20


# ---------------------------------------------------------------------------
# Test coverage scoring
# ---------------------------------------------------------------------------


def test_coverage_score_with_test_file(tmp_path: Path) -> None:
    _make_source(tmp_path, "src/bernstein/widget.py", "x = 1\n")
    test_dir = tmp_path / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_widget.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    _stub_git(tmp_path)
    score = compute_file_health("src/bernstein/widget.py", tmp_path)
    assert score.test_coverage_score == pytest.approx(1.0)


def test_coverage_score_without_test_file(tmp_path: Path) -> None:
    _make_source(tmp_path, "src/bernstein/orphan.py", "x = 1\n")

    _stub_git(tmp_path)
    score = compute_file_health("src/bernstein/orphan.py", tmp_path)
    assert score.test_coverage_score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Churn scoring
# ---------------------------------------------------------------------------


def test_churn_score_with_no_git(tmp_path: Path) -> None:
    _make_source(tmp_path, "src/bernstein/no_git.py", "x = 1\n")

    with patch("bernstein.core.quality.code_health.subprocess.run") as mock_run:
        mock_run.return_value = _mock_subprocess_result(returncode=128, stdout="")
        score = compute_file_health("src/bernstein/no_git.py", tmp_path)

    # No git → neutral score (0.5)
    assert score.churn_score == pytest.approx(0.5)


def test_churn_score_low_for_high_churn(tmp_path: Path) -> None:
    _make_source(tmp_path, "src/bernstein/hot.py", "x = 1\n")

    commit_lines = "\n".join(f"abc{i:04d} commit message {i}" for i in range(80))
    with patch("bernstein.core.quality.code_health.subprocess.run") as mock_run:
        mock_run.return_value = _mock_subprocess_result(returncode=0, stdout=commit_lines)
        score = compute_file_health("src/bernstein/hot.py", tmp_path)

    assert score.churn_score == pytest.approx(0.2)  # 1.0 - 80/100


# ---------------------------------------------------------------------------
# Coupling scoring
# ---------------------------------------------------------------------------


def test_coupling_score_high_for_isolated_file(tmp_path: Path) -> None:
    _make_source(tmp_path, "src/bernstein/isolated.py", "x = 1\n")

    _stub_git(tmp_path)
    score = compute_file_health("src/bernstein/isolated.py", tmp_path)
    # No other files import it, and it imports nothing from bernstein
    assert score.coupling_score >= 0.9


# ---------------------------------------------------------------------------
# check_health_delta
# ---------------------------------------------------------------------------


def test_check_health_delta_detects_degradation(tmp_path: Path) -> None:
    _make_source(tmp_path, "src/bernstein/mod.py", "x = 1\n")

    before = FileHealthScore(
        file_path="src/bernstein/mod.py",
        overall_score=0.9,
        complexity_score=1.0,
        bug_density_score=1.0,
        test_coverage_score=1.0,
        churn_score=1.0,
        coupling_score=1.0,
        last_updated="2026-01-01T00:00:00+00:00",
    )

    _stub_git(tmp_path)
    delta = check_health_delta("src/bernstein/mod.py", before, tmp_path)

    assert isinstance(delta, HealthDelta)
    assert delta.before == pytest.approx(0.9)
    # After is computed fresh (lower due to no test file → 0.0 coverage)
    assert delta.after < 0.9
    assert delta.delta < 0
    assert delta.degraded is True


def test_check_health_delta_detects_improvement(tmp_path: Path) -> None:
    _make_source(tmp_path, "src/bernstein/mod2.py", "x = 1\n")
    test_dir = tmp_path / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_mod2.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    before = FileHealthScore(
        file_path="src/bernstein/mod2.py",
        overall_score=0.2,
        complexity_score=0.1,
        bug_density_score=0.1,
        test_coverage_score=0.0,
        churn_score=0.1,
        coupling_score=0.1,
        last_updated="2026-01-01T00:00:00+00:00",
    )

    _stub_git(tmp_path)
    delta = check_health_delta("src/bernstein/mod2.py", before, tmp_path)

    assert delta.delta > 0
    assert delta.degraded is False


# ---------------------------------------------------------------------------
# get_unhealthiest_files
# ---------------------------------------------------------------------------


def test_get_unhealthiest_files_returns_sorted(tmp_path: Path) -> None:
    # Create a "healthy" file (simple, with test)
    _make_source(tmp_path, "src/bernstein/healthy.py", "x = 1\n")
    test_dir = tmp_path / "tests" / "unit"
    test_dir.mkdir(parents=True)
    (test_dir / "test_healthy.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    # Create a less healthy file (complex, no test)
    lines = ["def f(x):\n"]
    for i in range(30):
        lines.append(f"    if x == {i}:\n        pass\n")
    _make_source(tmp_path, "src/bernstein/unhealthy.py", "".join(lines))

    _stub_git(tmp_path)
    results = get_unhealthiest_files(tmp_path, top_n=5)

    assert len(results) >= 2
    # The unhealthy file should appear before the healthy one
    paths = [r.file_path for r in results]
    assert paths.index("src/bernstein/unhealthy.py") < paths.index("src/bernstein/healthy.py")


def test_get_unhealthiest_files_respects_top_n(tmp_path: Path) -> None:
    for i in range(5):
        _make_source(tmp_path, f"src/bernstein/mod{i}.py", "x = 1\n")

    _stub_git(tmp_path)
    results = get_unhealthiest_files(tmp_path, top_n=2)

    assert len(results) <= 2


def test_get_unhealthiest_files_empty_without_src(tmp_path: Path) -> None:
    results = get_unhealthiest_files(tmp_path)
    assert results == []


# ---------------------------------------------------------------------------
# Overall score aggregation
# ---------------------------------------------------------------------------


def test_overall_score_is_weighted_sum(tmp_path: Path) -> None:
    _make_source(tmp_path, "src/bernstein/target.py", "x = 1\n")

    _stub_git(tmp_path)
    score = compute_file_health("src/bernstein/target.py", tmp_path)

    expected = round(
        0.30 * score.complexity_score
        + 0.20 * score.bug_density_score
        + 0.15 * score.test_coverage_score
        + 0.15 * score.churn_score
        + 0.20 * score.coupling_score,
        4,
    )
    assert score.overall_score == expected


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(root: Path, relative: str, content: str) -> Path:
    """Create a source file at *root/relative* with parent dirs."""
    p = root / relative
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class _MockSubprocessResult:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _mock_subprocess_result(returncode: int, stdout: str) -> _MockSubprocessResult:
    return _MockSubprocessResult(returncode=returncode, stdout=stdout)


def _stub_git(tmp_path: Path) -> None:
    """Initialise a bare git repo so churn scoring works without mocks."""
    import subprocess

    subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        capture_output=True,
        check=False,
    )
    # Stage and commit all files so git log works
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init", "--allow-empty"],
        capture_output=True,
        check=False,
    )
