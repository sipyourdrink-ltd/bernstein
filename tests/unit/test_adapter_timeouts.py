"""Unit tests: watchdog timer kills hung agents after timeout."""

from __future__ import annotations

import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.aider import AiderAdapter
from bernstein.adapters.amp import AmpAdapter
from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.cursor import CursorAdapter
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.adapters.generic import GenericAdapter
from bernstein.adapters.qwen import QwenAdapter
from bernstein.adapters.roo_code import RooCodeAdapter
from bernstein.core.models import ModelConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SHORT_TIMEOUT = 0.05  # seconds — fire almost immediately


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock(spec=subprocess.Popen)
    m.pid = pid
    m.poll.return_value = None  # process never exits on its own
    m.stdout = None
    return m


def _make_llm_settings_mock() -> MagicMock:
    m = MagicMock()
    m.openrouter_api_key_paid = None
    m.openrouter_api_key_free = None
    m.oxen_api_key = None
    m.oxen_base_url = "https://hub.oxen.ai/api"
    m.togetherai_user_key = None
    m.g4f_api_key = None
    m.g4f_base_url = "https://g4f.space/v1"
    m.openai_api_key = None
    m.openai_base_url = None
    m.tavily_api_key = None
    return m


# ---------------------------------------------------------------------------
# Core watchdog logic — tested via CodexAdapter as representative
# ---------------------------------------------------------------------------


class TestWatchdogFiresSigterm:
    """Watchdog sends SIGTERM after timeout and timer is stored in SpawnResult."""

    def test_sigterm_called_after_timeout(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=9001)

        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock),
            patch("bernstein.adapters.base.os.getpgid", return_value=9001),
            patch("bernstein.adapters.base.os.killpg") as mock_killpg,
            patch("bernstein.adapters.base.subprocess.run"),  # suppress git calls
        ):
            result = adapter.spawn(
                prompt="sleep forever",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="timeout-codex",
                timeout_seconds=_SHORT_TIMEOUT,
            )
            time.sleep(_SHORT_TIMEOUT * 5)  # let the timer fire

        mock_killpg.assert_called_with(9001, signal.SIGTERM)
        # Clean up
        if result.timer:
            result.timer.cancel()

    def test_timer_stored_in_spawn_result(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=9002)

        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="timer-stored",
                timeout_seconds=1800,
            )

        assert result.timer is not None
        result.timer.cancel()

    def test_cancel_prevents_kill(self, tmp_path: Path) -> None:
        """Cancelling timer before it fires must prevent SIGTERM."""
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=9003)

        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock),
            patch("bernstein.adapters.base.os.killpg") as mock_killpg,
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="cancel-test",
                timeout_seconds=_SHORT_TIMEOUT,
            )
            assert result.timer is not None
            result.timer.cancel()
            time.sleep(_SHORT_TIMEOUT * 5)  # wait past timeout

        mock_killpg.assert_not_called()

    def test_no_kill_when_process_already_exited(self, tmp_path: Path) -> None:
        """If process exits before timeout, watchdog must not send signals."""
        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=9004)
        proc_mock.poll.return_value = 0  # process already exited

        with (
            patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock),
            patch("bernstein.adapters.base.os.killpg") as mock_killpg,
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="early-exit",
                timeout_seconds=_SHORT_TIMEOUT,
            )
            time.sleep(_SHORT_TIMEOUT * 5)

        mock_killpg.assert_not_called()
        if result.timer:
            result.timer.cancel()


# ---------------------------------------------------------------------------
# All 7 specified adapters expose timeout_seconds and return a timer
# ---------------------------------------------------------------------------


class TestAllAdaptersHaveTimeout:
    """Each adapter's spawn() accepts timeout_seconds and returns a live timer."""

    def _assert_has_timer(
        self,
        adapter_cls: type,
        popen_path: str,
        tmp_path: Path,
        extra_patches: dict | None = None,
        *,
        model: str = "gpt-4o",
        session_id: str = "s1",
    ) -> None:
        proc_mock = _make_popen_mock(pid=8000)
        patches = [patch(popen_path, return_value=proc_mock)]
        if extra_patches:
            for target, value in extra_patches.items():
                patches.append(patch(target, return_value=value))

        adapter = adapter_cls()
        with patches[0]:
            if len(patches) > 1:
                with patches[1]:
                    result = adapter.spawn(
                        prompt="hello",
                        workdir=tmp_path,
                        model_config=ModelConfig(model=model, effort="high"),
                        session_id=session_id,
                        timeout_seconds=3600,
                    )
            else:
                result = adapter.spawn(
                    prompt="hello",
                    workdir=tmp_path,
                    model_config=ModelConfig(model=model, effort="high"),
                    session_id=session_id,
                    timeout_seconds=3600,
                )
        assert result.timer is not None
        result.timer.cancel()

    def test_aider_has_timer(self, tmp_path: Path) -> None:
        self._assert_has_timer(AiderAdapter, "bernstein.adapters.aider.subprocess.Popen", tmp_path)

    def test_amp_has_timer(self, tmp_path: Path) -> None:
        self._assert_has_timer(AmpAdapter, "bernstein.adapters.amp.subprocess.Popen", tmp_path)

    def test_codex_has_timer(self, tmp_path: Path) -> None:
        self._assert_has_timer(CodexAdapter, "bernstein.adapters.codex.subprocess.Popen", tmp_path)

    def test_cursor_has_timer(self, tmp_path: Path) -> None:
        self._assert_has_timer(CursorAdapter, "bernstein.adapters.cursor.subprocess.Popen", tmp_path)

    def test_gemini_has_timer(self, tmp_path: Path) -> None:
        self._assert_has_timer(GeminiAdapter, "bernstein.adapters.gemini.subprocess.Popen", tmp_path)

    def test_roo_code_has_timer(self, tmp_path: Path) -> None:
        self._assert_has_timer(RooCodeAdapter, "bernstein.adapters.roo_code.subprocess.Popen", tmp_path)

    def test_generic_has_timer(self, tmp_path: Path) -> None:
        self._assert_has_timer(
            lambda: GenericAdapter(cli_command="mytool"),  # type: ignore[arg-type]
            "bernstein.adapters.generic.subprocess.Popen",
            tmp_path,
        )

    def test_qwen_has_timer(self, tmp_path: Path) -> None:
        adapter = QwenAdapter()
        proc_mock = _make_popen_mock(pid=8010)
        settings_mock = _make_llm_settings_mock()
        with (
            patch("bernstein.adapters.qwen.LLMSettings", return_value=settings_mock),
            patch("bernstein.adapters.qwen.subprocess.Popen", return_value=proc_mock),
        ):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="sonnet", effort="high"),
                session_id="qwen-timer",
                timeout_seconds=3600,
            )
        assert result.timer is not None
        result.timer.cancel()


# ---------------------------------------------------------------------------
# Default timeout is 1800 s
# ---------------------------------------------------------------------------


class TestDefaultTimeout:
    def test_default_timeout_1800(self, tmp_path: Path) -> None:
        """Watchdog fires after default 1800 s — verified by checking timer interval."""
        import threading

        adapter = CodexAdapter()
        proc_mock = _make_popen_mock(pid=7001)

        with patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock):
            result = adapter.spawn(
                prompt="hello",
                workdir=tmp_path,
                model_config=ModelConfig(model="gpt-4o", effort="high"),
                session_id="default-timeout",
                # no timeout_seconds → uses default 1800
            )

        assert result.timer is not None
        assert isinstance(result.timer, threading.Timer)
        assert result.timer.interval == 1800.0
        result.timer.cancel()
