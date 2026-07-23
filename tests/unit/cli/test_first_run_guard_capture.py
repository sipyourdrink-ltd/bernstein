"""Tests for explicit ``sentry_sdk.capture_exception`` wiring in the CLI barrier.

The first-run guard converts top-level exceptions into a Rich hint panel
plus a ``SystemExit``. Because the conversion swallows the original
exception, the default ``sys.excepthook`` never fires for these paths
and the configured Sentry-compatible sink would otherwise
miss every CLI crash.

These tests pin the explicit ``sentry_sdk.capture_exception`` call so
the wire-up cannot regress silently.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import click
import pytest

from bernstein.cli import first_run_guard


class _FakeClient:
    """Stand-in for ``sentry_sdk.get_client()``.

    Exposes ``is_active`` so the guard can short-circuit when no DSN
    was configured.
    """

    def __init__(self, *, active: bool) -> None:
        self._active = active

    def is_active(self) -> bool:
        return self._active


def _install_fake_sentry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active: bool,
) -> SimpleNamespace:
    """Install a fake ``sentry_sdk`` module and return a recorder.

    The recorder counts ``capture_exception`` invocations and stores
    each captured exception so tests can assert on them.
    """
    captured: list[BaseException] = []

    def _capture(exc: BaseException) -> None:
        captured.append(exc)

    fake = ModuleType("sentry_sdk")
    fake.get_client = lambda: _FakeClient(active=active)  # type: ignore[attr-defined]
    fake.capture_exception = _capture  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    return SimpleNamespace(captured=captured)


def test_capture_exception_called_once_on_unhandled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unhandled CLI exception triggers exactly one capture call."""
    recorder = _install_fake_sentry(monkeypatch, active=True)

    boom = RuntimeError("simulated CLI failure")

    # Silence the Rich hint render so the test stays quiet; the only
    # behaviour under test here is the capture call.
    monkeypatch.setattr(
        first_run_guard,
        "render_hint",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(SystemExit):
        first_run_guard.handle_first_run_exception(boom)

    assert len(recorder.captured) == 1
    assert recorder.captured[0] is boom


def test_capture_skipped_when_sdk_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No capture call when the SDK is installed but never initialised."""
    recorder = _install_fake_sentry(monkeypatch, active=False)

    monkeypatch.setattr(
        first_run_guard,
        "render_hint",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(SystemExit):
        first_run_guard.handle_first_run_exception(RuntimeError("boom"))

    assert recorder.captured == []


def test_capture_skipped_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is a no-op when ``sentry-sdk`` is not importable."""
    # Force ``import sentry_sdk`` to raise ImportError.
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)

    monkeypatch.setattr(
        first_run_guard,
        "render_hint",
        lambda *args, **kwargs: None,
    )

    # Must not raise anything other than the expected SystemExit.
    with pytest.raises(SystemExit):
        first_run_guard.handle_first_run_exception(RuntimeError("boom"))


def test_capture_failures_do_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misbehaving SDK must not block the CLI exit path."""

    def _explode(_: BaseException) -> None:
        raise RuntimeError("sentry sdk is on fire")

    fake = ModuleType("sentry_sdk")
    fake.get_client = lambda: _FakeClient(active=True)  # type: ignore[attr-defined]
    fake.capture_exception = _explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)

    monkeypatch.setattr(
        first_run_guard,
        "render_hint",
        lambda *args, **kwargs: None,
    )

    # The capture explosion is swallowed; SystemExit still bubbles up
    # so the categorised exit code reaches the operator.
    with pytest.raises(SystemExit):
        first_run_guard.handle_first_run_exception(RuntimeError("boom"))


def test_usage_error_skips_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """``click.UsageError`` is re-raised before any capture happens."""
    recorder = _install_fake_sentry(monkeypatch, active=True)

    monkeypatch.setattr(
        first_run_guard,
        "render_hint",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(click.UsageError):
        first_run_guard.handle_first_run_exception(click.UsageError("nope"))

    assert recorder.captured == []


def test_system_exit_skips_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ``SystemExit`` from inner code is honoured verbatim."""
    recorder = _install_fake_sentry(monkeypatch, active=True)

    monkeypatch.setattr(
        first_run_guard,
        "render_hint",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(SystemExit):
        first_run_guard.handle_first_run_exception(SystemExit(42))

    assert recorder.captured == []
