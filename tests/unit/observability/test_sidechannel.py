"""Unit and end-to-end tests for the portable side-channel telemetry module.

Coverage:

* DSN parsing: well-formed DSNs, store-url derivation, auth header shape,
  and every rejection path.
* Event rendering: required fields, default category tag, logger naming.
* Backpressure: ``drop`` discards the newest event on a full queue;
  ``queue`` blocks then gives up; the dropped counter tracks losses.
* Fail-closed boundary: a raising transport never propagates; an invalid
  DSN yields a NullSideChannel and emits nothing.
* End-to-end: emit a synthetic event and assert the (fake) backend
  received it via the Sentry store protocol body.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

import pytest

from bernstein.core.observability import sidechannel
from bernstein.core.observability.sidechannel import (
    Backpressure,
    Dsn,
    DsnError,
    EventLevel,
    NullSideChannel,
    SideChannel,
    SideChannelEvent,
    build_sidechannel,
    parse_dsn,
)

VALID_DSN = "https://abc123@errors.example.com/42"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingTransport:
    """Captures every rendered payload; reports configurable success."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.sent: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._delivered = threading.Event()

    def send(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            self.sent.append(dict(payload))
        self._delivered.set()
        return self.ok

    def wait(self, timeout: float = 2.0) -> bool:
        return self._delivered.wait(timeout)


class _RaisingTransport:
    """Always raises to exercise the fail-closed delivery path."""

    def send(self, payload: dict[str, Any]) -> bool:
        raise RuntimeError("backend exploded")


# ---------------------------------------------------------------------------
# DSN parsing
# ---------------------------------------------------------------------------


def test_parse_dsn_wellformed() -> None:
    dsn = parse_dsn(VALID_DSN)
    assert dsn == Dsn(
        scheme="https",
        public_key="abc123",
        host="errors.example.com",
        project_id="42",
        port=None,
    )


def test_parse_dsn_with_port() -> None:
    dsn = parse_dsn("http://key@localhost:8000/7")
    assert dsn.port == 8000
    assert dsn.netloc == "localhost:8000"
    assert dsn.store_url == "http://localhost:8000/api/7/store/"


def test_store_url_derivation() -> None:
    assert parse_dsn(VALID_DSN).store_url == "https://errors.example.com/api/42/store/"


def test_auth_header_shape() -> None:
    header = parse_dsn(VALID_DSN).auth_header(now=1700000000.0)
    assert header.startswith("Sentry ")
    assert "sentry_version=7" in header
    assert "sentry_key=abc123" in header
    assert "sentry_timestamp=1700000000" in header
    assert "sentry_client=bernstein-sidechannel/1" in header


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ftp://key@host/1",
        "https://host/1",  # no public key
        "https://key@/1",  # no host
        "https://key@host/",  # no project id
        "https://key@host",  # no project id
    ],
)
def test_parse_dsn_rejects_malformed(raw: str) -> None:
    with pytest.raises(DsnError):
        parse_dsn(raw)


# ---------------------------------------------------------------------------
# Event rendering
# ---------------------------------------------------------------------------


def test_event_payload_required_fields() -> None:
    event = SideChannelEvent(category="cost", message="budget exceeded", level=EventLevel.WARNING)
    payload = event.to_payload()
    assert payload["message"] == "budget exceeded"
    assert payload["level"] == "warning"
    assert payload["logger"] == "bernstein.cost"
    assert payload["platform"] == "python"
    assert payload["event_id"] == event.event_id
    assert payload["timestamp"] == event.timestamp


def test_event_payload_default_category_tag() -> None:
    payload = SideChannelEvent(category="run", message="started").to_payload()
    assert payload["tags"]["bernstein.category"] == "run"


def test_event_payload_preserves_explicit_tags_and_extra() -> None:
    event = SideChannelEvent(
        category="tracker",
        message="webhook delivered",
        tags={"tracker": "linear"},
        extra={"status": 200},
    )
    payload = event.to_payload()
    assert payload["tags"]["tracker"] == "linear"
    assert payload["tags"]["bernstein.category"] == "tracker"
    assert payload["extra"]["status"] == 200


# ---------------------------------------------------------------------------
# Build / configuration
# ---------------------------------------------------------------------------


def test_build_sidechannel_no_dsn_is_null() -> None:
    sink = build_sidechannel(env={})
    assert isinstance(sink, NullSideChannel)
    assert sink.enabled is False
    assert sink.emit(SideChannelEvent(category="x", message="y")) is False


