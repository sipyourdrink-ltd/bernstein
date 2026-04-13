"""Unit tests for cProfile integration in bernstein.core.profiler."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from bernstein.core.profiler import (
    OrchestratorProfiler,
    ProfileResult,
    ProfilerSession,
    resolve_profile_output_dir,
)

# ---------------------------------------------------------------------------
# ProfilerSession (context-manager API)
# ---------------------------------------------------------------------------


def test_profiler_session_creates_output_files(tmp_path: Path) -> None:
    """ProfilerSession writes .prof and .txt files on exit."""
    output_dir = tmp_path / "profiles"

    with ProfilerSession(output_dir):
        # Do a tiny bit of work so cProfile has something to record
        _ = sum(range(100))

    prof_files = list(output_dir.glob("profile-*.prof"))
    txt_files = list(output_dir.glob("profile-*.txt"))

    assert len(prof_files) == 1, "Expected exactly one .prof file"
    assert len(txt_files) == 1, "Expected exactly one .txt file"


def test_profiler_session_creates_output_dir(tmp_path: Path) -> None:
    """ProfilerSession creates the output directory if it doesn't exist."""
    output_dir = tmp_path / "nested" / "profiles"
    assert not output_dir.exists()

    with ProfilerSession(output_dir):
        pass  # Session creates output dir on exit

    assert output_dir.is_dir()


def test_profiler_session_txt_contains_header(tmp_path: Path) -> None:
    """The .txt report includes the Bernstein header lines."""
    output_dir = tmp_path / "profiles"

    with ProfilerSession(output_dir):
        pass  # Session writes profile on exit

    txt_file = next(output_dir.glob("profile-*.txt"))
    content = txt_file.read_text()

    assert "Bernstein orchestrator profile" in content
    assert "Elapsed:" in content
    assert "Top" in content


def test_profiler_session_elapsed_positive(tmp_path: Path) -> None:
    """The .txt report records a positive elapsed time."""
    output_dir = tmp_path / "profiles"

    with ProfilerSession(output_dir):
        time.sleep(0.01)

    txt_file = next(output_dir.glob("profile-*.txt"))
    content = txt_file.read_text()

    # Extract the elapsed value
    for line in content.splitlines():
        if "Elapsed:" in line:
            elapsed_str = line.split("Elapsed:")[1].strip().rstrip("s")
            elapsed = float(elapsed_str)
            assert elapsed > 0.0
            break
    else:
        pytest.fail("No Elapsed line found in report")


def test_profiler_session_custom_top_n(tmp_path: Path) -> None:
    """top_n parameter controls how many functions are printed."""
    output_dir = tmp_path / "profiles"

    with ProfilerSession(output_dir, top_n=5):
        _ = sum(range(10))

    txt_file = next(output_dir.glob("profile-*.txt"))
    content = txt_file.read_text()
    assert "Top 5 functions" in content


def test_profiler_session_prof_file_is_valid_pstats(tmp_path: Path) -> None:
    """The .prof file can be loaded with pstats.Stats."""
    import pstats

    output_dir = tmp_path / "profiles"

    with ProfilerSession(output_dir):
        pass  # Session writes .prof file on exit

    prof_file = next(output_dir.glob("profile-*.prof"))
    # Should not raise
    stats = pstats.Stats(str(prof_file))
    assert stats.total_calls >= 0


def test_profiler_session_propagates_exception(tmp_path: Path) -> None:
    """ProfilerSession saves output even when an exception escapes."""
    output_dir = tmp_path / "profiles"

    with pytest.raises(ValueError, match="test error"):
        with ProfilerSession(output_dir):
            raise ValueError("test error")

    # Files should still be written
    assert list(output_dir.glob("profile-*.prof"))
    assert list(output_dir.glob("profile-*.txt"))


# ---------------------------------------------------------------------------
# OrchestratorProfiler (explicit start/stop API)
# ---------------------------------------------------------------------------


def test_orchestrator_profiler_start_stop_produces_results(tmp_path: Path) -> None:
    """start()/stop() cycle returns a ProfileResult with timing data."""
    output_dir = tmp_path / "profiles"
    profiler = OrchestratorProfiler(output_dir)

    profiler.start()
    _ = sum(range(1000))
    result = profiler.stop()

    assert isinstance(result, ProfileResult)
    assert result.total_time > 0.0
    assert result.output_path is not None
    assert result.output_path.exists()


def test_orchestrator_profiler_top_functions_extraction(tmp_path: Path) -> None:
    """stop() extracts top functions with name, cumtime, and calls."""
    output_dir = tmp_path / "profiles"
    profiler = OrchestratorProfiler(output_dir, top_n=10)

    profiler.start()
    # Do enough work to ensure multiple functions appear
    for _ in range(100):
        _ = sorted(range(50), reverse=True)
    result = profiler.stop()

    assert len(result.top_functions) > 0
    for name, cumtime, calls in result.top_functions:
        assert isinstance(name, str)
        assert isinstance(cumtime, float)
        assert isinstance(calls, int)
        assert calls >= 0


