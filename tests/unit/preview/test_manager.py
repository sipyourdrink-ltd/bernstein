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
import subprocess
import sys
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
def recorded_signals(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, int, int]]:
    """Intercept ``os.kill`` / ``os.killpg`` and isolate ``_PROC_ROOT`` from host ``/proc``."""
    import bernstein.core.preview.manager as mgr_mod

    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append(("kill", pid, int(sig))))
    if hasattr(os, "killpg"):
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, int(sig))))
    monkeypatch.setattr(mgr_mod, "_PROC_ROOT", tmp_path / "_no_proc")
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


def test_preview_and_cli_import_succeed_without_psutil(tmp_path: Path) -> None:
    """Minimal installs without psutil import CLI/preview cleanly and fail closed."""
    repo_src = Path(__file__).resolve().parents[3] / "src"
    probe = f"""
import importlib.abc
import os
import sys
from pathlib import Path

class _BlockPsutil(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "psutil" or fullname.startswith("psutil."):
            raise ModuleNotFoundError("No module named 'psutil'")
        return None

sys.meta_path.insert(0, _BlockPsutil())
sys.modules.pop("psutil", None)

signals = []
os.kill = lambda pid, sig: signals.append(("kill", pid, int(sig)))
if hasattr(os, "killpg"):
    os.killpg = lambda pgid, sig: signals.append(("killpg", pgid, int(sig)))
if hasattr(os, "getpgid"):
    os.getpgid = lambda pid: pid

import bernstein.cli.main
import bernstein.cli.commands.preview_cmd
import bernstein.core.preview.manager as mgr_mod

assert "psutil" not in sys.modules
if hasattr(mgr_mod, "_PROC_ROOT"):
    mgr_mod._PROC_ROOT = Path({str(tmp_path / "no-proc")!r})

target_pid = max(os.getpid(), os.getpgrp() if hasattr(os, "getpgrp") else 0) + 12345
assert mgr_mod._read_process_create_time(target_pid) is None
signaled, reason = mgr_mod._terminate_pid(target_pid, expected_start_time=1_700_000_000.0)
assert (signaled, reason) == (False, "psutil_unavailable"), (signaled, reason)
assert signals == [], signals
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_src)
    proc = subprocess.run(
        [sys.executable, "-S", "-c", f"import site; site.main()\n{probe}"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


def test_terminate_pid_and_manager_stop_fail_closed_when_psutil_unavailable(
    tmp_path: Path,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When psutil cannot be imported, guard refuses to signal with a clear reason and cleans state."""
    import bernstein.core.preview.manager as mgr_mod

    target_pid = max(os.getpid(), os.getpgrp() if hasattr(os, "getpgrp") else 0) + 9100
    if hasattr(os, "getpgid"):
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(mgr_mod, "_PROC_ROOT", tmp_path / "no-proc", raising=False)

    store = PreviewStore(path=tmp_path / "preview.json")
    audit = _audit_log(tmp_path)
    store.upsert(
        PreviewState(
            preview_id="prv-no-psutil",
            command="npm run dev",
            cwd=str(tmp_path),
            port=3000,
            sandbox_backend="worktree",
            sandbox_session_id="sbx",
            tunnel_provider="cloudflared",
            tunnel_name="t-no-psutil",
            public_url="https://no-psutil/",
            share_url="https://no-psutil/",
            auth_mode="none",
            expires_at_epoch=9_999_999_999.0,
            process_pid=target_pid,
            process_start_time=1_700_000_000.0,
            created_at_epoch=0.0,
        )
    )
    manager = PreviewManager(
        store=store,
        tunnel=_FakeTunnel(),
        token_issuer=_FakeIssuer(),
        audit_log=audit,
    )

    with monkeypatch.context() as mp:
        mp.setitem(sys.modules, "psutil", None)
        assert mgr_mod._read_process_create_time(target_pid) is None
        assert mgr_mod._terminate_pid(target_pid, expected_start_time=1_700_000_000.0) == (
            False,
            "psutil_unavailable",
        )
        with caplog.at_level(logging.WARNING, logger="bernstein.core.preview.manager"):
            assert manager.stop("prv-no-psutil") is True

    assert recorded_signals == []
    assert store.get("prv-no-psutil") is None
    assert manager.last_skipped_pids == (("prv-no-psutil", target_pid, "psutil_unavailable"),)
    assert "psutil_unavailable" in caplog.text
    events = [
        json.loads(line)
        for f in sorted((tmp_path / "audit").glob("*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    stop_details = [e["details"] for e in events if e["event_type"] == "preview.stop"][-1]
    assert stop_details["process_signal_status"] == "skipped"
    assert stop_details["process_signal_reason"] == "psutil_unavailable"


def _write_fake_proc_stat(proc_root: Path, pid: int, comm: str, start_ticks: int) -> None:
    """Create a synthetic Linux ``/proc/<pid>/stat`` with *start_ticks* at field 22."""
    # Fields after ``(comm)``: field 3 (state) through field 22 (starttime) = 20 tokens (index 19).
    after_comm = ["S", "1", str(pid), str(pid), "0", "-1", "4194304"] + ["0"] * 12 + [str(start_ticks), "1024"]
    stat_dir = proc_root / str(pid)
    stat_dir.mkdir(parents=True, exist_ok=True)
    (stat_dir / "stat").write_text(f"{pid} ({comm}) {' '.join(after_comm)}\n", encoding="utf-8")


def test_linux_proc_stat_start_ticks_survives_btime_shift_and_rejects_recycled_pid(
    tmp_path: Path,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monotonic /proc/<pid>/stat field-22 ticks survive ±1s btime NTP jumps and fail closed on mismatch."""
    import bernstein.core.preview.manager as mgr_mod

    target_pid = max(os.getpid(), os.getpgrp() if hasattr(os, "getpgrp") else 0) + 9200
    fake_proc = tmp_path / "proc"
    _write_fake_proc_stat(fake_proc, target_pid, "node (dev) server", start_ticks=987_654_321)
    monkeypatch.setattr(mgr_mod, "_PROC_ROOT", fake_proc, raising=False)
    if hasattr(os, "getpgid"):
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)

    # PreviewState round-trips process_start_ticks and defaults legacy records to None.
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}), encoding="utf-8")
    monkeypatch.setattr(mgr_mod, "probe_port", lambda *args, **kwargs: True)
    current_create_time = [1_700_000_000.0]
    _patch_psutil_create_time(monkeypatch, lambda _pid: current_create_time[0])

    store = PreviewStore(path=tmp_path / "preview.json")
    manager = PreviewManager(
        store=store,
        tunnel=_FakeTunnel(),
        token_issuer=_FakeIssuer(),
        audit_log=_audit_log(tmp_path),
        runner=_FakeRunner(["http://localhost:5173"], pid=target_pid),
    )
    preview = manager.start(cwd=tmp_path, sandbox_session=_FakeSandbox(), approve_discovered=True)
    assert preview.state.process_start_ticks == 987_654_321
    assert PreviewState.from_dict(preview.state.to_dict()).process_start_ticks == 987_654_321

    # Simulate a +1.0s Linux /proc/stat btime NTP step (20x the 0.05s create_time tolerance):
    # matching kernel starttime ticks still verify and signal the dev server.
    current_create_time[0] = 1_700_000_001.0
    assert manager.stop(preview.state.preview_id) is True
    expected_sig = (
        [("killpg", target_pid, int(signal.SIGTERM))]
        if hasattr(os, "killpg")
        else [("kill", target_pid, int(signal.SIGTERM))]
    )
    assert recorded_signals == expected_sig
    recorded_signals.clear()

    # Recycled PID (ticks changed) must fail closed even if psutil create_time matches.
    _write_fake_proc_stat(fake_proc, target_pid, "other-proc", start_ticks=987_654_999)
    current_create_time[0] = 1_700_000_000.0
    assert mgr_mod._terminate_pid(
        target_pid,
        expected_start_time=1_700_000_000.0,
        expected_start_ticks=987_654_321,
    ) == (False, "start_time_mismatch")

    # Malformed /proc/<pid>/stat fails closed without downgrading to psutil.
    (fake_proc / str(target_pid) / "stat").write_text("corrupt stat line\n", encoding="utf-8")
    assert mgr_mod._terminate_pid(
        target_pid,
        expected_start_time=1_700_000_000.0,
        expected_start_ticks=987_654_321,
    ) == (False, "unreadable_start_ticks")

    # Invalid expected_start_ticks values fail closed.
    for bad_ticks in (True, False, 0, -5, "987654321"):
        assert mgr_mod._terminate_pid(
            target_pid,
            expected_start_time=1_700_000_000.0,
            expected_start_ticks=bad_ticks,
        ) == (False, "invalid_start_ticks")
    assert recorded_signals == []


def test_terminate_pid_windows_fallback_signals_single_pid_after_start_time_check(
    tmp_path: Path,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When POSIX process-group APIs are absent (Windows), verify start time and call os.kill."""
    import bernstein.core.preview.manager as mgr_mod

    monkeypatch.setattr(os, "getpgrp", None, raising=False)
    monkeypatch.setattr(os, "getpgid", None, raising=False)
    monkeypatch.setattr(os, "killpg", None, raising=False)
    monkeypatch.setattr(mgr_mod, "_PROC_ROOT", tmp_path / "no-proc", raising=False)

    target_pid = os.getpid() + 9300
    _patch_psutil_create_time(monkeypatch, lambda _pid: 1_700_000_000.0)

    for bad_pid in (0, -1, 1, True):
        assert mgr_mod._terminate_pid(bad_pid, expected_start_time=1_700_000_000.0) == (False, "invalid_pid")

    assert mgr_mod._terminate_pid(target_pid, expected_start_time=1_700_000_500.0) == (False, "start_time_mismatch")

    with monkeypatch.context() as mp:
        mp.setitem(sys.modules, "psutil", None)
        assert mgr_mod._terminate_pid(target_pid, expected_start_time=1_700_000_000.0) == (
            False,
            "psutil_unavailable",
        )
    assert recorded_signals == []

    assert mgr_mod._terminate_pid(target_pid, expected_start_time=1_700_000_000.0) == (True, "signaled")
    assert recorded_signals == [("kill", target_pid, int(signal.SIGTERM))]


def test_preview_stop_all_cli_reports_skipped_pids(
    tmp_path: Path,
    recorded_signals: list[tuple[str, int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bernstein preview stop --all`` reports every skipped PID and cleans up state."""
    import bernstein.core.preview.manager as mgr_mod
    from bernstein.cli.commands import preview_cmd

    store = PreviewStore(path=tmp_path / "preview.json")
    base_pid = max(os.getpid(), os.getpgrp() if hasattr(os, "getpgrp") else 0) + 9400
    pid_legacy = base_pid
    pid_no_psutil = base_pid + 1
    if hasattr(os, "getpgid"):
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(mgr_mod, "_PROC_ROOT", tmp_path / "no-proc", raising=False)

    store.path.write_text(
        json.dumps(
            {
                "previews": [
                    {
                        "preview_id": "prv-all-legacy",
                        "command": "npm run dev",
                        "cwd": str(tmp_path),
                        "port": 3001,
                        "sandbox_backend": "worktree",
                        "sandbox_session_id": "sbx-1",
                        "tunnel_provider": "cloudflared",
                        "tunnel_name": "t-all-1",
                        "public_url": "https://all-1/",
                        "share_url": "https://all-1/",
                        "auth_mode": "none",
                        "expires_at_epoch": 9_999_999_999.0,
                        "process_pid": pid_legacy,
                        "created_at_epoch": 0.0,
                    },
                    {
                        "preview_id": "prv-all-nopsutil",
                        "command": "npm run dev",
                        "cwd": str(tmp_path),
                        "port": 3002,
                        "sandbox_backend": "worktree",
                        "sandbox_session_id": "sbx-2",
                        "tunnel_provider": "cloudflared",
                        "tunnel_name": "t-all-2",
                        "public_url": "https://all-2/",
                        "share_url": "https://all-2/",
                        "auth_mode": "none",
                        "expires_at_epoch": 9_999_999_999.0,
                        "process_pid": pid_no_psutil,
                        "process_start_time": 1_700_000_000.0,
                        "created_at_epoch": 0.0,
                    },
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

    with monkeypatch.context() as mp:
        mp.setitem(sys.modules, "psutil", None)
        result = CliRunner().invoke(preview_cmd.preview_group, ["stop", "--all"])

    assert result.exit_code == 0, result.output
    assert recorded_signals == []
    assert store.list() == []
    rendered = "\n".join(out_lines)
    assert str(pid_legacy) in rendered and "missing_start_time" in rendered
    assert str(pid_no_psutil) in rendered and "psutil_unavailable" in rendered
    assert "Stopped 2 preview(s)." in rendered
