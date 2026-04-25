"""Unit tests for :class:`bernstein.core.preview.manager.PreviewManager`.

Covers:

* Discovery → port detection → tunnel → audit → metrics happy path.
* Tunnel-failure rollback (the dev-server is terminated and no record
  is persisted).
* Expiry enforcement via :meth:`PreviewManager.reap_expired`.
* HMAC audit-chain integrity (``preview.start`` plus ``preview.link``
  plus ``preview.stop`` chains correctly).
* ``parse_duration`` happy path + error path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from bernstein.core.preview.manager import (
    AuthMode,
    DevServerHandle,
    DevServerRunner,
    PreviewError,
    PreviewManager,
    PreviewState,
    PreviewStore,
    parse_duration,
)
from bernstein.core.preview.token_issuer import IssuedAuth, PreviewTokenIssuer
from bernstein.core.preview.tunnel_bridge import TunnelBridge, TunnelBridgeError
from bernstein.core.security.audit import AuditLog
from bernstein.core.tunnels.protocol import TunnelHandle

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRunner(DevServerRunner):
    """In-memory dev-server runner that yields canned stdout lines."""

    def __init__(self, lines: list[str], *, pid: int = 1234) -> None:
        self._lines = lines
        self._pid = pid
        self.terminated: list[int] = []

    def spawn(self, *, command: str | None, cwd: Path) -> DevServerHandle:
        return DevServerHandle(pid=self._pid, process=object(), stdout_lines=iter(self._lines))

    def terminate(self, handle: DevServerHandle) -> None:
        self.terminated.append(handle.pid)


class _FakeTunnel(TunnelBridge):
    """Tunnel bridge that returns a deterministic handle (or fails)."""

    def __init__(self, *, fail: bool = False) -> None:
        # Skip the parent constructor — we override every method.
        self.opened: list[tuple[int, str | None, str]] = []
        self.closed: list[str] = []
        self._fail = fail

    def open(self, *, port: int, provider: str = "auto", name: str | None = None) -> TunnelHandle:
        self.opened.append((port, name, provider))
        if self._fail:
            raise TunnelBridgeError("simulated tunnel failure")
        return TunnelHandle(
            name=name or "auto-generated",
            provider="cloudflared",
            port=port,
            public_url="https://abc.trycloudflare.com",
            pid=0,
        )

    def close(self, name: str) -> bool:
        self.closed.append(name)
        return True

    def list(self) -> list[TunnelHandle]:
        return []


@dataclass
class _FakeSandbox:
    backend_name: str = "worktree"
    session_id: str = "sbx-test"


class _FakeIssuer(PreviewTokenIssuer):
    def __init__(self) -> None:
        super().__init__(secret="x" * 32)
        self.calls: list[tuple[str, str, int]] = []

    def issue(
        self,
        *,
        preview_id: str,
        mode: str,
        expires_in_seconds: int,
        scopes: tuple[str, ...] = ("preview:read",),
    ) -> IssuedAuth:
        self.calls.append((preview_id, mode, expires_in_seconds))
        if mode == "none":
            return IssuedAuth(mode="none", expires_at_epoch=0.0)
        if mode == "token":
            return IssuedAuth(
                mode="token",
                token="signed-jwt-token",
                expires_at_epoch=999_999.0,
            )
        return IssuedAuth(
            mode="basic",
            basic_user="preview",
            basic_password="strong",
            expires_at_epoch=999_999.0,
        )


def _audit_log(tmp_path: Path) -> AuditLog:
    """Build an :class:`AuditLog` with a deterministic key."""
    return AuditLog(tmp_path / "audit", key=b"\x00" * 32)


# ---------------------------------------------------------------------------
# parse_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("30m", 1800),
        ("4h", 14_400),
        ("1d", 86_400),
        ("60", 60),
        (3600, 3600),
        (None, 14_400),
    ],
)
def test_parse_duration_happy_path(spec: object, expected: int) -> None:
    assert parse_duration(spec) == expected


@pytest.mark.parametrize("spec", ["", "abc", "-1m", "0", "0s"])
def test_parse_duration_rejects_invalid(spec: str) -> None:
    if spec == "":
        # empty string falls back to default
        assert parse_duration(spec) == 14_400
        return
    with pytest.raises(ValueError):
        parse_duration(spec)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_start_persists_state_and_writes_audit_chain(tmp_path: Path) -> None:
    """Successful start emits 2 audit entries (start + link) and persists state."""
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8"
    )
    runner = _FakeRunner(["VITE ready", "Local:   http://localhost:5173/"])
    audit = _audit_log(tmp_path)
    store = PreviewStore(path=tmp_path / "preview.json")

    # Replace probe_port with an instant success — we don't want the
    # test to actually open a TCP socket.
    import bernstein.core.preview.manager as mgr_mod

    real_probe = mgr_mod.probe_port
    mgr_mod.probe_port = lambda *args, **kwargs: True  # type: ignore[assignment]
    try:
        manager = PreviewManager(
            store=store,
            tunnel=_FakeTunnel(),
            token_issuer=_FakeIssuer(),
            audit_log=audit,
            runner=runner,
        )
        preview = manager.start(
            cwd=tmp_path,
            sandbox_session=_FakeSandbox(),
            auth_mode=AuthMode.TOKEN,
            expire_seconds="1h",
        )
    finally:
        mgr_mod.probe_port = real_probe  # type: ignore[assignment]

    state = preview.state
    assert state.port == 5173
    assert state.tunnel_provider == "cloudflared"
    assert state.public_url == "https://abc.trycloudflare.com"
    assert state.share_url.startswith("https://abc.trycloudflare.com/")
    assert "token=signed-jwt-token" in state.share_url
    assert state.auth_mode == "token"
    assert state.command == "npm run dev"

    # Persistence
    assert store.get(state.preview_id) == state

    # Audit chain integrity
    valid, errors = audit.verify()
    assert valid, errors
    log_files = list((tmp_path / "audit").glob("*.jsonl"))
    assert log_files, "audit log should have produced at least one daily file"
    raw_lines = [
        json.loads(line)
        for f in log_files
        for line in f.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    event_types = [r["event_type"] for r in raw_lines]
    assert "preview.start" in event_types
    assert "preview.link" in event_types


def test_stop_removes_record_and_audits(tmp_path: Path) -> None:
    """``stop`` tears the tunnel down, deletes state, audits ``preview.stop``."""
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8"
    )
    runner = _FakeRunner(["http://localhost:1234"])
    tunnel = _FakeTunnel()
    audit = _audit_log(tmp_path)
    store = PreviewStore(path=tmp_path / "preview.json")

    import bernstein.core.preview.manager as mgr_mod

    real_probe = mgr_mod.probe_port
    real_terminate = mgr_mod._terminate_pid
    mgr_mod.probe_port = lambda *args, **kwargs: True  # type: ignore[assignment]
    mgr_mod._terminate_pid = lambda pid: None  # type: ignore[assignment]
    try:
        manager = PreviewManager(
            store=store,
            tunnel=tunnel,
            token_issuer=_FakeIssuer(),
            audit_log=audit,
            runner=runner,
        )
        preview = manager.start(cwd=tmp_path, sandbox_session=_FakeSandbox())
        assert manager.stop(preview.state.preview_id) is True
    finally:
        mgr_mod.probe_port = real_probe  # type: ignore[assignment]
        mgr_mod._terminate_pid = real_terminate  # type: ignore[assignment]

    assert store.get(preview.state.preview_id) is None
    assert preview.state.tunnel_name in tunnel.closed
    valid, errors = audit.verify()
    assert valid, errors


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_tunnel_failure_rolls_back_dev_server(tmp_path: Path) -> None:
    """When the tunnel can't open, the dev server is terminated."""
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8"
    )
    runner = _FakeRunner(["http://localhost:1234"])
    audit = _audit_log(tmp_path)
    store = PreviewStore(path=tmp_path / "preview.json")

    import bernstein.core.preview.manager as mgr_mod

    real_probe = mgr_mod.probe_port
    mgr_mod.probe_port = lambda *args, **kwargs: True  # type: ignore[assignment]
    try:
        manager = PreviewManager(
            store=store,
            tunnel=_FakeTunnel(fail=True),
            token_issuer=_FakeIssuer(),
            audit_log=audit,
            runner=runner,
        )
        with pytest.raises(PreviewError):
            manager.start(cwd=tmp_path, sandbox_session=_FakeSandbox())
    finally:
        mgr_mod.probe_port = real_probe  # type: ignore[assignment]

    # Runner should have been terminated and nothing should be persisted.
    assert runner.terminated == [1234]
    assert store.list() == []


