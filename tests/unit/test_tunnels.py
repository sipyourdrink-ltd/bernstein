"""Unit tests for the ``bernstein tunnel`` wrapper (op-003)."""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.tunnels.drivers import register_default_drivers
from bernstein.core.tunnels.drivers.bore import BoreDriver
from bernstein.core.tunnels.drivers.cloudflared import CloudflaredDriver
from bernstein.core.tunnels.drivers.ngrok import NgrokDriver
from bernstein.core.tunnels.drivers.tailscale import TailscaleDriver
from bernstein.core.tunnels.protocol import (
    ProviderNotAvailable,
    TunnelHandle,
    TunnelProvider,
)
from bernstein.core.tunnels.registry import TunnelRegistry

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProvider(TunnelProvider):
    """In-memory provider used to exercise the registry without spawning procs."""

    def __init__(self, name: str, binary: str, base_port: int = 40000) -> None:
        self.name = name
        self.binary = binary
        self._base = base_port
        self.started: list[str] = []
        self.stopped: list[str] = []

    def detect(self) -> Any:  # pragma: no cover - unused in fake
        raise NotImplementedError

    def start(self, port: int, name: str) -> TunnelHandle:
        self.started.append(name)
        pid = self._base + len(self.started)
        return TunnelHandle(
            name=name,
            provider=self.name,
            port=port,
            public_url=f"https://fake-{self.name}.example.com",
            pid=pid,
        )

    def stop(self, name: str) -> None:
        self.stopped.append(name)


@pytest.fixture(autouse=True)
def _chdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate each test to its own temp cwd."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_refuses_to_replace_a_default_driver() -> None:
    registry = TunnelRegistry()
    register_default_drivers(registry)
    first = registry.providers()["cloudflared"]

    with pytest.raises(ValueError, match="Duplicate tunnel provider") as duplicate:
        registry.register(_FakeProvider("cloudflared", "fake"))

    assert "bernstein.core.tunnels.drivers" in str(duplicate.value)
    assert __name__ in str(duplicate.value)
    assert registry.providers()["cloudflared"] is first


def test_registry_refuses_to_replace_a_constructor_driver() -> None:
    first = _FakeProvider("fixture", "first")
    registry = TunnelRegistry(providers={"fixture": first})

    with pytest.raises(ValueError, match="Duplicate tunnel provider"):
        registry.register(_FakeProvider("fixture", "second"))

    assert registry.providers() == {"fixture": first}


def test_registry_round_trip_across_restart(tmp_path: Path) -> None:
    """State written by one registry instance is readable by the next."""
    state = tmp_path / "tunnels.json"
    r1 = TunnelRegistry(state_path=state)
    r1.register(_FakeProvider("cloudflared", "cloudflared"))
    h = r1.create(port=5173, provider="cloudflared", name="first")

    # New registry instance should see the persisted tunnel.
    r2 = TunnelRegistry(state_path=state)
    got = r2.get("first")
    assert got is not None
    assert got.port == 5173
    assert got.public_url == h.public_url
    assert got.pid == h.pid
    assert [x.name for x in r2.list_active()] == ["first"]


def test_registry_destroy_removes_entry_and_persists(tmp_path: Path) -> None:
    """``destroy`` removes the tunnel from disk state as well as memory."""
    state = tmp_path / "tunnels.json"
    reg = TunnelRegistry(state_path=state)
    prov = _FakeProvider("cloudflared", "cloudflared")
    reg.register(prov)
    reg.create(port=8080, provider="cloudflared", name="t1")
    assert reg.destroy("t1") is True
    assert reg.get("t1") is None
    assert prov.stopped == ["t1"]
    raw = json.loads(state.read_text())
    assert raw["tunnels"] == []


