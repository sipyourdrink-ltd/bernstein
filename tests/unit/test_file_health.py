"""Tests for per-file code health score tracking."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.file_health import (
    DEGRADATION_THRESHOLD,
    MIN_HEALTHY_SCORE,
    FileHealthTracker,
    _bug_density_score,
    _composite,
    _compute_complexity_score,
    _compute_coupling_score,
    _score_to_grade,
)

# ---------------------------------------------------------------------------
# Unit tests for sub-score calculators
# ---------------------------------------------------------------------------


def test_complexity_score_simple_file(tmp_path: Path) -> None:
    """A simple Python file with minimal branching scores high."""
    src = tmp_path / "simple.py"
    src.write_text("def hello():\n    return 42\n")
    score = _compute_complexity_score(src)
    assert score >= 80, f"Expected ≥80 for simple file, got {score}"


def test_complexity_score_complex_file(tmp_path: Path) -> None:
    """A file with many branches scores lower than a simple file."""
    # Build a file with lots of if/for/while/try
    branches = "\n    ".join(f"if x > {i}:\n        pass" for i in range(20))
    src = tmp_path / "complex.py"
    src.write_text(f"def foo(x):\n    {branches}\n")
    simple = tmp_path / "simple.py"
    simple.write_text("def bar():\n    return 1\n")
    complex_score = _compute_complexity_score(src)
    simple_score = _compute_complexity_score(simple)
    assert complex_score < simple_score, "Complex file should score lower than simple file"


def test_complexity_score_nonpython_file(tmp_path: Path) -> None:
    """Non-Python files return a neutral default (80)."""
    f = tmp_path / "README.md"
    f.write_text("# hello\n")
    assert _compute_complexity_score(f) == 80


def test_complexity_score_missing_file(tmp_path: Path) -> None:
    """Missing file returns neutral score (50)."""
    score = _compute_complexity_score(tmp_path / "nonexistent.py")
    assert score == 50


def test_coupling_score_no_imports(tmp_path: Path) -> None:
    """File with no imports gets maximum coupling score (100)."""
    src = tmp_path / "standalone.py"
    src.write_text("x = 1\n")
    assert _compute_coupling_score(src) == 100


def test_coupling_score_many_imports(tmp_path: Path) -> None:
    """File with 30+ imports gets score of 0."""
    imports = "\n".join(f"import mod{i}" for i in range(35))
    src = tmp_path / "heavy.py"
    src.write_text(imports + "\n")
    assert _compute_coupling_score(src) == 0


def test_bug_density_no_history() -> None:
    """No task history returns slightly-above-neutral score (80)."""
    assert _bug_density_score(0, 0) == 80


def test_bug_density_all_success() -> None:
    """All successes → max score (100)."""
    assert _bug_density_score(0, 10) == 100


def test_bug_density_all_failures() -> None:
    """All failures → score of 0."""
    assert _bug_density_score(10, 0) == 0


def test_bug_density_mixed() -> None:
    """50% failure rate → score of 50."""
    assert _bug_density_score(5, 5) == 50


def test_score_to_grade_boundaries() -> None:
    """Grade boundaries: A≥90, B≥80, C≥70, D≥60, F<60."""
    assert _score_to_grade(100) == "A"
    assert _score_to_grade(90) == "A"
    assert _score_to_grade(89) == "B"
    assert _score_to_grade(80) == "B"
    assert _score_to_grade(79) == "C"
    assert _score_to_grade(70) == "C"
    assert _score_to_grade(69) == "D"
    assert _score_to_grade(60) == "D"
    assert _score_to_grade(59) == "F"
    assert _score_to_grade(0) == "F"


def test_composite_score_clamped() -> None:
    """Composite score is always clamped to [0, 100]."""
    assert _composite(0, 0, 0, 0, 0) == 0
    assert _composite(100, 100, 100, 100, 100) == 100


def test_composite_weighted() -> None:
    """Composite score is a weighted average of sub-scores."""
    # All 60 → composite should be 60
    score = _composite(60, 60, 60, 60, 60)
    assert score == 60


# ---------------------------------------------------------------------------
# FileHealthTracker integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".sdd"
    d.mkdir()
    return d


@pytest.fixture()
def tracker(sdd_dir: Path) -> FileHealthTracker:
    return FileHealthTracker(sdd_dir=sdd_dir, workdir=sdd_dir.parent)


def test_get_all_empty(tracker: FileHealthTracker) -> None:
    """get_all returns empty list when no files tracked."""
    assert tracker.get_all() == []


def test_get_missing_file(tracker: FileHealthTracker) -> None:
    """get returns None for an untracked file."""
    assert tracker.get("src/foo.py") is None


def test_compute_and_record_success(sdd_dir: Path, tracker: FileHealthTracker) -> None:
    """Record a successful task touch and verify score is persisted."""
    # Create a real Python file so complexity/coupling work
    src = sdd_dir.parent / "src" / "foo.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("def hello():\n    return 1\n")

    with patch(
        "bernstein.core.file_health._compute_churn_score", return_value=85
    ):
        score, _flagged = tracker.compute_and_record("src/foo.py", "task-1", "success")

    assert score.path == "src/foo.py"
    assert 0 <= score.total <= 100
    assert score.success_touches == 1
    assert score.failure_touches == 0
    assert score.grade in {"A", "B", "C", "D", "F"}

    # Should be persisted
    loaded = tracker.get("src/foo.py")
    assert loaded is not None
    assert loaded.success_touches == 1


def test_compute_and_record_failure_increases_density(
    sdd_dir: Path, tracker: FileHealthTracker
) -> None:
    """A failure touch increases failure_touches and may flag the file."""
    src = sdd_dir.parent / "src" / "buggy.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x = 1\n")

    with patch("bernstein.core.file_health._compute_churn_score", return_value=70):
        score, _ = tracker.compute_and_record("src/buggy.py", "task-fail", "failure")

    assert score.failure_touches == 1
    assert score.bug_density_score < 100


def test_flagged_when_score_below_threshold(
    sdd_dir: Path, tracker: FileHealthTracker
) -> None:
    """Files with total score below MIN_HEALTHY_SCORE are flagged."""
    src = sdd_dir.parent / "src" / "bad.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    # Maximally complex file: many branches
    branches = "\n    ".join(f"if x > {i}:\n        pass" for i in range(50))
    src.write_text(f"def foo(x):\n    {branches}\n")

    with patch("bernstein.core.file_health._compute_churn_score", return_value=0):
        # Patch bug_density to force a low score
        with patch("bernstein.core.file_health._bug_density_score", return_value=0):
            score, flagged = tracker.compute_and_record(
                "src/bad.py", "task-bad", "failure"
            )

    if score.total < MIN_HEALTHY_SCORE:
        assert flagged
        assert score.flagged


def test_flagged_on_degradation(
    sdd_dir: Path, tracker: FileHealthTracker
) -> None:
    """Score drop ≥ DEGRADATION_THRESHOLD flags the file."""
    src = sdd_dir.parent / "src" / "degrade.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x = 1\n")

    # First touch: high score (all sub-scores = 90)
    with (
        patch("bernstein.core.file_health._compute_complexity_score", return_value=90),
        patch("bernstein.core.file_health._compute_coupling_score", return_value=90),
        patch("bernstein.core.file_health._compute_churn_score", return_value=90),
        patch("bernstein.core.file_health._compute_coverage_score", return_value=90),
        patch("bernstein.core.file_health._bug_density_score", return_value=90),
    ):
        score1, _ = tracker.compute_and_record("src/degrade.py", "t1", "success")

    prev_total = score1.total

    # Second touch: much lower score (all = 60)
    with (
        patch("bernstein.core.file_health._compute_complexity_score", return_value=60),
        patch("bernstein.core.file_health._compute_coupling_score", return_value=60),
        patch("bernstein.core.file_health._compute_churn_score", return_value=60),
        patch("bernstein.core.file_health._compute_coverage_score", return_value=60),
        patch("bernstein.core.file_health._bug_density_score", return_value=60),
    ):
        score2, flagged2 = tracker.compute_and_record("src/degrade.py", "t2", "failure")

    if prev_total - score2.total >= DEGRADATION_THRESHOLD:
        assert flagged2


def test_get_degraded_returns_unhealthy_files(
    sdd_dir: Path, tracker: FileHealthTracker
) -> None:
    """get_degraded returns only files below the threshold."""
    src = sdd_dir.parent / "src" / "poor.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x = 1\n")

    with (
        patch("bernstein.core.file_health._compute_complexity_score", return_value=30),
        patch("bernstein.core.file_health._compute_coupling_score", return_value=30),
        patch("bernstein.core.file_health._compute_churn_score", return_value=30),
        patch("bernstein.core.file_health._compute_coverage_score", return_value=30),
        patch("bernstein.core.file_health._bug_density_score", return_value=30),
    ):
        tracker.compute_and_record("src/poor.py", "t1", "failure")

    degraded = tracker.get_degraded(threshold=MIN_HEALTHY_SCORE)
    assert any(s.path == "src/poor.py" for s in degraded)


def test_record_task_outcome_multiple_files(
    sdd_dir: Path, tracker: FileHealthTracker
) -> None:
    """record_task_outcome updates all files in owned_files."""
    for name in ("a.py", "b.py"):
        src = sdd_dir.parent / "src" / name
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("x = 1\n")

    with patch("bernstein.core.file_health._compute_churn_score", return_value=70):
        results = tracker.record_task_outcome(
            "task-multi", ["src/a.py", "src/b.py"], "success"
        )

    assert len(results) == 2
    paths = {s.path for s, _ in results}
    assert "src/a.py" in paths
    assert "src/b.py" in paths


def test_touch_log_written(sdd_dir: Path, tracker: FileHealthTracker) -> None:
    """Touch events are appended to file_health_touches.jsonl."""
    src = sdd_dir.parent / "src" / "t.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("x = 1\n")

    with patch("bernstein.core.file_health._compute_churn_score", return_value=70):
        tracker.compute_and_record("src/t.py", "task-123", "success")

    touch_path = sdd_dir / "metrics" / "file_health_touches.jsonl"
    assert touch_path.exists()
    lines = [l for l in touch_path.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["task_id"] == "task-123"
    assert record["outcome"] == "success"
    assert record["path"] == "src/t.py"


def test_get_flagged_empty_initially(tracker: FileHealthTracker) -> None:
    """get_flagged returns empty list when no files have been flagged."""
    assert tracker.get_flagged() == []


# ---------------------------------------------------------------------------
# Route integration smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_file_health_route_empty(tmp_path: Path) -> None:
    """GET /quality/file-health returns empty list when no files tracked."""
    from httpx import ASGITransport, AsyncClient

    from bernstein.core.server import create_app

    jsonl = tmp_path / "tasks.jsonl"
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    (sdd / "metrics").mkdir()
    app = create_app(jsonl_path=jsonl)
    app.state.sdd_dir = sdd

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/quality/file-health")

    assert resp.status_code == 200
    data = resp.json()
    assert "files" in data
    assert "summary" in data
    assert data["files"] == []


@pytest.mark.anyio
async def test_file_health_flagged_route(tmp_path: Path) -> None:
    """GET /quality/file-health/flagged returns 200 even when empty."""
    from httpx import ASGITransport, AsyncClient

    from bernstein.core.server import create_app

    jsonl = tmp_path / "tasks.jsonl"
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    app = create_app(jsonl_path=jsonl)
    app.state.sdd_dir = sdd

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/quality/file-health/flagged")

    assert resp.status_code == 200
    data = resp.json()
    assert "files" in data
    assert "count" in data


@pytest.mark.anyio
async def test_file_health_single_file_404(tmp_path: Path) -> None:
    """GET /quality/file-health/<path> returns 404 for untracked files."""
    from httpx import ASGITransport, AsyncClient

    from bernstein.core.server import create_app

    jsonl = tmp_path / "tasks.jsonl"
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    app = create_app(jsonl_path=jsonl)
    app.state.sdd_dir = sdd

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/quality/file-health/src/foo.py")

    assert resp.status_code == 404