def test_build_sidechannel_invalid_dsn_is_null() -> None:
    sink = build_sidechannel(env={sidechannel.DSN_ENV: "not-a-dsn"})
    assert isinstance(sink, NullSideChannel)


def test_build_sidechannel_valid_dsn_is_live() -> None:
    transport = _RecordingTransport()
    sink = build_sidechannel(env={sidechannel.DSN_ENV: VALID_DSN}, transport=transport)
    try:
        assert isinstance(sink, SideChannel)
        assert sink.enabled is True
    finally:
        sink.close()


def test_build_sidechannel_reads_backpressure_env() -> None:
    transport = _RecordingTransport()
    sink = build_sidechannel(
        env={sidechannel.DSN_ENV: VALID_DSN, sidechannel.BACKPRESSURE_ENV: "queue"},
        transport=transport,
    )
    try:
        assert isinstance(sink, SideChannel)
        assert sink._backpressure is Backpressure.QUEUE
    finally:
        sink.close()


# ---------------------------------------------------------------------------
# Backpressure
# ---------------------------------------------------------------------------


def _blocking_transport_sink(policy: Backpressure, maxsize: int) -> tuple[SideChannel, threading.Event]:
    """Build a sink whose transport blocks until released, to fill the queue."""
    release = threading.Event()

    class _Blocking:
        def send(self, payload: dict[str, Any]) -> bool:
            release.wait(2.0)
            return True

    sink = SideChannel(
        parse_dsn(VALID_DSN),
        backpressure=policy,
        maxsize=maxsize,
        transport=_Blocking(),
    )
    return sink, release


def test_drop_policy_discards_when_full() -> None:
    sink, release = _blocking_transport_sink(Backpressure.DROP, maxsize=1)
    try:
        # First event is pulled by the worker and blocks in transport.
        # Fill the single queue slot, then overflow it.
        accepted = [sink.emit(SideChannelEvent(category="c", message=str(i))) for i in range(8)]
        assert any(accepted)  # at least the first slots are accepted
        assert not all(accepted)  # overflow is dropped
        assert sink.dropped >= 1
    finally:
        release.set()
        sink.close()


def test_queue_policy_blocks_then_gives_up() -> None:
    sink, release = _blocking_transport_sink(Backpressure.QUEUE, maxsize=1)
    try:
        for i in range(3):
            sink.emit(SideChannelEvent(category="c", message=str(i)))
        # Fill the queue; the next emit blocks up to QUEUE_BLOCK_SECONDS then drops.
        start = time.monotonic()
        sink.emit(SideChannelEvent(category="c", message="overflow"))
        elapsed = time.monotonic() - start
        # Either it slotted in fast or it waited near the block budget; never raises.
        assert elapsed < sidechannel.QUEUE_BLOCK_SECONDS + 1.0
    finally:
        release.set()
        sink.close()


# ---------------------------------------------------------------------------
# Fail-closed boundary
# ---------------------------------------------------------------------------


def test_raising_transport_never_propagates() -> None:
    sink = SideChannel(parse_dsn(VALID_DSN), transport=_RaisingTransport())
    try:
        assert sink.emit(SideChannelEvent(category="c", message="m")) is True
        sink.flush()
        # Delivery raised internally but was swallowed; nothing counted as sent.
        assert sink.sent == 0
    finally:
        sink.close()


def test_emit_helper_uses_injected_sink() -> None:
    transport = _RecordingTransport()
    sink = SideChannel(parse_dsn(VALID_DSN), transport=transport)
    try:
        assert sidechannel.emit("run", "lifecycle", sink=sink) is True
    finally:
        sink.close()


# ---------------------------------------------------------------------------
# End-to-end: emit then assert the backend received it
# ---------------------------------------------------------------------------


def test_end_to_end_backend_receives_event() -> None:
    transport = _RecordingTransport()
    sink = build_sidechannel(env={sidechannel.DSN_ENV: VALID_DSN}, transport=transport)
    assert isinstance(sink, SideChannel)
    try:
        emitted = sidechannel.emit(
            "probe",
            "synthetic verification event",
            level=EventLevel.INFO,
            tags={"synthetic": "true"},
            extra={"probe": True},
            sink=sink,
        )
        assert emitted is True
        sink.flush()
        assert transport.wait(2.0), "background worker did not deliver the event"
    finally:
        sink.close()

    assert len(transport.sent) == 1
    received = transport.sent[0]
    assert received["message"] == "synthetic verification event"
    assert received["logger"] == "bernstein.probe"
    assert received["tags"]["synthetic"] == "true"
    assert received["tags"]["bernstein.category"] == "probe"
    assert received["extra"]["probe"] is True
    assert sink.sent == 1


