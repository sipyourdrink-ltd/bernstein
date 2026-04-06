"""Tests for BBS-style boot sequence animation."""

from __future__ import annotations

import asyncio
import json
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from bernstein.tui.boot_sequence import (
    ANSI_LOGO,
    _count_agents,
    _count_backlog,
    _typewriter,
    play_boot_sequence,
)

# ---------------------------------------------------------------------------
# _count_agents
# ---------------------------------------------------------------------------


def test_count_agents_with_file(tmp_path: Path) -> None:
    """Agent count should match entries in agents.json."""
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    agents_file = runtime / "agents.json"
    agents_file.write_text(json.dumps([{"id": "a1"}, {"id": "a2"}, {"id": "a3"}]))

    assert _count_agents(tmp_path) == 3


def test_count_agents_with_dict_file(tmp_path: Path) -> None:
    """Agent count works when agents.json is a dict keyed by agent id."""
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    agents_file = runtime / "agents.json"
    agents_file.write_text(json.dumps({"a1": {}, "a2": {}}))

    assert _count_agents(tmp_path) == 2


def test_count_agents_missing_file(tmp_path: Path) -> None:
    """Missing agents.json should return 0."""
    assert _count_agents(tmp_path) == 0


# ---------------------------------------------------------------------------
# _count_backlog
# ---------------------------------------------------------------------------


def test_count_backlog_with_files(tmp_path: Path) -> None:
    """Backlog count should match YAML files in backlog dir."""
    backlog = tmp_path / ".sdd" / "backlog"
    backlog.mkdir(parents=True)
    (backlog / "task-001.yaml").write_text("goal: test")
    (backlog / "task-002.yml").write_text("goal: test2")
    (backlog / "notes.txt").write_text("ignored")

    assert _count_backlog(tmp_path) == 2


def test_count_backlog_empty(tmp_path: Path) -> None:
    """Empty or missing backlog dir should return 0."""
    assert _count_backlog(tmp_path) == 0


# ---------------------------------------------------------------------------
# play_boot_sequence — skip paths
# ---------------------------------------------------------------------------


def test_play_boot_sequence_no_splash() -> None:
    """no_splash=True should return immediately without printing."""
    mock_console = MagicMock(spec=Console)
    asyncio.run(play_boot_sequence(mock_console, no_splash=True))
    mock_console.print.assert_not_called()


def test_play_boot_sequence_env_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """BERNSTEIN_NO_SPLASH=1 should skip the entire sequence."""
    monkeypatch.setenv("BERNSTEIN_NO_SPLASH", "1")
    mock_console = MagicMock(spec=Console)
    asyncio.run(play_boot_sequence(mock_console))
    mock_console.print.assert_not_called()


# ---------------------------------------------------------------------------
# _typewriter
# ---------------------------------------------------------------------------


def test_typewriter_outputs_all_chars() -> None:
    """Every character should appear in typewriter output."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=80)
    asyncio.run(_typewriter(console, "HELLO", style="green", delay_ms=0))
    output = buf.getvalue()
    for ch in "HELLO":
        assert ch in output


# ---------------------------------------------------------------------------
# ANSI_LOGO constant
# ---------------------------------------------------------------------------


def test_ansi_art_is_defined() -> None:
    """ANSI art constant should be a non-empty multi-line string."""
    assert isinstance(ANSI_LOGO, str)
    assert len(ANSI_LOGO) > 0
    assert ANSI_LOGO.count("\n") >= 5
