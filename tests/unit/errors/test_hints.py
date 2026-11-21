"""Unit tests for ``bernstein.core.errors.hints``.

We exercise:

- one snapshot test per category to lock the hint substring,
- border colour per category,
- the optional context fields (adapter, env var, port, etc.),
- verbose-mode toggle (traceback appears or stays hidden),
- and that ``hint_for`` is total for every enum member.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from rich.panel import Panel

from bernstein.core.errors import (
    ErrorCategory,
    HintContext,
    hint_for,
    render_hint,
)
from bernstein.core.errors.hints import _CATEGORY_BORDER  # type: ignore[attr-defined]


def _render(panel: Panel) -> str:
    """Render a Rich Panel to a plain string for substring assertions."""
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, color_system=None, width=120).print(panel)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Snapshot-style substring tests (one per category)
# ---------------------------------------------------------------------------


def test_hint_config_missing_mentions_bernstein_init() -> None:
    text = _render(hint_for(ErrorCategory.CONFIG_MISSING))
    assert "No bernstein config" in text
    assert "bernstein init" in text


def test_hint_auth_failed_mentions_adapter_and_env_var() -> None:
    ctx: HintContext = {"adapter": "claude", "env_var": "ANTHROPIC_API_KEY"}
    text = _render(hint_for(ErrorCategory.AUTH_FAILED, ctx))
    assert "claude" in text
    assert "ANTHROPIC_API_KEY" in text


def test_hint_auth_failed_falls_back_when_env_var_absent() -> None:
    ctx: HintContext = {"adapter": "codex"}
    text = _render(hint_for(ErrorCategory.AUTH_FAILED, ctx))
    assert "codex" in text
    assert "codex auth" in text


def test_hint_dependency_missing_mentions_install_command() -> None:
    ctx: HintContext = {"adapter": "aider", "package_manager_command": "pipx install aider"}
    text = _render(hint_for(ErrorCategory.DEPENDENCY_MISSING, ctx))
    assert "aider" in text
    assert "pipx install aider" in text


def test_hint_dependency_missing_works_without_command() -> None:
    text = _render(hint_for(ErrorCategory.DEPENDENCY_MISSING, {"adapter": "aider"}))
    assert "aider" in text
    assert "binary is not installed" in text


def test_hint_model_unreachable_mentions_provider_and_offline() -> None:
    text = _render(hint_for(ErrorCategory.MODEL_UNREACHABLE, {"provider": "anthropic"}))
    assert "anthropic" in text
    assert "bernstein doctor extended --provider anthropic" in text
    # BERNSTEIN_OFFLINE skips the doctor's probes; it never switched to the mock adapter.
    assert "BERNSTEIN_OFFLINE" not in text


def test_hint_timeout_includes_seconds_when_supplied() -> None:
    ctx: HintContext = {"adapter": "claude", "timeout_seconds": 30}
    text = _render(hint_for(ErrorCategory.TIMEOUT, ctx))
    assert "claude" in text
    assert "30s" in text


def test_hint_timeout_renders_without_seconds() -> None:
    text = _render(hint_for(ErrorCategory.TIMEOUT, {"adapter": "claude"}))
    assert "claude" in text
    assert "timed out" in text


def test_hint_permission_denied_mentions_path() -> None:
    text = _render(hint_for(ErrorCategory.PERMISSION_DENIED, {"path": "/srv/x"}))
    assert "/srv/x" in text
    assert "worktree" in text


def test_hint_port_conflict_mentions_lsof_and_port() -> None:
    text = _render(hint_for(ErrorCategory.PORT_CONFLICT, {"port": 9090}))
    assert "9090" in text
    assert "lsof -i :9090" in text


def test_hint_port_conflict_defaults_to_8052() -> None:
    text = _render(hint_for(ErrorCategory.PORT_CONFLICT))
    assert "8052" in text


def test_hint_unknown_mentions_verbose_and_issues_url() -> None:
    text = _render(hint_for(ErrorCategory.UNKNOWN))
    assert "Unhandled error" in text
    assert "github.com" in text


# ---------------------------------------------------------------------------
# Coverage: hint_for is total and never empty
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", list(ErrorCategory))
def test_hint_for_returns_panel_for_every_category(category: ErrorCategory) -> None:
    panel = hint_for(category)
    assert isinstance(panel, Panel)
    assert _render(panel).strip() != ""


@pytest.mark.parametrize("category", list(ErrorCategory))
def test_hint_for_uses_category_border(category: ErrorCategory) -> None:
    panel = hint_for(category)
    assert panel.border_style == _CATEGORY_BORDER[category]


@pytest.mark.parametrize("category", list(ErrorCategory))
def test_hint_for_has_nonempty_title(category: ErrorCategory) -> None:
    panel = hint_for(category)
    # ``Panel.title`` returns a TextType (may be str or rich.text.Text).
    assert panel.title is not None
    assert str(panel.title).strip() != ""


# ---------------------------------------------------------------------------
# Verbose-mode toggle
# ---------------------------------------------------------------------------


def _make_console() -> tuple[Console, io.StringIO]:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, color_system=None, width=120)
    return console, buf


def test_render_hint_default_hides_traceback() -> None:
    console, buf = _make_console()
    try:
        raise RuntimeError("kaboom")
    except RuntimeError as exc:
        render_hint(console, ErrorCategory.UNKNOWN, exc=exc, verbose=False)
    output = buf.getvalue()
    assert "Unhandled error" in output
    # The traceback frame label should not leak by default.
    assert "Traceback" not in output
    assert "kaboom" not in output or "RuntimeError" not in output


def test_render_hint_verbose_shows_traceback() -> None:
    console, buf = _make_console()
    try:
        raise RuntimeError("kaboom-verbose")
    except RuntimeError as exc:
        render_hint(console, ErrorCategory.UNKNOWN, exc=exc, verbose=True)
    output = buf.getvalue()
    assert "Unhandled error" in output
    # Rich's Traceback frame names include the exception type.
    assert "RuntimeError" in output
    assert "kaboom-verbose" in output


def test_render_hint_verbose_without_exception_is_safe() -> None:
    console, buf = _make_console()
    render_hint(console, ErrorCategory.PORT_CONFLICT, verbose=True)
    output = buf.getvalue()
    assert "Port" in output