def test_default_sink_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sidechannel.DSN_ENV, VALID_DSN)
    sidechannel.reset_sidechannel()
    try:
        sink = sidechannel.get_sidechannel()
        assert isinstance(sink, SideChannel)
        # Same instance returned across calls.
        assert sidechannel.get_sidechannel() is sink
    finally:
        sidechannel.reset_sidechannel()


def test_default_sink_null_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sidechannel.DSN_ENV, raising=False)
    sidechannel.reset_sidechannel()
    try:
        assert isinstance(sidechannel.get_sidechannel(), NullSideChannel)
    finally:
        sidechannel.reset_sidechannel()


def test_close_does_not_hang_on_full_queue() -> None:
    sink, release = _blocking_transport_sink(Backpressure.DROP, maxsize=1)
    # Fill the queue while the worker is blocked, then close: must return.
    for i in range(4):
        sink.emit(SideChannelEvent(category="c", message=str(i)))
    release.set()
    sink.close()  # should not raise or hang
    assert not sink._worker.is_alive()


def test_queue_full_exception_path_counts_drop() -> None:
    # Directly exercise the Full branch deterministically.
    sink = SideChannel(parse_dsn(VALID_DSN), transport=_RecordingTransport())
    try:
        sink._queue = queue.Queue(maxsize=1)
        sink._queue.put_nowait(SideChannelEvent(category="c", message="seed"))
        accepted = sink.emit(SideChannelEvent(category="c", message="overflow"))
        assert accepted is False
        assert sink.dropped == 1
    finally:
        sink.close()


# ---------------------------------------------------------------------------
# End-to-end against a real localhost HTTP backend (Sentry store protocol)
# ---------------------------------------------------------------------------


def test_end_to_end_real_http_backend_receives_event() -> None:
    """Drive the live HTTP transport against a localhost stand-in for the
    Sentry store endpoint.

    Any Sentry-compatible backend ingests the same store
    payload over HTTP. By spinning up a tiny ``http.server`` on a random
    local port and pointing a DSN at it, we exercise the entire pipeline
    (build sink, render payload, POST via httpx, parse store URL and auth
    header) without depending on a hosted backend in CI. The assertion
    below confirms what the backend would have received.
    """
    import http.server
    import socketserver
    import threading as _th

    received: list[dict[str, Any]] = []
    received_headers: list[dict[str, str]] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            try:
                import json as _json

                received.append(_json.loads(body.decode("utf-8")))
            except Exception:  # pragma: no cover - defensive
                received.append({"_raw": body.decode("utf-8", "replace")})
            received_headers.append({k: v for k, v in self.headers.items()})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args: object) -> None:
            # Silence the test server; it would otherwise spam stderr.
            return

    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as server:
        host = str(server.server_address[0])
        port = int(server.server_address[1])
        thread = _th.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            dsn = f"http://localkey@{host}:{port}/9"
            sink = build_sidechannel(env={sidechannel.DSN_ENV: dsn})
            assert isinstance(sink, SideChannel)
            try:
                accepted = sidechannel.emit(
                    "probe",
                    "live-http synthetic event",
                    level=EventLevel.INFO,
                    tags={"synthetic": "true"},
                    extra={"probe": True},
                    sink=sink,
                )
                assert accepted is True
                # Wait for the worker to drain (bounded).
                deadline = time.monotonic() + 5.0
                while not received and time.monotonic() < deadline:
                    time.sleep(0.02)
                sink.flush()
            finally:
                sink.close()
        finally:
            server.shutdown()
            thread.join(timeout=2.0)

    assert received, "live HTTP backend did not receive any event"
    payload = received[0]
    assert payload["message"] == "live-http synthetic event"
    assert payload["logger"] == "bernstein.probe"
    assert payload["platform"] == "python"
    assert payload["level"] == EventLevel.INFO.value
    assert payload["tags"]["bernstein.category"] == "probe"
    assert payload["tags"]["synthetic"] == "true"
    assert payload["extra"]["probe"] is True
    auth = received_headers[0].get("X-Sentry-Auth", "")
    assert auth.startswith("Sentry "), auth
    assert "sentry_key=localkey" in auth
    assert "sentry_version=7" in auth