def test_registry_atomic_write_does_not_leave_tmp_files(tmp_path: Path) -> None:
    """Atomic writes should not leak ``.tmp`` siblings on success."""
    state = tmp_path / "tunnels.json"
    reg = TunnelRegistry(state_path=state)
    reg.register(_FakeProvider("cloudflared", "cloudflared"))
    reg.create(port=3000, provider="cloudflared", name="x")
    leftovers = [p for p in tmp_path.iterdir() if p.name != "tunnels.json"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Auto-selection
# ---------------------------------------------------------------------------


def test_auto_prefers_cloudflared_over_ngrok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When both cloudflared and ngrok are on PATH, cloudflared wins."""
    reg = TunnelRegistry(state_path=tmp_path / "t.json")
    cf = _FakeProvider("cloudflared", "cloudflared")
    ng = _FakeProvider("ngrok", "ngrok")
    reg.register(cf)
    reg.register(ng)

    fake_path = {"cloudflared": "/usr/local/bin/cloudflared", "ngrok": "/usr/local/bin/ngrok"}
    monkeypatch.setattr(
        "bernstein.core.tunnels.registry.shutil.which",
        lambda name: fake_path.get(name),
    )
    handle = reg.create(port=9000, provider=None, name="auto1")
    assert handle.provider == "cloudflared"
    assert cf.started == ["auto1"]
    assert ng.started == []


def test_auto_falls_back_when_cloudflared_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If cloudflared is absent, the next provider in order is chosen."""
    reg = TunnelRegistry(state_path=tmp_path / "t.json")
    cf = _FakeProvider("cloudflared", "cloudflared")
    bore = _FakeProvider("bore", "bore")
    reg.register(cf)
    reg.register(bore)

    monkeypatch.setattr(
        "bernstein.core.tunnels.registry.shutil.which",
        lambda name: "/usr/local/bin/bore" if name == "bore" else None,
    )
    handle = reg.create(port=9000, provider="auto", name="auto2")
    assert handle.provider == "bore"


def test_auto_raises_when_nothing_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no binaries available, ``create`` raises with an install hint."""
    reg = TunnelRegistry(state_path=tmp_path / "t.json")
    reg.register(_FakeProvider("cloudflared", "cloudflared"))
    reg.register(_FakeProvider("ngrok", "ngrok"))
    monkeypatch.setattr("bernstein.core.tunnels.registry.shutil.which", lambda _name: None)
    with pytest.raises(ProviderNotAvailable) as excinfo:
        reg.create(port=8080, provider="auto")
    assert "brew install" in excinfo.value.hint.lower()


# ---------------------------------------------------------------------------
# URL parsers per driver
# ---------------------------------------------------------------------------


def test_cloudflared_parser_extracts_trycloudflare_url() -> None:
    """Parse the URL from recorded cloudflared stdout."""
    sample = (
        "2024-01-01 cloudflared INF Starting tunnel\n"
        "Your quick Tunnel has been created! Visit it at:\n"
        "https://whispering-kite-1234.trycloudflare.com\n"
    )
    assert CloudflaredDriver.parse_url(sample) == "https://whispering-kite-1234.trycloudflare.com"


def test_cloudflared_parser_returns_none_without_url() -> None:
    """No URL in stdout yields ``None``."""
    assert CloudflaredDriver.parse_url("INF starting...") is None


def test_ngrok_parser_extracts_https_url_from_json_stream() -> None:
    """Parse a ``url=`` field from ngrok's JSON log lines."""
    sample = "\n".join(
        [
            json.dumps({"lvl": "info", "msg": "starting tunnel"}),
            json.dumps({"lvl": "info", "msg": "started tunnel", "url": "https://abc.ngrok.io"}),
        ]
    )
    assert NgrokDriver.parse_url(sample) == "https://abc.ngrok.io"


def test_ngrok_parser_skips_non_json_lines() -> None:
    """Garbage prefix lines are ignored."""
    sample = "some stderr garbage\n" + json.dumps({"url": "https://xyz.ngrok.io", "msg": "ok"})
    assert NgrokDriver.parse_url(sample) == "https://xyz.ngrok.io"


def test_bore_parser_builds_tcp_url_from_listening_line() -> None:
    """Parse ``listening at bore.pub:PORT`` into a ``tcp://`` URL."""
    sample = "listening at bore.pub:44567\n"
    assert BoreDriver.parse_url(sample) == "tcp://bore.pub:44567"


def test_tailscale_parser_extracts_dns_name() -> None:
    """Parse the local node's DNS name from ``tailscale status --json``."""
    sample = json.dumps(
        {
            "Self": {
                "DNSName": "my-machine.tail1234.ts.net.",
                "HostName": "my-machine",
            }
        }
    )
    assert TailscaleDriver.parse_url(sample) == "https://my-machine.tail1234.ts.net"


# ---------------------------------------------------------------------------
# Detection error paths
# ---------------------------------------------------------------------------


def test_missing_binary_raises_provider_not_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``detect`` should raise ``ProviderNotAvailable`` with an install hint."""
    monkeypatch.setattr(
        "bernstein.core.tunnels.drivers._base.shutil.which",
        lambda _name: None,
    )
    with pytest.raises(ProviderNotAvailable) as excinfo:
        CloudflaredDriver().detect()
    assert excinfo.value.hint == "brew install cloudflared"


def test_missing_bore_binary_hints_cargo(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bore`` install hint should mention ``cargo install bore-cli``."""
    monkeypatch.setattr(
        "bernstein.core.tunnels.drivers._base.shutil.which",
        lambda _name: None,
    )
    with pytest.raises(ProviderNotAvailable) as excinfo:
        BoreDriver().detect()
    assert "cargo install bore-cli" in excinfo.value.hint


# ---------------------------------------------------------------------------
# Stop --all
# ---------------------------------------------------------------------------


def test_stop_all_sigterms_every_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``bernstein tunnel stop --all`` should SIGTERM each active PID."""
    state = tmp_path / "tunnels.json"
    reg = TunnelRegistry(state_path=state)
    prov = _FakeProvider("cloudflared", "cloudflared", base_port=10000)
    reg.register(prov)
    reg.create(port=3000, provider="cloudflared", name="a")
    reg.create(port=3001, provider="cloudflared", name="b")

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "bernstein.core.tunnels.process_guard._PROC_ROOT",
        tmp_path / "no-proc",
    )
    monkeypatch.setattr(
        "bernstein.core.tunnels.process_guard.IS_WINDOWS",
        False,
    )
    monkeypatch.setattr(
        "bernstein.core.tunnels.process_guard.os.kill",
        lambda pid, sig: sent.append((pid, sig)),
    )
    monkeypatch.setattr(
        "bernstein.core.tunnels.process_guard.is_process_alive",
        lambda _pid: True,
    )
    monkeypatch.setattr(
        "bernstein.core.tunnels.process_guard.pid_command_line",
        lambda _pid: "cloudflared tunnel --url http://localhost:3000",
    )
    monkeypatch.setattr(
        "bernstein.core.tunnels.process_guard._ps_comm",
        lambda _pid: None,
    )
    # Re-import registry in the command under test: it reads the same state file.
    monkeypatch.setattr(
        "bernstein.cli.commands.tunnel_cmd.TunnelRegistry",
        lambda: TunnelRegistry(state_path=state),
    )
    # Drivers registered in the command path are fresh instances; that's fine -
    # ``destroy`` tolerates a missing in-memory process, and we only check signals.

    from click.testing import CliRunner

    from bernstein.cli.commands.tunnel_cmd import tunnel_group

    runner = CliRunner()
    result = runner.invoke(tunnel_group, ["stop", "--all"])
    assert result.exit_code == 0, result.output
    pids = sorted(p for p, _ in sent)
    assert pids == [10001, 10002]
    assert all(sig == signal.SIGTERM for _, sig in sent)
    # Both tunnels should be gone from persisted state.
    assert TunnelRegistry(state_path=state).list_active() == []


def test_stop_all_is_noop_with_no_tunnels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--all`` with no tunnels is a no-op and exits 0 without errors."""
    from click.testing import CliRunner

    state = tmp_path / "tunnels.json"
    monkeypatch.setattr(
        "bernstein.cli.commands.tunnel_cmd.TunnelRegistry",
        lambda: TunnelRegistry(state_path=state),
    )
    sent: list[int] = []
    monkeypatch.setattr(
        "bernstein.core.tunnels.process_guard.os.kill",
        lambda pid, _sig: sent.append(pid),
    )

    from bernstein.cli.commands.tunnel_cmd import tunnel_group

    runner = CliRunner()
    result = runner.invoke(tunnel_group, ["stop", "--all"])
    assert result.exit_code == 0
    assert sent == []
    assert "No active tunnels" in result.output


def test_stop_single_unknown_name_errors_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop <name>`` for a missing tunnel exits non-zero with a red message."""
    from click.testing import CliRunner

    state = tmp_path / "tunnels.json"
    monkeypatch.setattr(
        "bernstein.cli.commands.tunnel_cmd.TunnelRegistry",
        lambda: TunnelRegistry(state_path=state),
    )
    from bernstein.cli.commands.tunnel_cmd import tunnel_group

    runner = CliRunner()
    result = runner.invoke(tunnel_group, ["stop", "ghost"])
    assert result.exit_code == 1
    assert "ghost" in result.output


def test_unknown_explicit_provider_raises_key_error(tmp_path: Path) -> None:
    """Explicit unknown provider name is a ``KeyError``."""
    reg = TunnelRegistry(state_path=tmp_path / "t.json")
    reg.register(_FakeProvider("cloudflared", "cloudflared"))
    with pytest.raises(KeyError):
        reg.create(port=5000, provider="nope")


# Sanity: ensure we haven't lost SIGTERM in imports.
def test_signal_sigterm_is_defined() -> None:
    """``signal.SIGTERM`` must exist on the running platform."""
    assert hasattr(signal, "SIGTERM")
    _ = os  # silence unused-import warning


def test_tunnel_stop_cmd_skips_guarded_and_reused_pids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``bernstein tunnel stop --all`` must skip pid=1, self, pgrp, tailscale, and unrelated live PIDs."""
    import subprocess
    import sys

    from click.testing import CliRunner

    from bernstein.cli.commands.tunnel_cmd import tunnel_group

    state = tmp_path / "tunnels.json"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        tunnels: list[dict[str, Any]] = [
            {"name": "init", "provider": "cloudflared", "port": 3000, "public_url": "https://a.example", "pid": 1},
            {
                "name": "self",
                "provider": "cloudflared",
                "port": 3001,
                "public_url": "https://b.example",
                "pid": os.getpid(),
            },
            {
                "name": "reused",
                "provider": "cloudflared",
                "port": 3002,
                "public_url": "https://c.example",
                "pid": proc.pid,
            },
            {
                "name": "ts",
                "provider": "tailscale",
                "port": 3003,
                "public_url": "https://d.example",
                "pid": proc.pid,
            },
        ]
        state.write_text(json.dumps({"tunnels": tunnels}), encoding="utf-8")
        monkeypatch.setattr(
            "bernstein.cli.commands.tunnel_cmd.TunnelRegistry",
            lambda: TunnelRegistry(state_path=state),
        )
        sent: list[tuple[int, int]] = []
        real_kill = os.kill

        def spy_kill(pid: int, sig: int) -> None:
            if sig != 0:
                sent.append((pid, sig))
            if sig == 0:
                real_kill(pid, sig)

        with monkeypatch.context() as mp:
            mp.setattr(os, "kill", spy_kill)
            result = CliRunner().invoke(tunnel_group, ["stop", "--all"])
        assert result.exit_code == 0, result.output
        assert sent == [], f"tunnel stop --all must not SIGTERM guarded/reused PIDs, sent={sent}"
        assert proc.poll() is None
        assert TunnelRegistry(state_path=state).list_active() == []
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_stop_active_tunnels_skips_guarded_and_reused_pids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``stop_cmd.stop_active_tunnels`` must skip pid=1, self, and unrelated live PIDs."""
    import subprocess
    import sys

    from bernstein.cli.commands.stop_cmd import stop_active_tunnels

    state = tmp_path / "tunnels.json"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        tunnels = [
            {"name": "init", "provider": "cloudflared", "port": 3000, "public_url": "https://a.example", "pid": 1},
            {
                "name": "self",
                "provider": "cloudflared",
                "port": 3001,
                "public_url": "https://b.example",
                "pid": os.getpid(),
            },
            {
                "name": "reused",
                "provider": "cloudflared",
                "port": 3002,
                "public_url": "https://c.example",
                "pid": proc.pid,
            },
        ]
        state.write_text(json.dumps({"tunnels": tunnels}), encoding="utf-8")
        monkeypatch.setattr(
            "bernstein.core.tunnels.registry.STATE_PATH",
            state,
        )
        sent: list[tuple[int, int]] = []
        real_kill = os.kill

        def spy_kill(pid: int, sig: int) -> None:
            if sig != 0:
                sent.append((pid, sig))
            if sig == 0:
                real_kill(pid, sig)

        with monkeypatch.context() as mp:
            mp.setattr(os, "kill", spy_kill)
            count = stop_active_tunnels()
        assert count == 3
        assert sent == [], f"stop_active_tunnels must not SIGTERM guarded/reused PIDs, sent={sent}"
        assert proc.poll() is None
        assert TunnelRegistry(state_path=state).list_active() == []
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_is_signallable_tunnel_pid_and_terminate_guards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct unit coverage for bool, <=1, self, pgrp, dead/zombie, empty-provider, and cmdline mismatch."""
    from bernstein.core.tunnels.process_guard import (
        is_signallable_tunnel_pid,
        terminate_tunnel_process,
    )

    assert is_signallable_tunnel_pid(False) is False
    assert is_signallable_tunnel_pid(True) is False
    assert is_signallable_tunnel_pid(-41001) is False
    assert is_signallable_tunnel_pid(-1) is False
    assert is_signallable_tunnel_pid(0) is False
    assert is_signallable_tunnel_pid(1) is False
    assert is_signallable_tunnel_pid(os.getpid()) is False
    if hasattr(os, "getpgrp"):
        assert is_signallable_tunnel_pid(os.getpgrp()) is False

    # Dead / zombie process skipped cleanly.
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr("bernstein.core.tunnels.process_guard._PROC_ROOT", tmp_path / "no-proc")
    monkeypatch.setattr("bernstein.core.tunnels.process_guard.IS_WINDOWS", False)
    monkeypatch.setattr("bernstein.core.tunnels.process_guard._ps_comm", lambda _pid: None)
    monkeypatch.setattr("bernstein.core.tunnels.process_guard.os.kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr("bernstein.core.tunnels.process_guard.is_process_alive", lambda _pid: False)
    handle = TunnelHandle(name="dead", provider="cloudflared", port=8000, public_url="https://x.example", pid=54321)
    assert terminate_tunnel_process(handle) is False
    assert sent == []

    # Empty provider fails closed even when process is alive.
    monkeypatch.setattr("bernstein.core.tunnels.process_guard.is_process_alive", lambda _pid: True)
    empty_provider = TunnelHandle(name="empty", provider="", port=8000, public_url="https://x.example", pid=54321)
    assert terminate_tunnel_process(empty_provider) is False
    assert sent == []

    # Matching cmdline delivers SIGTERM; non-matching cmdline skips.
    monkeypatch.setattr(
        "bernstein.core.tunnels.process_guard.pid_command_line",
        lambda _pid: "python3 -m http.server 8000",
    )
    assert terminate_tunnel_process(handle) is False
    assert sent == []

    monkeypatch.setattr(
        "bernstein.core.tunnels.process_guard.pid_command_line",
        lambda _pid: "/usr/local/bin/cloudflared tunnel --url http://localhost:8000",
    )
    assert terminate_tunnel_process(handle) is True
    assert sent == [(54321, signal.SIGTERM)]


def test_terminate_fails_closed_when_cmdline_unreadable_and_reports_in_stop_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unreadable command lines (missing ps, Windows, ps failure) must fail closed and report it."""
    from click.testing import CliRunner

    from bernstein.cli.commands import stop_cmd
    from bernstein.cli.commands.tunnel_cmd import tunnel_group
    from bernstein.core.tunnels import process_guard
    from bernstein.gui import cli as gui_cli

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(process_guard, "_PROC_ROOT", tmp_path / "no-proc", raising=False)
    monkeypatch.setattr(process_guard, "IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(process_guard.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(process_guard, "is_process_alive", lambda _pid: True)
    monkeypatch.setattr(process_guard, "pid_command_line", lambda _pid: None)

    handle = TunnelHandle(name="stale", provider="cloudflared", port=8052, public_url="https://x.example", pid=54321)
    assert process_guard.terminate_tunnel_process(handle) is False
    assert sent == [], f"must not signal when cmdline is None, sent={sent}"

    # Windows must never signal without a verified process identity.
    monkeypatch.setattr(process_guard, "IS_WINDOWS", True, raising=False)
    assert process_guard.terminate_tunnel_process(handle) is False
    assert sent == [], f"must not signal on Windows without verified identity, sent={sent}"
    monkeypatch.setattr(process_guard, "IS_WINDOWS", False, raising=False)

    # Real `pid_command_line` with `ps` missing from PATH (slim container without procfs/ps) fails closed.
    from bernstein.core.orchestration.process_utils import pid_command_line as real_pid_command_line

    with monkeypatch.context() as mp:
        mp.setattr(process_guard, "pid_command_line", real_pid_command_line)
        mp.setenv("PATH", "")
        assert process_guard.terminate_tunnel_process(handle) is False
    assert sent == [], f"must not signal when ps is missing from PATH, sent={sent}"

    # `bernstein tunnel stop <name>` and `--all` must report that PID was not signaled.
    state = tmp_path / "tunnels.json"
    entry = {
        "name": "stale-cf",
        "provider": "cloudflared",
        "port": 8052,
        "public_url": "https://x.example",
        "pid": 54321,
    }
    state.write_text(json.dumps({"tunnels": [entry]}), encoding="utf-8")
    monkeypatch.setattr("bernstein.cli.commands.tunnel_cmd.TunnelRegistry", lambda: TunnelRegistry(state_path=state))
    res_single = CliRunner().invoke(tunnel_group, ["stop", "stale-cf"])
    assert res_single.exit_code == 0, res_single.output
    assert sent == []
    assert "command line" in res_single.output.lower(), res_single.output
    assert "did not signal" in res_single.output.lower(), res_single.output
    assert TunnelRegistry(state_path=state).list_active() == []

    state.write_text(json.dumps({"tunnels": [entry]}), encoding="utf-8")
    res_all = CliRunner().invoke(tunnel_group, ["stop", "--all"])
    assert res_all.exit_code == 0, res_all.output
    assert sent == []
    assert "command line" in res_all.output.lower(), res_all.output
    assert "did not signal" in res_all.output.lower(), res_all.output
    assert TunnelRegistry(state_path=state).list_active() == []

    # `stop_active_tunnels()` reports unreadable command line on console and removes entry.
    state.write_text(json.dumps({"tunnels": [entry]}), encoding="utf-8")
    monkeypatch.setattr("bernstein.core.tunnels.registry.STATE_PATH", state)
    printed: list[str] = []
    monkeypatch.setattr(stop_cmd.console, "print", lambda *args, **_kw: printed.append(" ".join(str(a) for a in args)))
    assert stop_cmd.stop_active_tunnels() == 1
    assert sent == []
    assert any("command line" in line.lower() for line in printed), printed
    assert TunnelRegistry(state_path=state).list_active() == []

    # `gui_cli._stop_tunnel()` warns on stderr and removes entry.
    state.write_text(json.dumps({"tunnels": [entry]}), encoding="utf-8")
    monkeypatch.setattr(gui_cli, "_build_tunnel_registry", lambda: TunnelRegistry(state_path=state))
    capsys.readouterr()
    gui_cli._stop_tunnel("stale-cf")
    captured = capsys.readouterr()
    assert sent == []
    assert "command line" in captured.err.lower(), captured
    assert TunnelRegistry(state_path=state).list_active() == []


def test_terminate_reads_proc_cmdline_first_and_matches_real_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux `/proc/<pid>/cmdline` is read before `ps`, matching real providers and interpreter wrappers."""
    from bernstein.core.tunnels import process_guard

    fake_proc = tmp_path / "proc"
    proc_cases: list[tuple[int, str, bytes, bool]] = [
        (54321, "cloudflared", b"/usr/local/bin/cloudflared\x00tunnel\x00--url\x00http://localhost:8052\x00", True),
        (54322, "bore", b"/opt/homebrew/bin/bore\x00local\x008052\x00--to\x00bore.pub\x00", True),
        (54323, "ngrok", b"/usr/bin/ngrok\x00http\x008052\x00--log\x00stdout\x00", True),
        (
            54324,
            "cloudflared",
            b"/bin/sh\x00/usr/local/bin/cloudflared\x00tunnel\x00--url\x00http://localhost:8052\x00",
            True,
        ),
        (54325, "bore", b"/usr/bin/env\x00bash\x00/opt/bin/bore\x00local\x008052\x00", True),
        (54326, "ngrok", b"/usr/bin/python3\x00-u\x00/usr/local/bin/ngrok\x00http\x008052\x00", True),
        (54327, "bore", b"/usr/bin/python3\x00/srv/laborer/worker.py\x00", False),
        (54328, "ngrok", b"C:\\Program Files\\ngrok\\ngrok.exe\x00http\x008052\x00", True),
    ]
    for pid, _provider, raw, _expect in proc_cases:
        pid_dir = fake_proc / str(pid)
        pid_dir.mkdir(parents=True)
        (pid_dir / "cmdline").write_bytes(raw)

    sent: list[tuple[int, int]] = []
    ps_calls: list[int] = []
    monkeypatch.setattr(process_guard, "_PROC_ROOT", fake_proc, raising=False)
    monkeypatch.setattr(process_guard, "IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(process_guard.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(process_guard, "is_process_alive", lambda _pid: True)
    monkeypatch.setattr(process_guard, "pid_command_line", lambda pid: ps_calls.append(pid) or None)
    monkeypatch.setattr(process_guard, "_ps_comm", lambda _pid: None)

    expected_killed: list[tuple[int, int]] = []
    for pid, provider, _raw, should_kill in proc_cases:
        handle = TunnelHandle(name=f"t-{pid}", provider=provider, port=8052, public_url="https://x.example", pid=pid)
        assert process_guard.terminate_tunnel_process(handle) is should_kill, (pid, provider)
        if should_kill:
            expected_killed.append((pid, signal.SIGTERM))

    assert sent == expected_killed
    assert ps_calls == [], f"/proc/<pid>/cmdline should be preferred without calling ps, ps_calls={ps_calls}"

    # Fallback to `ps` when `/proc` is absent still matches real cloudflared/bore/ngrok argv[0] & wrappers.
    fallback_cases: list[tuple[str, str, str | None]] = [
        ("cloudflared", "cloudflared tunnel --url http://localhost:8052", None),
        ("bore", "/opt/homebrew/bin/bore local 8052 --to bore.pub", None),
        ("ngrok", "/usr/local/bin/ngrok http 8052 --log stdout", None),
        ("bore", "/bin/sh /usr/local/bin/bore local 8052", None),
        (
            "cloudflared",
            "/Applications/My Tools/cloudflared tunnel --url http://localhost:8052",
            "/Applications/My Tools/cloudflared",
        ),
    ]
    for idx, (provider, cmdline, comm) in enumerate(fallback_cases, start=55000):
        sent.clear()
        monkeypatch.setattr(process_guard, "pid_command_line", lambda _pid, c=cmdline: c)
        monkeypatch.setattr(process_guard, "_ps_comm", lambda _pid, cm=comm: cm)
        handle = TunnelHandle(name=f"fb-{idx}", provider=provider, port=8052, public_url="https://x.example", pid=idx)
        assert process_guard.terminate_tunnel_process(handle) is True, (provider, cmdline)
        assert sent == [(idx, signal.SIGTERM)]


def test_terminate_rejects_substring_false_positives_laborer_and_tunnel_provider_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Substring hits like `/srv/laborer/worker.py` or `--tunnel-provider cloudflared` must not match."""
    import subprocess
    import sys

    from bernstein.core.tunnels import process_guard

    # 1. Direct command-line unit cases (ps fallback, no /proc).
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(process_guard, "_PROC_ROOT", tmp_path / "no-proc", raising=False)
    monkeypatch.setattr(process_guard, "IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(process_guard.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(process_guard, "is_process_alive", lambda _pid: True)
    monkeypatch.setattr(process_guard, "_ps_comm", lambda _pid: None)

    false_positives: list[tuple[str, str]] = [
        ("bore", "/usr/bin/python3 /srv/laborer/worker.py"),
        ("bore", "/srv/laborer/worker.py"),
        ("bore", "/usr/bin/python3 /tmp/bore_helper.py"),
        ("bore", "/bin/sh -c 'sleep 60 # bore'"),
        ("cloudflared", "/usr/local/bin/bernstein gui serve --tunnel --tunnel-provider cloudflared"),
        ("cloudflared", "/usr/bin/python3 -m bernstein gui serve --tunnel --tunnel-provider cloudflared"),
        ("cloudflared", "/usr/bin/python3 -c 'import time; time.sleep(60)' --tunnel-provider cloudflared"),
        ("ngrok", "/usr/bin/python3 -m pytest -k ngrok"),
    ]
    for idx, (provider, cmdline) in enumerate(false_positives, start=56000):
        monkeypatch.setattr(process_guard, "pid_command_line", lambda _pid, c=cmdline: c)
        handle = TunnelHandle(name=f"fp-{idx}", provider=provider, port=8052, public_url="https://x.example", pid=idx)
        assert process_guard.terminate_tunnel_process(handle) is False, (provider, cmdline)
    assert sent == [], f"false-positive command lines must not be signaled, sent={sent}"

    # 2. Live unrelated subprocesses whose path/args contain provider substrings.
    laborer_dir = tmp_path / "laborer"
    laborer_dir.mkdir()
    laborer_worker = laborer_dir / "worker.py"
    laborer_worker.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")

    proc_laborer = subprocess.Popen([sys.executable, str(laborer_worker)])
    proc_flag = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "--tunnel-provider", "cloudflared"]
    )
    try:
        monkeypatch.undo()
        live_sent: list[tuple[int, int]] = []
        real_kill = os.kill

        def spy_kill(pid: int, sig: int) -> None:
            if sig != 0:
                live_sent.append((pid, sig))
                return
            real_kill(pid, sig)

        with monkeypatch.context() as mp:
            mp.setattr(os, "kill", spy_kill)
            bore_handle = TunnelHandle(
                name="stale-bore",
                provider="bore",
                port=8052,
                public_url="tcp://bore.pub:1234",
                pid=proc_laborer.pid,
            )
            cf_handle = TunnelHandle(
                name="stale-cf",
                provider="cloudflared",
                port=8052,
                public_url="https://stale.example",
                pid=proc_flag.pid,
            )
            assert process_guard.terminate_tunnel_process(bore_handle) is False
            assert process_guard.terminate_tunnel_process(cf_handle) is False

        assert live_sent == [], f"unrelated live subprocesses must not be signaled, sent={live_sent}"
        assert proc_laborer.poll() is None
        assert proc_flag.poll() is None
    finally:
        for proc in (proc_laborer, proc_flag):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
