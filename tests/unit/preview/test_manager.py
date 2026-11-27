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
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import pytest
from click.testing import CliRunner

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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def recorded_signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Intercept ``os.kill`` / ``os.killpg`` so unit tests never emit real signals."""
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append(("kill", pid, int(sig))))
    if hasattr(os, "killpg"):
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, int(sig))))
    return calls


def _patch_psutil_create_time(monkeypatch: pytest.MonkeyPatch, resolver: Any) -> None:
    """Patch ``psutil.Process(pid)`` while keeping ``psutil.Process()`` working for conftest."""
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
        # Skip the parent constructor - we override every method.
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


def test_discovered_command_requires_approval(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "example-check"}}))
    runner = _FakeRunner(["Local: http://localhost:3000"])
    with pytest.raises(PreviewError, match="approval"):
        PreviewManager(store=PreviewStore(tmp_path / "state.json"), runner=runner, tunnel=_FakeTunnel()).start(
            cwd=tmp_path, sandbox_session=_FakeSandbox()
        )


def test_isolated_preview_uses_argv_and_loopback_mapping(tmp_path, monkeypatch):
    import subprocess
    from unittest.mock import patch

    from bernstein.core.preview.manager import ContainerDevServerRunner

    monkeypatch.setenv("BERNSTEIN_PREVIEW_IMAGE", "preview-fixture")
    monkeypatch.setenv("BERNSTEIN_PREVIEW_PORT", "4321")
    with (
        patch("bernstein.core.preview.manager.runtime_executable", return_value="/local/bin/docker"),
        patch(
            "bernstein.core.preview.manager.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, b"container-id"),
                subprocess.CompletedProcess([], 0, b"127.0.0.1:32123"),
            ],
        ) as run,
    ):
        handle = ContainerDevServerRunner().spawn(command='npm run dev -- "a b"', cwd=tmp_path)

    args = run.call_args_list[0].args[0]
    assert args[:3] == ["/local/bin/docker", "run", "--detach"]
    assert args[-8:] == ["--entrypoint", "npm", "--", "preview-fixture", "run", "dev", "--", "a b"]
    assert "--read-only" in args
    assert "127.0.0.1::4321" in args
    assert handle.host_port == 32123
    assert handle.backend == "docker"
    assert handle.container_name == args[args.index("--name") + 1]
    assert not run.call_args_list[0].kwargs.get("shell", False)


def test_start_persists_state_and_writes_audit_chain(tmp_path: Path) -> None:
    """Successful start emits 2 audit entries (start + link) and persists state."""
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8")
    runner = _FakeRunner(["VITE ready", "Local:   http://localhost:5173/"])
    audit = _audit_log(tmp_path)
    store = PreviewStore(path=tmp_path / "preview.json")

    # Replace probe_port with an instant success - we don't want the
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
            approve_discovered=True,
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
        json.loads(line) for f in log_files for line in f.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    event_types = [r["event_type"] for r in raw_lines]
    assert "preview.start" in event_types
    assert "preview.link" in event_types


def test_stop_removes_record_and_audits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recorded_signals: list[tuple[str, int, int]],
) -> None:
    """``stop`` tears the tunnel down, deletes state, audits ``preview.stop``."""
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8")
    runner = _FakeRunner(["http://localhost:1234"])
    tunnel = _FakeTunnel()
    audit = _audit_log(tmp_path)
    store = PreviewStore(path=tmp_path / "preview.json")

    import bernstein.core.preview.manager as mgr_mod

    monkeypatch.setattr(mgr_mod, "probe_port", lambda *args, **kwargs: True)
    monkeypatch.setattr(mgr_mod, "_terminate_pid", lambda *args, **kwargs: (False, "skipped"))
    manager = PreviewManager(
        store=store,
        tunnel=tunnel,
        token_issuer=_FakeIssuer(),
        audit_log=audit,
        runner=runner,
    )
    preview = manager.start(cwd=tmp_path, sandbox_session=_FakeSandbox(), approve_discovered=True)
    assert manager.stop(preview.state.preview_id) is True

    assert recorded_signals == []
    assert store.get(preview.state.preview_id) is None
    assert preview.state.tunnel_name in tunnel.closed
    valid, errors = audit.verify()
    assert valid, errors


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_tunnel_failure_rolls_back_dev_server(tmp_path: Path) -> None:
    """When the tunnel can't open, the dev server is terminated."""
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8")
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
            manager.start(cwd=tmp_path, sandbox_session=_FakeSandbox(), approve_discovered=True)
    finally:
        mgr_mod.probe_port = real_probe  # type: ignore[assignment]

    # Runner should have been terminated and nothing should be persisted.
    assert runner.terminated == [1234]
    assert store.list() == []


def test_port_probe_failure_rolls_back(tmp_path: Path) -> None:
    """A failing TCP probe also tears the dev server down."""
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8")
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
            manager.start(cwd=tmp_path, sandbox_session=_FakeSandbox(), approve_discovered=True)
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
        manager.start(cwd=tmp_path, sandbox_session=_FakeSandbox(), approve_discovered=True)


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


