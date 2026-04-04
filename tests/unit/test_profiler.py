"""Unit tests for cProfile integration in bernstein.core.profiler."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from bernstein.core.profiler import ProfilerSession, resolve_profile_output_dir


# ---------------------------------------------------------------------------
# ProfilerSession
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
        pass

    assert output_dir.is_dir()


def test_profiler_session_txt_contains_header(tmp_path: Path) -> None:
    """The .txt report includes the Bernstein header lines."""
    output_dir = tmp_path / "profiles"

    with ProfilerSession(output_dir):
        pass

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
        pass

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
