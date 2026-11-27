"""Integration tests for the ``bernstein preview`` lifecycle.

End-to-end: a fake dev-server (a real Python TCP listener) plus a fake
tunnel provider (an in-memory :class:`TunnelProvider`) wired through
the real :class:`PreviewManager`. The test drives the manager's full
start → list → stop → audit-verify path the way the CLI does.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import pytest

from bernstein.core.preview.manager import (
    AuthMode,
    DevServerHandle,
    DevServerRunner,
    PreviewManager,
    PreviewStore,
)
from bernstein.core.preview.token_issuer import PreviewTokenIssuer
from bernstein.core.preview.tunnel_bridge import TunnelBridge
from bernstein.core.security.audit import AuditLog
from bernstein.core.tunnels.protocol import (
    TunnelHandle,
    TunnelProvider,
)
from bernstein.core.tunnels.registry import TunnelRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _LocalServer:
    """A real socket bound to 127.0.0.1 so the manager's TCP probe can succeed."""

    port: int
    sock: socket.socket
    accept_thread: threading.Thread

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()


@pytest.fixture
def local_server() -> Iterator[_LocalServer]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def _accept_loop() -> None:
        sock.settimeout(0.2)
        while True:
            try:
                conn, _ = sock.accept()
            except (TimeoutError, OSError):
                if sock.fileno() == -1:
                    return
                continue
            else:
                conn.close()

    thread = threading.Thread(target=_accept_loop, daemon=True)
    thread.start()
    server = _LocalServer(port=port, sock=sock, accept_thread=thread)
    try:
        yield server
    finally:
        server.close()


