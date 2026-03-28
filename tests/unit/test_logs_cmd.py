"""Tests for the `bernstein logs` CLI command."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.main import _find_agent_logs, logs_cmd


# ---------------------------------------------------------------------------
# _find_agent_logs helper
# ---------------------------------------------------------------------------


def test_find_agent_logs_empty_dir(tmp_path: Path) -> None:
    assert _find_agent_logs(tmp_path, None) == []


def test_find_agent_logs_missing_dir(tmp_path: Path) -> None:
    assert _find_agent_logs(tmp_path / "nonexistent", None) == []


def test_find_agent_logs_returns_sorted_by_mtime(tmp_path: Path) -> None:
    a = tmp_path / "backend-abc123.log"
    b = tmp_path / "frontend-def456.log"
    a.write_text("a")
    time.sleep(0.01)
    b.write_text("b")
    result = _find_agent_logs(tmp_path, None)
    assert result[-1] == b  # most recent last


def test_find_agent_logs_excludes_watchdog(tmp_path: Path) -> None:
    (tmp_path / "watchdog.log").write_text("watchdog")
    (tmp_path / "backend-abc123.log").write_text("agent")
    result = _find_agent_logs(tmp_path, None)
    names = [p.name for p in result]
    assert "watchdog.log" not in names
    assert "backend-abc123.log" in names


def test_find_agent_logs_filters_by_agent_id(tmp_path: Path) -> None:
    (tmp_path / "backend-abc123.log").write_text("backend")
    (tmp_path / "frontend-xyz789.log").write_text("frontend")
    result = _find_agent_logs(tmp_path, "abc123")
    assert len(result) == 1
    assert result[0].name == "backend-abc123.log"


# ---------------------------------------------------------------------------
# logs_cmd CLI
# ---------------------------------------------------------------------------


@pytest.fixture()
def runtime_dir(tmp_path: Path) -> Path:
    rdir = tmp_path / "runtime"
    rdir.mkdir()
    log = rdir / "backend-aabbccdd.log"
    log.write_text("line1\nline2\nline3\n")
    return rdir


def test_logs_cmd_accepts_follow_flag(runtime_dir: Path) -> None:
    """--follow flag is accepted without error (exits via KeyboardInterrupt handled internally)."""
    runner = CliRunner()
    # We can't actually follow, but we can verify the flag is accepted by checking
    # that the command parses without a "No such option" error.
    # We patch the time.sleep to raise immediately so the loop exits.
    import bernstein.cli.main as main_mod

    original_sleep = main_mod.time.sleep
    call_count = 0

    def _fast_sleep(s: float) -> None:  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count >= 1:
            raise KeyboardInterrupt

    main_mod.time.sleep = _fast_sleep  # type: ignore[attr-defined]
    try:
        result = runner.invoke(logs_cmd, ["--follow", "--runtime-dir", str(runtime_dir)])
    finally:
        main_mod.time.sleep = original_sleep  # type: ignore[attr-defined]

    assert "No such option" not in result.output
    assert result.exit_code == 0


def test_logs_cmd_shows_last_n_lines(runtime_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(logs_cmd, ["--lines", "2", "--runtime-dir", str(runtime_dir)])
    assert result.exit_code == 0
    assert "line2" in result.output
    assert "line3" in result.output
    # line1 should NOT appear since we only asked for 2
    assert "line1" not in result.output


def test_logs_cmd_no_logs_exits_nonzero(tmp_path: Path) -> None:
    empty = tmp_path / "runtime"
    empty.mkdir()
    runner = CliRunner()
    result = runner.invoke(logs_cmd, ["--runtime-dir", str(empty)])
    assert result.exit_code != 0
    assert "No agent logs found" in result.output


def test_logs_cmd_agent_filter(runtime_dir: Path) -> None:
    # Add a second log file
    (runtime_dir / "frontend-zzzzzzzz.log").write_text("frontend content\n")
    runner = CliRunner()
    result = runner.invoke(logs_cmd, ["--agent", "aabbccdd", "--runtime-dir", str(runtime_dir)])
    assert result.exit_code == 0
    assert "backend-aabbccdd.log" in result.output


def test_logs_cmd_agent_filter_no_match(runtime_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(logs_cmd, ["--agent", "nomatch", "--runtime-dir", str(runtime_dir)])
    assert result.exit_code != 0
    assert "No agent logs found" in result.output
    assert "nomatch" in result.output
