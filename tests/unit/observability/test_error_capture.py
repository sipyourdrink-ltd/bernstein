"""Tests for the shared error-capture routing helper.

The helper forwards genuine, unexpected failure signals (dead-letter
enqueues, agent crashes, autofix daemon faults) to the operator-managed
error sink. It routes through two transports that share the single
``BERNSTEIN_TELEMETRY_DSN`` contract:

* ``sentry-sdk`` when the SDK has been initialised (the idiomatic
  ``sentry_sdk.get_client().is_active()`` guard), so events get the SDK's
  grouping and release tagging; and
* the dependency-free side channel (Sentry store protocol over httpx),
  so the signal still lands in worker subprocesses that never ran the
  CLI's SDK ``init`` and in minimal installs without the SDK.

Both transports are best-effort: nothing raised inside either ever
propagates to the caller, because the failure path that calls this must
never be made worse by telemetry.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from bernstein.core.observability import error_capture, sidechannel


class _FakeClient:
    """Stand-in for ``sentry_sdk.get_client()`` with an ``is_active`` flag."""

    def __init__(self, *, active: bool) -> None:
        self._active = active

    def is_active(self) -> bool:
        return self._active


def _install_fake_sentry(
    monkeypatch: pytest.MonkeyPatch,
    *,
    active: bool,
) -> SimpleNamespace:
    """Install a fake ``sentry_sdk`` and return a recorder of calls."""
    captured_exc: list[BaseException] = []
    captured_msg: list[tuple[str, str]] = []

    def _capture_exc(exc: BaseException) -> None:
        captured_exc.append(exc)

    def _capture_msg(message: str, level: str = "error") -> None:
        captured_msg.append((message, level))

    fake = ModuleType("sentry_sdk")
    fake.get_client = lambda: _FakeClient(active=active)  # type: ignore[attr-defined]
    fake.capture_exception = _capture_exc  # type: ignore[attr-defined]
    fake.capture_message = _capture_msg  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    return SimpleNamespace(exc=captured_exc, msg=captured_msg)


def _install_recording_sidechannel(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Patch ``sidechannel.emit`` to record calls instead of touching the network."""
    calls: list[dict[str, object]] = []

    def _emit(
        category: str,
        message: str,
        *,
        level: sidechannel.EventLevel = sidechannel.EventLevel.INFO,
        tags: dict[str, str] | None = None,
        extra: dict[str, object] | None = None,
        sink: object | None = None,
    ) -> bool:
        calls.append(
            {
                "category": category,
                "message": message,
                "level": level,
                "tags": dict(tags or {}),
                "extra": dict(extra or {}),
            }
        )
        return True

    monkeypatch.setattr(error_capture.sidechannel, "emit", _emit)
    return calls


# ---------------------------------------------------------------------------
# capture_exception
# ---------------------------------------------------------------------------


def test_capture_exception_routes_to_sdk_and_sidechannel(monkeypatch: pytest.MonkeyPatch) -> None:
    """An active SDK plus a wired side channel both receive the exception."""
    recorder = _install_fake_sentry(monkeypatch, active=True)
    calls = _install_recording_sidechannel(monkeypatch)

    boom = RuntimeError("agent crashed")
    error_capture.capture_exception(
        boom,
        category="agent",
        tags={"session_id": "abc"},
        extra={"abort_reason": "oom"},
    )

    # SDK path fired exactly once with the original exception object.
    assert recorder.exc == [boom]
    # Side-channel mirror fired once, at error level, carrying the message
    # and the structured context.
    assert len(calls) == 1
    assert calls[0]["category"] == "agent"
    assert calls[0]["level"] == sidechannel.EventLevel.ERROR
    assert "agent crashed" in str(calls[0]["message"])
    assert calls[0]["tags"]["session_id"] == "abc"
    assert calls[0]["extra"]["abort_reason"] == "oom"


def test_capture_exception_sidechannel_still_fires_when_sdk_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the SDK is not initialised, the side channel still carries the signal.

    This is the worker-subprocess case: the CLI ``sentry_sdk.init`` never ran,
    but the DSN is exported, so the dependency-free side channel must still
    deliver the event. This is the gap that left the error sink empty.
    """
    recorder = _install_fake_sentry(monkeypatch, active=False)
    calls = _install_recording_sidechannel(monkeypatch)

    error_capture.capture_exception(ValueError("dlq entry"), category="dead_letter")

    assert recorder.exc == []  # SDK skipped: not active
    assert len(calls) == 1  # side channel still fired
    assert calls[0]["category"] == "dead_letter"


def test_capture_exception_works_without_sentry_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """A minimal install without ``sentry-sdk`` still routes via the side channel."""
    # Force ``import sentry_sdk`` to raise ImportError.
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)
    calls = _install_recording_sidechannel(monkeypatch)

    error_capture.capture_exception(RuntimeError("boom"), category="autofix")

    assert len(calls) == 1
    assert calls[0]["category"] == "autofix"


def test_capture_exception_swallows_sdk_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misbehaving SDK must not break the failure path the helper sits on."""

    def _explode(_: BaseException) -> None:
        raise RuntimeError("sentry is on fire")

    fake = ModuleType("sentry_sdk")
    fake.get_client = lambda: _FakeClient(active=True)  # type: ignore[attr-defined]
    fake.capture_exception = _explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    calls = _install_recording_sidechannel(monkeypatch)

    # Must not raise. The side channel still gets its turn.
    error_capture.capture_exception(RuntimeError("boom"), category="dead_letter")
    assert len(calls) == 1


def test_capture_exception_swallows_sidechannel_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising side channel must not propagate out of the helper."""
    _install_fake_sentry(monkeypatch, active=True)

    def _boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("sidechannel exploded")

    monkeypatch.setattr(error_capture.sidechannel, "emit", _boom)

    # No exception escapes.
    error_capture.capture_exception(RuntimeError("boom"), category="agent")


# ---------------------------------------------------------------------------
# capture_message
# ---------------------------------------------------------------------------


def test_capture_message_routes_to_sdk_and_sidechannel(monkeypatch: pytest.MonkeyPatch) -> None:
    """A message routes to the SDK (with level) and the side channel."""
    recorder = _install_fake_sentry(monkeypatch, active=True)
    calls = _install_recording_sidechannel(monkeypatch)

    error_capture.capture_message(
        "task moved to dead-letter queue",
        category="dead_letter",
        level=sidechannel.EventLevel.FATAL,
        tags={"task_id": "T-1"},
    )

    assert recorder.msg == [("task moved to dead-letter queue", "fatal")]
    assert len(calls) == 1
    assert calls[0]["level"] == sidechannel.EventLevel.FATAL
    assert calls[0]["tags"]["task_id"] == "T-1"


def test_capture_message_defaults_to_error_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default level is ERROR when not specified."""
    _install_fake_sentry(monkeypatch, active=False)
    calls = _install_recording_sidechannel(monkeypatch)

    error_capture.capture_message("autofix dispatch raised", category="autofix")

    assert len(calls) == 1
    assert calls[0]["level"] == sidechannel.EventLevel.ERROR