def test_orchestrator_profiler_stop_without_start_raises(tmp_path: Path) -> None:
    """stop() raises RuntimeError if start() was not called."""
    profiler = OrchestratorProfiler(tmp_path / "profiles")
    with pytest.raises(RuntimeError, match="start.*must be called"):
        profiler.stop()


def test_orchestrator_profiler_save_stats_writes_file(tmp_path: Path) -> None:
    """save_stats() dumps a pstats binary to the given path."""
    import pstats

    output_dir = tmp_path / "profiles"
    profiler = OrchestratorProfiler(output_dir)

    profiler.start()
    _ = sum(range(100))
    profiler.stop()

    custom_path = tmp_path / "custom" / "my.prof"
    profiler.save_stats(custom_path)

    assert custom_path.exists()
    # Must be loadable by pstats
    stats = pstats.Stats(str(custom_path))
    assert stats.total_calls >= 0


def test_orchestrator_profiler_save_stats_without_start_raises(tmp_path: Path) -> None:
    """save_stats() raises RuntimeError if start() was not called."""
    profiler = OrchestratorProfiler(tmp_path / "profiles")
    with pytest.raises(RuntimeError, match="start.*must be called"):
        profiler.save_stats(tmp_path / "nope.prof")


def test_orchestrator_profiler_creates_output_dir(tmp_path: Path) -> None:
    """start() creates the output directory if it doesn't exist."""
    output_dir = tmp_path / "deep" / "nested" / "profiles"
    assert not output_dir.exists()

    profiler = OrchestratorProfiler(output_dir)
    profiler.start()
    profiler.stop()

    assert output_dir.is_dir()


def test_orchestrator_profiler_writes_txt_and_prof(tmp_path: Path) -> None:
    """stop() writes both .prof and .txt files to output_dir."""
    output_dir = tmp_path / "profiles"
    profiler = OrchestratorProfiler(output_dir)

    profiler.start()
    _ = sum(range(100))
    profiler.stop()

    prof_files = list(output_dir.glob("profile-*.prof"))
    txt_files = list(output_dir.glob("profile-*.txt"))
    assert len(prof_files) == 1
    assert len(txt_files) == 1


# ---------------------------------------------------------------------------
# to_markdown
# ---------------------------------------------------------------------------


def test_to_markdown_output_format(tmp_path: Path) -> None:
    """to_markdown() produces a valid Markdown table."""
    result = ProfileResult(
        total_time=1.234,
        top_functions=[
            ("module.py:10(foo)", 0.5, 100),
            ("module.py:20(bar)", 0.3, 50),
        ],
        output_path=tmp_path / "test.prof",
    )
    md = OrchestratorProfiler.to_markdown(result)

    assert "## Orchestrator Profile" in md
    assert "**Total time:** 1.23" in md
    assert "| # | Function | Cumulative (s) | Calls |" in md
    assert "`module.py:10(foo)`" in md
    assert "`module.py:20(bar)`" in md
    assert "0.5000" in md
    assert "100" in md
    assert str(tmp_path / "test.prof") in md


def test_to_markdown_no_output_path() -> None:
    """to_markdown() omits the binary path line when output_path is None."""
    result = ProfileResult(
        total_time=0.5,
        top_functions=[("a.py:1(f)", 0.1, 10)],
        output_path=None,
    )
    md = OrchestratorProfiler.to_markdown(result)

    assert "Binary profile:" not in md
    assert "## Orchestrator Profile" in md


def test_to_markdown_empty_functions() -> None:
    """to_markdown() handles empty top_functions list."""
    result = ProfileResult(total_time=0.0, top_functions=[])
    md = OrchestratorProfiler.to_markdown(result)

    assert "## Orchestrator Profile" in md
    assert "| # | Function | Cumulative (s) | Calls |" in md
    # No data rows
    lines = md.strip().splitlines()
    # Header (title, blank, total time, blank, header row, separator row) = 6 lines
    assert len(lines) == 6


# ---------------------------------------------------------------------------
# resolve_profile_output_dir
# ---------------------------------------------------------------------------


def test_resolve_profile_output_dir_default(tmp_path: Path) -> None:
    """Returns .sdd/runtime/profiles/ by default."""
    os.environ.pop("BERNSTEIN_PROFILE_OUTPUT", None)
    result = resolve_profile_output_dir(tmp_path)
    assert result == tmp_path / ".sdd" / "runtime" / "profiles"


def test_resolve_profile_output_dir_env_override(tmp_path: Path) -> None:
    """Respects BERNSTEIN_PROFILE_OUTPUT env var."""
    override = tmp_path / "custom_profiles"
    os.environ["BERNSTEIN_PROFILE_OUTPUT"] = str(override)
    try:
        result = resolve_profile_output_dir(tmp_path)
        assert result == override
    finally:
        del os.environ["BERNSTEIN_PROFILE_OUTPUT"]
