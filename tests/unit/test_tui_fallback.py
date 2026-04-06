"""Tests for TUI-003: terminal capability detection and fallback display."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bernstein.cli.terminal_caps import TerminalCaps
from bernstein.tui.fallback import FallbackDisplay

# ---------------------------------------------------------------------------
# TUI-003: supports_textual property
# ---------------------------------------------------------------------------


class TestSupportsTextual:
    """Tests for the supports_textual capability flag."""

    def test_non_tty_returns_false(self) -> None:
        """Non-TTY environments do not support Textual."""
        caps = TerminalCaps(
            is_tty=False,
            supports_truecolor=True,
            supports_256color=True,
            supports_kitty=False,
            supports_iterm2=False,
            supports_sixel=False,
            term_width=80,
            term_height=24,
        )
        assert caps.supports_textual is False

    def test_dumb_terminal_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TERM=dumb does not support Textual."""
        monkeypatch.setenv("TERM", "dumb")
        caps = TerminalCaps(
            is_tty=True,
            supports_truecolor=False,
            supports_256color=False,
            supports_kitty=False,
            supports_iterm2=False,
            supports_sixel=False,
            term_width=80,
            term_height=24,
        )
        assert caps.supports_textual is False

    def test_bernstein_no_tui_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BERNSTEIN_NO_TUI=1 disables Textual."""
        monkeypatch.setenv("BERNSTEIN_NO_TUI", "1")
        monkeypatch.setenv("TERM", "xterm-256color")
        caps = TerminalCaps(
            is_tty=True,
            supports_truecolor=True,
            supports_256color=True,
            supports_kitty=False,
            supports_iterm2=False,
            supports_sixel=False,
            term_width=80,
            term_height=24,
        )
        assert caps.supports_textual is False

    def test_screen_without_256color_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """screen without 256color is unreliable for Textual."""
        monkeypatch.setenv("TERM", "screen")
        monkeypatch.delenv("BERNSTEIN_NO_TUI", raising=False)
        caps = TerminalCaps(
            is_tty=True,
            supports_truecolor=False,
            supports_256color=False,
            supports_kitty=False,
            supports_iterm2=False,
            supports_sixel=False,
            term_width=80,
            term_height=24,
        )
        assert caps.supports_textual is False

    def test_screen_with_256color_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """screen with 256color support works with Textual."""
        monkeypatch.setenv("TERM", "screen.xterm-256color")
        monkeypatch.delenv("BERNSTEIN_NO_TUI", raising=False)
        caps = TerminalCaps(
            is_tty=True,
            supports_truecolor=False,
            supports_256color=True,
            supports_kitty=False,
            supports_iterm2=False,
            supports_sixel=False,
            term_width=80,
            term_height=24,
        )
        assert caps.supports_textual is True

    def test_normal_tty_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A normal TTY with color support returns True."""
        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.delenv("BERNSTEIN_NO_TUI", raising=False)
        caps = TerminalCaps(
            is_tty=True,
            supports_truecolor=True,
            supports_256color=True,
            supports_kitty=False,
            supports_iterm2=False,
            supports_sixel=False,
            term_width=120,
            term_height=40,
        )
        assert caps.supports_textual is True

    def test_null_caps_returns_false(self) -> None:
        """TerminalCaps.null() should not support Textual."""
        caps = TerminalCaps.null()
        assert caps.supports_textual is False


# ---------------------------------------------------------------------------
# TUI-003: FallbackDisplay
# ---------------------------------------------------------------------------


