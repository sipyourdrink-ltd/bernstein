"""Integration tests for the ``bernstein preview`` lifecycle.

End-to-end: a fake dev-server (a real Python TCP listener) plus a fake
tunnel provider (an in-memory :class:`TunnelProvider`) wired through
the real :class:`PreviewManager`. The test drives the manager's full
start → list → stop → audit-verify path the way the CLI does.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def test_full_lifecycle_with_real_socket(tmp_path: Path, local_server: _LocalServer) -> None:
    """Start → list → stop, with audit-chain verification at the end."""
    # Project file the discovery layer can pick up.
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8"
    )

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

    # Stop tears the tunnel down and removes the record.
    assert manager.stop(preview.state.preview_id) is True
    assert manager.list() == []
    assert provider.stopped == [preview.state.tunnel_name]

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