@pytest.fixture(autouse=True)
def recorded_signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Intercept ``os.kill`` / ``os.killpg`` so preview tests never emit real signals."""
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append(("kill", pid, int(sig))))
    if hasattr(os, "killpg"):
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, int(sig))))
    return calls


def _patch_psutil_create_time(
    monkeypatch: pytest.MonkeyPatch,
    resolver: Any,
) -> None:
    """Patch ``psutil.Process(pid)`` while passing ``psutil.Process()`` through for conftest."""
    real_process = psutil.Process

    class _FakeProc:
        def __init__(self, pid: int) -> None:
            self._pid = pid

        def create_time(self) -> float:
            return float(resolver(self._pid))

    def _factory(pid: int | None = None) -> Any:
        if pid is None:
            return real_process()
        return _FakeProc(pid)

    monkeypatch.setattr(psutil, "Process", _factory)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _StaticRunner(DevServerRunner):
    """Returns a pre-recorded set of stdout lines."""

    def __init__(self, lines: list[str], pid: int = 9999) -> None:
        self._lines = lines
        self._pid = pid
        self.terminated: list[int] = []

    def spawn(self, *, command: str | None, cwd: Path) -> DevServerHandle:
        return DevServerHandle(pid=self._pid, process=object(), stdout_lines=iter(self._lines))

    def terminate(self, handle: DevServerHandle) -> None:
        self.terminated.append(handle.pid)


class _InMemoryProvider(TunnelProvider):
    """Tunnel provider that simulates cloudflared without spawning a binary."""

    def __init__(self, name: str = "cloudflared") -> None:
        self.name = name
        self.binary = name
        self.started: list[str] = []
        self.stopped: list[str] = []

    def detect(self) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    def start(self, port: int, name: str) -> TunnelHandle:
        self.started.append(name)
        return TunnelHandle(
            name=name,
            provider=self.name,
            port=port,
            public_url=f"https://{name}.example.com",
            pid=0,
        )

    def stop(self, name: str) -> None:
        self.stopped.append(name)


@dataclass
class _FakeSandbox:
    backend_name: str = "worktree"
    session_id: str = "sbx-int"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_full_lifecycle_with_real_socket(
    tmp_path: Path,
    local_server: _LocalServer,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start → list → stop, with audit-chain verification at the end."""
    # Project file the discovery layer can pick up.
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8")

    # Deterministic process metadata for the fake runner PID (9999) so the test
    # never probes a host process that happens to hold PID 9999.
    def _missing(pid: int) -> float:
        raise psutil.NoSuchProcess(pid)

    _patch_psutil_create_time(monkeypatch, _missing)

    # The fake dev server reports the port that the actual local socket
    # is bound to, so the TCP probe in PreviewManager.start succeeds.
    runner = _StaticRunner([f"Local:   http://localhost:{local_server.port}/"])

    provider = _InMemoryProvider()
    state_path = tmp_path / "tunnels.json"

    def _factory() -> TunnelRegistry:
        reg = TunnelRegistry(state_path=state_path)
        reg.register(provider)
        return reg

    audit = AuditLog(tmp_path / "audit", key=b"\xfa" * 32)
    store = PreviewStore(path=tmp_path / "preview.json")
    manager = PreviewManager(
        store=store,
        tunnel=TunnelBridge(registry_factory=_factory),
        token_issuer=PreviewTokenIssuer(secret="x" * 64),
        audit_log=audit,
        runner=runner,
    )

    preview = manager.start(
        cwd=tmp_path,
        sandbox_session=_FakeSandbox(),
        approve_discovered=True,
        provider="cloudflared",
        auth_mode=AuthMode.TOKEN,
        expire_seconds="30m",
        port_probe_timeout=5.0,
    )
    assert preview.state.port == local_server.port
    assert preview.state.tunnel_provider == "cloudflared"
    assert preview.state.share_url.startswith("https://")
    assert "token=" in preview.state.share_url

    # `list` reflects what was persisted.
    listed = manager.list()
    assert [s.preview_id for s in listed] == [preview.state.preview_id]

    # Stop tears the tunnel down and removes the record without sending a real signal.
    assert manager.stop(preview.state.preview_id) is True
    assert manager.list() == []
    assert provider.stopped == [preview.state.tunnel_name]
    assert recorded_signals == []

    # Audit chain stays intact across all three events.
    valid, errors = audit.verify()
    assert valid, errors
    raw_events = [
        json.loads(line)
        for f in (tmp_path / "audit").glob("*.jsonl")
        for line in f.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    event_types = [e["event_type"] for e in raw_events]
    # We expect at least: start, link, stop.
    assert event_types.count("preview.start") >= 1
    assert event_types.count("preview.link") >= 1
    assert event_types.count("preview.stop") >= 1


def _write_persisted_preview(
    store_path: Path,
    *,
    preview_id: str,
    process_pid: Any,
    process_start_time: Any = ...,
) -> None:
    row: dict[str, Any] = {
        "preview_id": preview_id,
        "command": "npm run dev",
        "cwd": str(store_path.parent),
        "port": 5173,
        "sandbox_backend": "worktree",
        "sandbox_session_id": "sbx-int",
        "tunnel_provider": "cloudflared",
        "tunnel_name": preview_id,
        "public_url": f"https://{preview_id}.example.com",
        "share_url": f"https://{preview_id}.example.com",
        "auth_mode": "none",
        "expires_at_epoch": 9_999_999_999.0,
        "process_pid": process_pid,
        "created_at_epoch": 1_700_000_000.0,
    }
    if process_start_time is not ...:
        row["process_start_time"] = process_start_time
    store_path.write_text(json.dumps({"previews": [row]}, indent=2) + "\n", encoding="utf-8")


def _make_manager(
    tmp_path: Path, *, runner: DevServerRunner | None = None
) -> tuple[PreviewManager, PreviewStore, AuditLog]:
    provider = _InMemoryProvider()
    state_path = tmp_path / "tunnels.json"

    def _factory() -> TunnelRegistry:
        reg = TunnelRegistry(state_path=state_path)
        reg.register(provider)
        return reg

    audit = AuditLog(tmp_path / "audit", key=b"\xfb" * 32)
    store = PreviewStore(path=tmp_path / "preview.json")
    manager = PreviewManager(
        store=store,
        tunnel=TunnelBridge(registry_factory=_factory),
        token_issuer=PreviewTokenIssuer(secret="y" * 64),
        audit_log=audit,
        runner=runner,
    )
    return manager, store, audit


def _read_stop_audit_details(audit_dir: Path) -> list[dict[str, Any]]:
    events = [
        json.loads(line)
        for f in sorted(audit_dir.glob("*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [e["details"] for e in events if e["event_type"] == "preview.stop"]


def test_stop_skips_stale_pid_with_different_start_time(
    tmp_path: Path,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reused PID whose live create_time differs must receive no signal."""
    manager, store, _ = _make_manager(tmp_path)
    _write_persisted_preview(
        store.path,
        preview_id="prv-stale",
        process_pid=42424,
        process_start_time=1_700_000_000.0,
    )
    if hasattr(os, "getpgid"):
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    _patch_psutil_create_time(monkeypatch, lambda _pid: 1_700_000_500.0)

    with caplog.at_level(logging.WARNING, logger="bernstein.core.preview.manager"):
        assert manager.stop("prv-stale") is True

    assert recorded_signals == []
    assert store.get("prv-stale") is None
    assert "42424" in caplog.text and "start_time_mismatch" in caplog.text
    details = _read_stop_audit_details(tmp_path / "audit")[-1]
    assert details["process_signal_status"] == "skipped"
    assert details["process_signal_reason"] == "start_time_mismatch"


def test_stop_refuses_pid_1(
    tmp_path: Path,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """PID 1 must never be signaled even if persisted with a start time."""
    manager, store, _ = _make_manager(tmp_path)
    _write_persisted_preview(
        store.path,
        preview_id="prv-pid1",
        process_pid=1,
        process_start_time=1_700_000_000.0,
    )
    if hasattr(os, "getpgid"):
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    with caplog.at_level(logging.WARNING, logger="bernstein.core.preview.manager"):
        assert manager.stop("prv-pid1") is True

    assert recorded_signals == []
    assert store.get("prv-pid1") is None
    assert "invalid_pid" in caplog.text
    details = _read_stop_audit_details(tmp_path / "audit")[-1]
    assert details["process_signal_status"] == "skipped"
    assert details["process_signal_reason"] == "invalid_pid"


def test_stop_refuses_own_process_group(
    tmp_path: Path,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Neither our own pgid nor any PID sharing our process group may be signaled."""
    if not (hasattr(os, "getpgrp") and hasattr(os, "getpgid")):
        pytest.skip("POSIX process groups not available on this platform")

    own_pgid = os.getpgrp()
    peer_pid = max(own_pgid, os.getpid()) + 10_000
    manager, store, _ = _make_manager(tmp_path)
    _patch_psutil_create_time(monkeypatch, lambda _pid: 1_700_000_000.0)
    monkeypatch.setattr(os, "getpgid", lambda pid: own_pgid)

    _write_persisted_preview(
        store.path,
        preview_id="prv-own-pgid",
        process_pid=peer_pid,
        process_start_time=1_700_000_000.0,
    )
    with caplog.at_level(logging.WARNING, logger="bernstein.core.preview.manager"):
        assert manager.stop("prv-own-pgid") is True

    assert recorded_signals == []
    assert store.get("prv-own-pgid") is None
    assert "own_process_group" in caplog.text
    details = _read_stop_audit_details(tmp_path / "audit")[-1]
    assert details["process_signal_status"] == "skipped"
    assert details["process_signal_reason"] == "own_process_group"


def test_stop_legacy_state_without_start_time_skips_signal_and_cleans_up(
    tmp_path: Path,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Legacy state files lacking process_start_time send no signal and clean up state."""
    manager, store, _ = _make_manager(tmp_path)
    _write_persisted_preview(
        store.path,
        preview_id="prv-legacy",
        process_pid=42426,
    )
    if hasattr(os, "getpgid"):
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    _patch_psutil_create_time(monkeypatch, lambda _pid: 1_700_000_000.0)

    with caplog.at_level(logging.WARNING, logger="bernstein.core.preview.manager"):
        assert manager.stop("prv-legacy") is True

    assert recorded_signals == []
    assert store.get("prv-legacy") is None
    assert "42426" in caplog.text and "missing_start_time" in caplog.text
    details = _read_stop_audit_details(tmp_path / "audit")[-1]
    assert details["process_signal_status"] == "skipped"
    assert details["process_signal_reason"] == "missing_start_time"


def test_stop_signals_matching_start_time(
    tmp_path: Path,
    local_server: _LocalServer,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start records psutil create_time and stop signals when create_time matches."""
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8")
    target_pid = max(os.getpid(), os.getpgrp() if hasattr(os, "getpgrp") else 0) + 20_000
    expected_create_time = 1_700_000_123.25

    def _resolve(pid: int) -> float:
        if pid != target_pid:
            raise psutil.NoSuchProcess(pid)
        return expected_create_time

    _patch_psutil_create_time(monkeypatch, _resolve)
    if hasattr(os, "getpgid"):
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    runner = _StaticRunner([f"Local:   http://localhost:{local_server.port}/"], pid=target_pid)
    manager, store, _ = _make_manager(tmp_path, runner=runner)

    preview = manager.start(
        cwd=tmp_path,
        sandbox_session=_FakeSandbox(),
        approve_discovered=True,
        provider="cloudflared",
        auth_mode=AuthMode.TOKEN,
        expire_seconds="30m",
        port_probe_timeout=5.0,
    )
    assert preview.state.process_start_time == pytest.approx(expected_create_time)
    persisted = json.loads(store.path.read_text(encoding="utf-8"))["previews"][0]
    assert persisted["process_start_time"] == pytest.approx(expected_create_time)

    assert manager.stop(preview.state.preview_id) is True
    assert store.get(preview.state.preview_id) is None
    if hasattr(os, "killpg"):
        assert recorded_signals == [("killpg", target_pid, int(signal.SIGTERM))]
    else:
        assert recorded_signals == [("kill", target_pid, int(signal.SIGTERM))]
    details = _read_stop_audit_details(tmp_path / "audit")[-1]
    assert details["process_signal_status"] == "signaled"
    assert details["process_signal_reason"] == "signaled"