class TestFallbackDisplay:
    """Tests for the Rich-based fallback display."""

    def test_can_instantiate(self) -> None:
        """FallbackDisplay can be created with defaults."""
        display = FallbackDisplay()
        assert display._server_url == "http://127.0.0.1:8052"
        assert display._interval == pytest.approx(2.0)

    def test_custom_server_url(self) -> None:
        """FallbackDisplay accepts a custom server URL."""
        display = FallbackDisplay(server_url="http://example.com:9999")
        assert display._server_url == "http://example.com:9999"

    def test_custom_interval(self) -> None:
        """FallbackDisplay accepts a custom polling interval."""
        display = FallbackDisplay(interval=5.0)
        assert display._interval == pytest.approx(5.0)

    def test_render_offline(self) -> None:
        """Render shows output when server is unreachable."""
        display = FallbackDisplay()
        # Mock _get to return None (server offline)
        display._get = MagicMock(return_value=None)  # type: ignore[method-assign]
        result = display._render()
        # Group contains renderables; verify we can call it
        assert result is not None

    def test_render_with_data(self) -> None:
        """Render produces output when server returns valid data."""
        display = FallbackDisplay()
        display._get = MagicMock(
            return_value={  # type: ignore[method-assign]
                "active_agents": 3,
                "completed": 5,
                "total": 10,
                "failed": 1,
                "per_role": [
                    {"status": "done", "role": "backend", "title": "Implement auth"},
                    {"status": "in_progress", "role": "qa", "title": "Run tests"},
                ],
            }
        )
        result = display._render()
        assert result is not None

    def test_render_with_empty_per_role(self) -> None:
        """Render handles empty per_role list gracefully."""
        display = FallbackDisplay()
        display._get = MagicMock(
            return_value={  # type: ignore[method-assign]
                "active_agents": 0,
                "completed": 0,
                "total": 0,
                "failed": 0,
                "per_role": [],
            }
        )
        result = display._render()
        assert result is not None


# ---------------------------------------------------------------------------
# TUI-003: _finalize_run_output fallback integration
# ---------------------------------------------------------------------------


class TestFinalizeRunOutputFallback:
    """Tests for the fallback path in _finalize_run_output."""

    def test_fallback_when_textual_unsupported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When Textual is not supported but is TTY, fallback display is used."""
        from bernstein.cli import run_cmd

        mock_caps = MagicMock()
        mock_caps.supports_textual = False
        mock_caps.is_tty = True

        monkeypatch.setattr(
            "bernstein.cli.terminal_caps.detect_capabilities",
            lambda: mock_caps,
        )
        mock_fallback = MagicMock()
        monkeypatch.setattr(run_cmd, "_try_fallback_display", mock_fallback)

        run_cmd._finalize_run_output(quiet=False)
        mock_fallback.assert_called_once()

    def test_summary_when_not_tty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When not a TTY, the static summary is shown."""
        from bernstein.cli import run_cmd

        mock_caps = MagicMock()
        mock_caps.supports_textual = False
        mock_caps.is_tty = False

        monkeypatch.setattr(
            "bernstein.cli.terminal_caps.detect_capabilities",
            lambda: mock_caps,
        )
        mock_summary = MagicMock()
        monkeypatch.setattr(run_cmd, "_show_run_summary", mock_summary)

        run_cmd._finalize_run_output(quiet=False)
        mock_summary.assert_called_once()

    def test_textual_used_when_supported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When Textual is supported, the dashboard app is launched."""
        from bernstein.cli import run_cmd

        mock_caps = MagicMock()
        mock_caps.supports_textual = True

        monkeypatch.setattr(
            "bernstein.cli.terminal_caps.detect_capabilities",
            lambda: mock_caps,
        )

        mock_app_class = MagicMock()
        mock_app_instance = MagicMock()
        mock_app_instance._restart_on_exit = False
        mock_app_class.return_value = mock_app_instance

        monkeypatch.setattr(
            "bernstein.cli.dashboard.BernsteinApp",
            mock_app_class,
        )

        run_cmd._finalize_run_output(quiet=False)
        mock_app_instance.run.assert_called_once()

    def test_try_fallback_display_catches_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_try_fallback_display falls back to summary on error."""
        from bernstein.cli import run_cmd

        monkeypatch.setattr(
            "bernstein.tui.fallback.FallbackDisplay",
            MagicMock(side_effect=ImportError("no rich")),
        )
        mock_summary = MagicMock()
        monkeypatch.setattr(run_cmd, "_show_run_summary", mock_summary)

        run_cmd._try_fallback_display()
        mock_summary.assert_called_once()