def test_stop_keeps_state_when_container_cleanup_fails(tmp_path):
    import subprocess
    from unittest.mock import patch

    store = PreviewStore(tmp_path / "state.json")
    name = "bernstein-preview-0123456789abcdef"
    state = PreviewState(
        preview_id="prv-retry",
        command="example",
        cwd=str(tmp_path),
        port=3000,
        sandbox_backend="docker",
        sandbox_session_id=name,
        tunnel_provider="cloudflared",
        tunnel_name="example",
        public_url="https://example.invalid",
        share_url="https://example.invalid",
        auth_mode="none",
        expires_at_epoch=1,
        process_pid=0,
        container_name=name,
    )
    raw = dict(state.to_dict(), runtime_path="/untrusted/workspace/docker")
    store.upsert(PreviewState.from_dict(raw))
    manager = PreviewManager(store=store, tunnel=_FakeTunnel(), audit_log=_audit_log(tmp_path))
    with (
        patch("bernstein.core.preview.manager.runtime_executable", return_value="/local/bin/docker") as resolve,
        patch(
            "bernstein.core.preview.manager.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 1, stderr=b"daemon unavailable"),
                subprocess.CompletedProcess([], 0, stderr=b""),
            ],
        ) as run,
    ):
        with pytest.raises(PreviewError, match="cleanup could not be confirmed"):
            manager.stop(state.preview_id)
        assert store.get(state.preview_id) == state
        assert manager.stop(state.preview_id)
    assert store.get(state.preview_id) is None
    assert all(call.args == ("docker",) for call in resolve.call_args_list)
    assert all(call.args[0] == ["/local/bin/docker", "rm", "--force", name] for call in run.call_args_list)


