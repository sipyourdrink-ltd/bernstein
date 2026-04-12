"""Tests for hot-reload support in evolve (self-development) mode.

Covers:
- Server --reload flag injection in evolve mode
- Orchestrator source-change detection
- Session state saving before restart
- Curl retry flags in agent completion commands
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Server hot-reload: --reload flag added in evolve mode
# ---------------------------------------------------------------------------


class TestServerHotReload:
    """Verify _start_server adds --reload when evolve_mode=True."""

    def _setup_runtime(self, tmp_path: Path) -> None:
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True)

    def test_no_reload_flag_by_default(self, tmp_path: Path) -> None:
        from bernstein.core.bootstrap import _start_server

        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 100

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            _start_server(tmp_path, port=8052)

        cmd = mock_popen.call_args[0][0]
        assert "--reload" not in cmd

    def test_reload_flag_in_evolve_mode(self, tmp_path: Path) -> None:
        from bernstein.core.bootstrap import _start_server

        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 101

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            _start_server(tmp_path, port=8052, evolve_mode=True)

        cmd = mock_popen.call_args[0][0]
        assert "--reload" in cmd
        assert "--reload-dir" in cmd
        reload_dir_idx = cmd.index("--reload-dir")
        reload_dir_val = cmd[reload_dir_idx + 1]
        assert reload_dir_val == str(tmp_path / "src" / "bernstein")

    def test_reload_flag_not_present_when_evolve_false(self, tmp_path: Path) -> None:
        from bernstein.core.bootstrap import _start_server

        self._setup_runtime(tmp_path)
        mock_proc = MagicMock()
        mock_proc.pid = 102

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            _start_server(tmp_path, port=8052, evolve_mode=False)

        cmd = mock_popen.call_args[0][0]
        assert "--reload" not in cmd
        assert "--reload-dir" not in cmd


# ---------------------------------------------------------------------------
# Orchestrator source-change detection
# ---------------------------------------------------------------------------


class TestSourceChangeDetection:
    """Verify _check_source_changed detects modified orchestrator source files."""

    def _make_orchestrator(self, tmp_path: Path) -> object:
        """Build an Orchestrator with minimal config for testing."""
        from bernstein.core.models import OrchestratorConfig
        from bernstein.core.orchestrator import Orchestrator

        config = OrchestratorConfig(evolve_mode=True, dry_run=True)
        spawner = MagicMock()
        with (
            patch("bernstein.core.orchestration.orchestrator.get_collector"),
            patch("bernstein.core.orchestration.orchestrator.build_manifest", return_value=MagicMock()),
            patch("bernstein.core.orchestration.orchestrator.save_manifest"),
        ):
            orch = Orchestrator(config=config, spawner=spawner, workdir=tmp_path)
        return orch

    def test_no_change_returns_false(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        # No source files exist, so nothing is newer
        assert orch._check_source_changed() is False  # type: ignore[union-attr]

    def test_detects_newer_source_file(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)

        src_file = Path("src/bernstein/core/orchestrator.py")
        if not src_file.exists():
            pytest.skip("Source file not available in test environment")

        # Set source_mtime well before the file's actual mtime so the
        # check reliably detects a "newer" file, regardless of when the
        # file was last modified on disk.
        file_mtime = src_file.stat().st_mtime
        orch._source_mtime = file_mtime - 10  # type: ignore[union-attr]
        assert orch._check_source_changed() is True  # type: ignore[union-attr]

    def test_old_source_file_returns_false(self, tmp_path: Path) -> None:
        orch = self._make_orchestrator(tmp_path)
        # Set source_mtime far in the future so nothing is "newer"
        orch._source_mtime = time.time() + 99999  # type: ignore[union-attr]
        assert orch._check_source_changed() is False  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Session save before restart
# ---------------------------------------------------------------------------


class TestSessionSaveBeforeRestart:
    """Verify that _save_session_state is called before _restart in the run loop."""

    def test_run_loop_saves_session_before_restart(self, tmp_path: Path) -> None:
        """When restart_requested flag exists, session state is saved before os.execv."""
        from bernstein.core.models import OrchestratorConfig
        from bernstein.core.orchestrator import Orchestrator

        config = OrchestratorConfig(evolve_mode=True, poll_interval_s=0)
        spawner = MagicMock()

        with (
            patch("bernstein.core.orchestration.orchestrator.get_collector"),
            patch("bernstein.core.orchestration.orchestrator.build_manifest", return_value=MagicMock()),
            patch("bernstein.core.orchestration.orchestrator.save_manifest"),
        ):
            orch = Orchestrator(config=config, spawner=spawner, workdir=tmp_path)

        # Create the restart flag
        runtime_dir = tmp_path / ".sdd" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "restart_requested").write_text("")

        call_order: list[str] = []

        def track_save() -> None:
            call_order.append("save")

        def track_restart() -> None:
            call_order.append("restart")
            orch._running = False  # break the loop instead of os.execv

        # Make tick() return a minimal result
        from bernstein.core.orchestrator import TickResult

        orch.tick = MagicMock(return_value=TickResult())  # type: ignore[method-assign]
        orch._save_session_state = track_save  # type: ignore[method-assign]
        orch._restart = track_restart  # type: ignore[method-assign]

        with patch("time.sleep"):
            orch.run()

        assert "save" in call_order
        assert "restart" in call_order
        assert call_order.index("save") < call_order.index("restart")


# ---------------------------------------------------------------------------
# Curl retry flags in agent prompts
# ---------------------------------------------------------------------------


class TestCurlRetryInPrompts:
    """Verify completion curl commands include --retry flags for resilience."""

    def test_completion_commands_include_retry(self) -> None:
        from bernstein.core.models import Task
        from bernstein.core.spawner import _render_prompt

        task = Task(
            id="test-1",
            title="Fix the bug",
            description="Fix the bug in module X",
            role="backend",
        )
        templates_dir = Path("templates/roles")
        workdir = Path(".")

        prompt = _render_prompt(
            [task],
            templates_dir,
            workdir,
        )

        # The curl commands should include retry flags (connrefused only, not all errors)
        assert "--retry 3" in prompt
        assert "--retry-delay 2" in prompt
        assert "--retry-connrefused" in prompt
        assert "--retry-all-errors" not in prompt


# ---------------------------------------------------------------------------
# Orchestrator _HOT_RELOAD_SOURCES class variable
# ---------------------------------------------------------------------------


class TestHotReloadSourcesList:
    """Verify the hot-reload source file list contains expected entries."""

    def test_contains_orchestrator(self) -> None:
        from bernstein.core.orchestrator import Orchestrator

        assert "src/bernstein/core/orchestrator.py" in Orchestrator._HOT_RELOAD_SOURCES

    def test_contains_spawner(self) -> None:
        from bernstein.core.orchestrator import Orchestrator

        assert "src/bernstein/core/spawner.py" in Orchestrator._HOT_RELOAD_SOURCES

    def test_contains_router(self) -> None:
        from bernstein.core.orchestrator import Orchestrator

        assert "src/bernstein/core/router.py" in Orchestrator._HOT_RELOAD_SOURCES