def test_port_probe_failure_rolls_back(tmp_path: Path) -> None:
    """A failing TCP probe also tears the dev server down."""
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8"
    )
    runner = _FakeRunner(["http://localhost:1234"])
    store = PreviewStore(path=tmp_path / "preview.json")
    audit = _audit_log(tmp_path)

    import bernstein.core.preview.manager as mgr_mod

    real_probe = mgr_mod.probe_port
    mgr_mod.probe_port = lambda *args, **kwargs: False  # type: ignore[assignment]
    try:
        manager = PreviewManager(
            store=store,
            tunnel=_FakeTunnel(),
            token_issuer=_FakeIssuer(),
            audit_log=audit,
            runner=runner,
        )
        with pytest.raises(PreviewError, match="TCP probe failed"):
            manager.start(cwd=tmp_path, sandbox_session=_FakeSandbox())
    finally:
        mgr_mod.probe_port = real_probe  # type: ignore[assignment]

    assert runner.terminated == [1234]
    assert store.list() == []


def test_no_command_discovered_raises(tmp_path: Path) -> None:
    """An empty cwd without ``--command`` is rejected."""
    runner = _FakeRunner([])
    manager = PreviewManager(
        store=PreviewStore(path=tmp_path / "preview.json"),
        tunnel=_FakeTunnel(),
        token_issuer=_FakeIssuer(),
        audit_log=_audit_log(tmp_path),
        runner=runner,
    )
    with pytest.raises(PreviewError, match="No dev-server command"):
        manager.start(cwd=tmp_path, sandbox_session=_FakeSandbox())


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_reap_expired_terminates_only_expired(tmp_path: Path) -> None:
    """``reap_expired`` only tears down records whose epoch has elapsed."""
    store = PreviewStore(path=tmp_path / "preview.json")
    audit = _audit_log(tmp_path)
    expired = PreviewState(
        preview_id="prv-old",
        command="npm run dev",
        cwd=str(tmp_path),
        port=1234,
        sandbox_backend="worktree",
        sandbox_session_id="x",
        tunnel_provider="cloudflared",
        tunnel_name="t-old",
        public_url="https://old/",
        share_url="https://old/",
        auth_mode="none",
        expires_at_epoch=10.0,
        process_pid=0,
        created_at_epoch=0.0,
    )
    fresh = PreviewState(
        preview_id="prv-new",
        command="npm run dev",
        cwd=str(tmp_path),
        port=2345,
        sandbox_backend="worktree",
        sandbox_session_id="y",
        tunnel_provider="cloudflared",
        tunnel_name="t-new",
        public_url="https://new/",
        share_url="https://new/",
        auth_mode="none",
        expires_at_epoch=10_000_000.0,
        process_pid=0,
        created_at_epoch=0.0,
    )
    store.upsert(expired)
    store.upsert(fresh)

    tunnel = _FakeTunnel()
    manager = PreviewManager(
        store=store,
        tunnel=tunnel,
        token_issuer=_FakeIssuer(),
        audit_log=audit,
    )
    reaped = manager.reap_expired(now=20.0)
    assert reaped == 1
    remaining = [s.preview_id for s in store.list()]
    assert remaining == ["prv-new"]
    assert "t-old" in tunnel.closed
    assert "t-new" not in tunnel.closed


def test_preview_state_round_trip_through_dict() -> None:
    """``PreviewState`` round-trips through ``to_dict``/``from_dict``."""
    s = PreviewState(
        preview_id="prv-x",
        command="cmd",
        cwd="/tmp",
        port=8080,
        sandbox_backend="worktree",
        sandbox_session_id="sbx",
        tunnel_provider="cloudflared",
        tunnel_name="t",
        public_url="https://x/",
        share_url="https://x/",
        auth_mode="token",
        expires_at_epoch=12345.0,
        process_pid=42,
        created_at_epoch=10.0,
    )
    raw = s.to_dict()
    raw["unknown_extra"] = 1  # Forward-compat: unknown keys must be ignored.
    assert PreviewState.from_dict(raw) == s