# ---------------------------------------------------------------------------
# Fail-closed PID identity guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad_pid", "expected_reason"),
    [
        (False, "invalid_pid"),
        (True, "invalid_pid"),
        (0, "invalid_pid"),
        (-1, "invalid_pid"),
        (-42, "invalid_pid"),
        (1, "invalid_pid"),
    ],
)
def test_terminate_pid_refuses_invalid_and_bool_pids(
    bad_pid: Any,
    expected_reason: str,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bernstein.core.preview.manager as mgr_mod

    if hasattr(os, "getpgid"):
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    signaled, reason = mgr_mod._terminate_pid(bad_pid, expected_start_time=1_700_000_000.0)
    assert signaled is False
    assert reason == expected_reason
    assert recorded_signals == []


def test_terminate_pid_refuses_self_pid_and_own_pgrp(
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bernstein.core.preview.manager as mgr_mod

    own_pid = os.getpid()
    signaled, reason = mgr_mod._terminate_pid(own_pid, expected_start_time=1_700_000_000.0)
    assert (signaled, reason) == (False, "self_pid")
    assert recorded_signals == []

    if hasattr(os, "getpgrp") and hasattr(os, "getpgid"):
        own_pgid = os.getpgrp()
        if own_pgid != own_pid:
            signaled_pgrp, reason_pgrp = mgr_mod._terminate_pid(own_pgid, expected_start_time=1_700_000_000.0)
            assert (signaled_pgrp, reason_pgrp) == (False, "own_process_group")
        peer_pid = max(own_pid, own_pgid) + 5000
        monkeypatch.setattr(os, "getpgid", lambda pid: own_pgid)
        signaled_peer, reason_peer = mgr_mod._terminate_pid(peer_pid, expected_start_time=1_700_000_000.0)
        assert (signaled_peer, reason_peer) == (False, "own_process_group")
        assert recorded_signals == []


def test_terminate_pid_fails_closed_when_start_time_unreadable_or_invalid(
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bernstein.core.preview.manager as mgr_mod

    target_pid = max(os.getpid(), os.getpgrp() if hasattr(os, "getpgrp") else 0) + 6000
    if hasattr(os, "getpgid"):
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    for bad_ts, expected_reason in [
        (None, "missing_start_time"),
        (True, "invalid_start_time"),
        (-5.0, "invalid_start_time"),
        (float("nan"), "invalid_start_time"),
        (float("inf"), "invalid_start_time"),
    ]:
        assert mgr_mod._terminate_pid(target_pid, expected_start_time=bad_ts) == (False, expected_reason)

    def _deny_create_time(pid: int) -> float:
        raise psutil.AccessDenied(pid)

    _patch_psutil_create_time(monkeypatch, _deny_create_time)
    assert mgr_mod._terminate_pid(target_pid, expected_start_time=1_700_000_000.0) == (False, "unreadable_start_time")

    if hasattr(os, "getpgid"):

        def _deny_pgid(_pid: int) -> int:
            raise PermissionError("denied")

        monkeypatch.setattr(os, "getpgid", _deny_pgid)
        assert mgr_mod._terminate_pid(target_pid, expected_start_time=1_700_000_000.0) == (False, "unreadable_pgid")

    assert recorded_signals == []


def test_terminate_pid_refuses_pgid_1_and_limits_non_leader_to_single_pid(
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not (hasattr(os, "getpgid") and hasattr(os, "killpg")):
        pytest.skip("POSIX process groups not available on this platform")

    import bernstein.core.preview.manager as mgr_mod

    target_pid = max(os.getpid(), os.getpgrp()) + 7000
    _patch_psutil_create_time(monkeypatch, lambda _pid: 1_700_000_000.0)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 1)
    assert mgr_mod._terminate_pid(target_pid, expected_start_time=1_700_000_000.0) == (False, "unsafe_process_group")
    assert recorded_signals == []

    # When target is NOT the process-group leader, signal only the verified PID
    # rather than killing an unverified group leader via killpg.
    other_pgid = target_pid + 1
    monkeypatch.setattr(os, "getpgid", lambda _pid: other_pgid)
    assert mgr_mod._terminate_pid(target_pid, expected_start_time=1_700_000_000.0) == (True, "signaled")
    assert recorded_signals == [("kill", target_pid, int(signal.SIGTERM))]


def test_reap_expired_skips_stale_pid_reports_and_cleans_state(
    tmp_path: Path,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = PreviewStore(path=tmp_path / "preview.json")
    audit = _audit_log(tmp_path)
    target_pid = max(os.getpid(), os.getpgrp() if hasattr(os, "getpgrp") else 0) + 8000
    store.path.write_text(
        json.dumps(
            {
                "previews": [
                    {
                        "preview_id": "prv-expired-stale",
                        "command": "npm run dev",
                        "cwd": str(tmp_path),
                        "port": 1234,
                        "sandbox_backend": "worktree",
                        "sandbox_session_id": "x",
                        "tunnel_provider": "cloudflared",
                        "tunnel_name": "t-stale",
                        "public_url": "https://old/",
                        "share_url": "https://old/",
                        "auth_mode": "none",
                        "expires_at_epoch": 10.0,
                        "process_pid": target_pid,
                        "process_start_time": 1_700_000_000.0,
                        "created_at_epoch": 0.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    if hasattr(os, "getpgid"):
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    _patch_psutil_create_time(monkeypatch, lambda _pid: 1_700_009_999.0)

    manager = PreviewManager(store=store, tunnel=_FakeTunnel(), token_issuer=_FakeIssuer(), audit_log=audit)
    with caplog.at_level(logging.WARNING, logger="bernstein.core.preview.manager"):
        reaped = manager.reap_expired(now=20.0)

    assert reaped == 1
    assert recorded_signals == []
    assert store.list() == []
    assert "start_time_mismatch" in caplog.text


def test_preview_state_round_trip_with_process_start_time() -> None:
    state = PreviewState(
        preview_id="prv-ts",
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
        process_start_time=1_700_000_123.5,
        created_at_epoch=10.0,
    )
    assert PreviewState.from_dict(state.to_dict()) == state

    legacy = state.to_dict()
    legacy.pop("process_start_time")
    restored = PreviewState.from_dict(legacy)
    assert restored.process_start_time is None


def test_preview_stop_cli_reports_skipped_pid(
    tmp_path: Path,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bernstein.cli.commands import preview_cmd

    store = PreviewStore(path=tmp_path / "preview.json")
    target_pid = max(os.getpid(), os.getpgrp() if hasattr(os, "getpgrp") else 0) + 9000
    store.path.write_text(
        json.dumps(
            {
                "previews": [
                    {
                        "preview_id": "prv-cli-legacy",
                        "command": "npm run dev",
                        "cwd": str(tmp_path),
                        "port": 3000,
                        "sandbox_backend": "worktree",
                        "sandbox_session_id": "sbx",
                        "tunnel_provider": "cloudflared",
                        "tunnel_name": "t-cli",
                        "public_url": "https://cli/",
                        "share_url": "https://cli/",
                        "auth_mode": "none",
                        "expires_at_epoch": 9_999_999_999.0,
                        "process_pid": target_pid,
                        "created_at_epoch": 0.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    manager = PreviewManager(
        store=store,
        tunnel=_FakeTunnel(),
        token_issuer=_FakeIssuer(),
        audit_log=_audit_log(tmp_path),
    )
    monkeypatch.setattr(preview_cmd, "PreviewManager", lambda: manager)

    out_lines: list[str] = []
    monkeypatch.setattr(
        preview_cmd.console, "print", lambda *args, **kwargs: out_lines.append(" ".join(map(str, args)))
    )

    result = CliRunner().invoke(preview_cmd.preview_group, ["stop", "prv-cli-legacy"])
    assert result.exit_code == 0, result.output
    assert recorded_signals == []
    assert store.get("prv-cli-legacy") is None
    rendered = "\n".join(out_lines)
    assert str(target_pid) in rendered and "missing_start_time" in rendered
