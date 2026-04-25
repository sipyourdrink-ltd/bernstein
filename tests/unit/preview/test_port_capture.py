"""Unit tests for :mod:`bernstein.core.preview.port_capture`."""

from __future__ import annotations

from bernstein.core.preview.port_capture import (
    capture_port,
    probe_port,
)


def test_capture_port_localhost_match() -> None:
    """``localhost:<port>`` is the canonical happy path."""
    assert capture_port(["VITE v5.0.0  ready in 542 ms", "Local:   http://localhost:5173/"]) == 5173


def test_capture_port_listening_on_match() -> None:
    """``Listening on <port>`` style log lines are matched."""
    assert capture_port(["[server] Listening on 4000"]) == 4000


def test_capture_port_127_match() -> None:
    """``127.0.0.1:<port>`` is matched even without a hostname."""
    assert capture_port(["bound to 127.0.0.1:8000"]) == 8000


def test_capture_port_returns_none_when_no_match() -> None:
    """No match means ``None``, not an exception."""
    assert capture_port(["nothing useful here"]) is None


def test_capture_port_first_match_wins() -> None:
    """The first matching line short-circuits the iterator."""
    lines = ["[stub] localhost:1234", "later port=9999"]
    assert capture_port(lines) == 1234


def test_probe_port_times_out_when_nothing_listens() -> None:
    """A short timeout against an unbound port returns ``False``."""
    fake_now = [0.0]

    def clock() -> float:
        fake_now[0] += 0.5
        return fake_now[0]

    sleeps: list[float] = []

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        fake_now[0] += seconds

    # Use a port we are very unlikely to have anything bound on.
    ok = probe_port(
        65000,
        host="127.0.0.1",
        timeout_seconds=1.0,
        sleeper=sleeper,
        clock=clock,
    )
    assert ok is False
    # We slept at least once and respected the budget.
    assert sleeps, "probe_port should have slept between attempts"


def test_probe_port_succeeds_against_live_socket() -> None:
    """A real listening socket gets a green light immediately."""
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert probe_port(port, host="127.0.0.1", timeout_seconds=2.0) is True
    finally:
        sock.close()


def test_probe_port_invalid_port_returns_false() -> None:
    """Out-of-range ports are rejected without dialling."""
    assert probe_port(0, timeout_seconds=0.1) is False
    assert probe_port(70000, timeout_seconds=0.1) is False
