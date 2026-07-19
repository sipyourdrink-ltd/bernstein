"""Unit tests for ``bernstein gui`` CLI helpers and subcommands (#1218).

Network-free: the tunnel registry is monkey-patched with an in-process
fake provider, and ``uvicorn.run`` is monkey-patched to a no-op so
``serve`` does not actually bind a port. The tests focus on:

* persistence of the onboarding payload to ``dashboard.passphrase``
* QR + passphrase echo block formatting
* ``qr --rotate`` regenerating credentials in place
* ``serve --tunnel`` happy path + error path (provider unavailable)
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from bernstein.core.tunnels.protocol import ProviderNotAvailable, TunnelHandle, TunnelProvider
from bernstein.gui import cli as gui_cli

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_passphrase_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Override the module-level default to a tmp location."""
    p = tmp_path / "dashboard.passphrase"
    monkeypatch.setattr(gui_cli, "PASSPHRASE_FILE", p)
    yield p


class _FakeProvider(TunnelProvider):
    """Module-level fake provider - installable into a registry by name."""

    def __init__(self, name: str = "cloudflared", base_pid: int = 41000) -> None:
        self.name = name
        self.binary = name
        self._pid = base_pid
        self.started: list[str] = []
        self.stopped: list[str] = []

    def detect(self) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    def start(self, port: int, name: str) -> TunnelHandle:
        self.started.append(name)
        self._pid += 1
        return TunnelHandle(
            name=name,
            provider=self.name,
            port=port,
            public_url="https://fake-cloudflared.example.com",
            pid=self._pid,
        )

    def stop(self, name: str) -> None:
        self.stopped.append(name)


@pytest.fixture
def tunnel_state_path(tmp_path: Path) -> Path:
    """Tmp-scoped tunnel registry state file.

    The registry persists via a plain ``Path``, so a relative state path
    would resolve against the CWD and dirty the repo root on every run.
    """
    return tmp_path / "tunnels.json"


@pytest.fixture
def fake_registry(monkeypatch: pytest.MonkeyPatch, tunnel_state_path: Path) -> _FakeProvider:
    """Replace ``_build_tunnel_registry`` with one wired to a fake provider."""
    fake = _FakeProvider()

    def _build() -> Any:
        from bernstein.core.tunnels.registry import TunnelRegistry

        reg = TunnelRegistry(state_path=tunnel_state_path)
        reg.register(fake)
        return reg

    monkeypatch.setattr(gui_cli, "_build_tunnel_registry", _build)
    return fake


# ---------------------------------------------------------------------------
# write_passphrase_file / read_passphrase_file
# ---------------------------------------------------------------------------


def test_write_passphrase_file_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "more" / "creds.json"
    gui_cli.write_passphrase_file(target, {"token": "a", "passphrase": "b"})
    assert target.exists()
    assert target.parent.is_dir()


def test_write_passphrase_file_permissions_0600(tmp_path: Path) -> None:
    target = tmp_path / "creds.json"
    gui_cli.write_passphrase_file(target, {"token": "a"})
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:o}"


def test_write_passphrase_file_atomic_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "creds.json"
    # Pre-existing payload that must survive a failed overwrite.
    gui_cli.write_passphrase_file(target, {"token": "old"})

    real_replace = os.replace

    def boom(src: str, dst: str) -> None:
        del src, dst
        raise RuntimeError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(RuntimeError):
        gui_cli.write_passphrase_file(target, {"token": "new"})
    # The old payload is still readable.
    monkeypatch.setattr(os, "replace", real_replace)
    parsed = gui_cli.read_passphrase_file(target)
    assert parsed is not None
    assert parsed["token"] == "old"


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "creds.json"
    payload = {"token": "tok", "passphrase": "amber-bridge", "url": "https://x/y"}
    gui_cli.write_passphrase_file(target, payload)
    parsed = gui_cli.read_passphrase_file(target)
    assert parsed == payload


def test_read_passphrase_file_missing_returns_none(tmp_path: Path) -> None:
    assert gui_cli.read_passphrase_file(tmp_path / "absent") is None


def test_read_passphrase_file_invalid_json_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json")
    assert gui_cli.read_passphrase_file(p) is None


def test_read_passphrase_file_non_dict_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text(json.dumps(["a", "b"]))
    assert gui_cli.read_passphrase_file(p) is None


# ---------------------------------------------------------------------------
# _print_onboarding
# ---------------------------------------------------------------------------


def test_print_onboarding_returns_full_block(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    out = gui_cli._print_onboarding(
        "https://x.example.com/ui/#t=abc",
        "amber-bridge-cedar-dunes-eagle-feather",
        echo=captured.append,
    )
    assert "Bernstein PWA onboarding" in out
    assert "https://x.example.com/ui/#t=abc" in out
    assert "amber-bridge" in out
    assert captured and captured[0] == out


def test_print_onboarding_default_calls_click_echo() -> None:
    runner = CliRunner()

    @click.command()
    def _cmd() -> None:
        gui_cli._print_onboarding("https://example.com", "alpha-bravo-charlie-delta-echo-foxtrot")

    result = runner.invoke(_cmd, [])
    assert result.exit_code == 0
    assert "alpha-bravo" in result.output
    assert "https://example.com" in result.output


def test_print_onboarding_contains_qr_dark_module() -> None:
    captured: list[str] = []
    gui_cli._print_onboarding("https://x.example.com", "ph-rase", echo=captured.append)
    text = captured[0]
    # Either real QR (full block) or fallback "QR rendering unavailable" diagnostic.
    assert ("██" in text) or ("QR rendering unavailable" in text)


# ---------------------------------------------------------------------------
# CLI: `bernstein gui qr`
# ---------------------------------------------------------------------------


def test_cli_qr_requires_url_or_persisted(tmp_passphrase_file: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(gui_cli.gui_group, ["qr"])
    assert result.exit_code != 0
    assert "No persisted onboarding credentials" in result.output


def test_cli_qr_with_explicit_url(tmp_passphrase_file: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        gui_cli.gui_group,
        ["qr", "--url", "https://demo.example.com"],
    )
    assert result.exit_code == 0, result.output
    assert "https://demo.example.com" in result.output


def test_cli_qr_explicit_url_persists_credentials(tmp_passphrase_file: Path) -> None:
    runner = CliRunner()
    runner.invoke(gui_cli.gui_group, ["qr", "--url", "https://demo.example.com"])
    parsed = gui_cli.read_passphrase_file(tmp_passphrase_file)
    assert parsed is not None
    assert parsed["url"].startswith("https://demo.example.com/ui/#t=")
    assert parsed["passphrase"]


def test_cli_qr_reuse_persisted(tmp_passphrase_file: Path) -> None:
    # Pre-seed credentials and ensure a second invocation re-prints them.
    gui_cli.write_passphrase_file(
        tmp_passphrase_file,
        {
            "url": "https://demo.example.com/ui/#t=stable-token",
            "passphrase": "amber-bridge-cedar-dunes-eagle-feather",
        },
    )
    runner = CliRunner()
    result = runner.invoke(gui_cli.gui_group, ["qr"])
    assert result.exit_code == 0, result.output
    assert "stable-token" in result.output
    assert "amber-bridge-cedar" in result.output


def test_cli_qr_rotate_changes_token(tmp_passphrase_file: Path) -> None:
    gui_cli.write_passphrase_file(
        tmp_passphrase_file,
        {
            "url": "https://demo.example.com/ui/#t=old-token",
            "passphrase": "stale-stale-stale-stale-stale-stale",
            "public_url": "https://demo.example.com",
        },
    )
    runner = CliRunner()
    result = runner.invoke(gui_cli.gui_group, ["qr", "--rotate"])
    assert result.exit_code == 0, result.output
    parsed = gui_cli.read_passphrase_file(tmp_passphrase_file)
    assert parsed is not None
    assert parsed["url"] != "https://demo.example.com/ui/#t=old-token"
    assert parsed["passphrase"] != "stale-stale-stale-stale-stale-stale"
    # Preserved metadata
    assert parsed.get("public_url") == "https://demo.example.com"


def test_cli_qr_passphrase_file_override(tmp_path: Path) -> None:
    custom = tmp_path / "custom.json"
    gui_cli.write_passphrase_file(
        custom,
        {"url": "https://custom.example.com/ui/#t=tok", "passphrase": "abc-def-ghi-jkl-mno-pqr"},
    )
    runner = CliRunner()
    result = runner.invoke(
        gui_cli.gui_group,
        ["qr", "--passphrase-file", str(custom)],
    )
    assert result.exit_code == 0, result.output
    assert "https://custom.example.com" in result.output


def test_cli_qr_rotate_with_url_strips_old_fragment(tmp_passphrase_file: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        gui_cli.gui_group,
        ["qr", "--url", "https://demo.example.com/ui/#t=old", "--rotate"],
    )
    assert result.exit_code == 0
    parsed = gui_cli.read_passphrase_file(tmp_passphrase_file)
    assert parsed is not None
    # The fresh token replaced the old fragment.
    assert "#t=old" not in parsed["url"]


# ---------------------------------------------------------------------------
# CLI: `bernstein gui serve --tunnel`
# ---------------------------------------------------------------------------


def _patch_uvicorn_noop(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Replace uvicorn.run with a no-op that records its call args."""
    import uvicorn

    calls: list[dict[str, Any]] = []

    def _fake_run(app: Any, **kwargs: Any) -> None:
        del app
        calls.append(kwargs)

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    return calls


def test_cli_serve_minimal_no_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_uvicorn_noop(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(gui_cli.gui_group, ["serve", "--minimal", "--no-open"])
    assert result.exit_code == 0, result.output
    assert "Bernstein GUI" in result.output


def test_cli_serve_with_tunnel_prints_onboarding(
    monkeypatch: pytest.MonkeyPatch, tmp_passphrase_file: Path, fake_registry: _FakeProvider
) -> None:
    _patch_uvicorn_noop(monkeypatch)
    runner = CliRunner()
    # Pass an explicit provider so the registry does not have to discover
    # a real ``cloudflared`` binary on PATH (CI runners do not ship one).
    result = runner.invoke(
        gui_cli.gui_group,
        ["serve", "--minimal", "--no-open", "--tunnel", "--tunnel-provider", "cloudflared"],
    )
    assert result.exit_code == 0, result.output
    assert "Tunnel (cloudflared) up" in result.output
    assert "Bernstein PWA onboarding" in result.output
    # Persisted on disk
    parsed = gui_cli.read_passphrase_file(tmp_passphrase_file)
    assert parsed is not None
    assert parsed["public_url"] == "https://fake-cloudflared.example.com"
    assert parsed["provider"] == "cloudflared"
    assert parsed["passphrase"]
    # Tunnel was started and torn down via stop.
    assert fake_registry.started, "tunnel was never started"


def test_cli_serve_with_tunnel_handles_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_passphrase_file: Path, tmp_path: Path
) -> None:
    _patch_uvicorn_noop(monkeypatch)

    def _build_empty() -> Any:
        from bernstein.core.tunnels.registry import TunnelRegistry

        return TunnelRegistry(state_path=tmp_path / "empty.json")

    monkeypatch.setattr(gui_cli, "_build_tunnel_registry", _build_empty)
    runner = CliRunner()
    result = runner.invoke(gui_cli.gui_group, ["serve", "--minimal", "--no-open", "--tunnel"])
    assert result.exit_code != 0
    assert "Tunnel start failed" in result.output


def test_start_tunnel_helper_creates_handle(fake_registry: _FakeProvider) -> None:
    handle = gui_cli._start_tunnel(port=8052, provider="cloudflared")
    assert handle.port == 8052
    assert handle.provider == "cloudflared"
    assert handle.public_url == "https://fake-cloudflared.example.com"


def test_start_tunnel_persists_outside_the_repo(fake_registry: _FakeProvider, tunnel_state_path: Path) -> None:
    """A relative state path resolves against the CWD and dirties the repo."""
    repo_root = Path(__file__).resolve().parents[2]
    assert tunnel_state_path.is_absolute(), f"state path must be absolute, got {tunnel_state_path}"
    assert not tunnel_state_path.resolve().is_relative_to(repo_root), (
        f"tunnel state {tunnel_state_path} would be written inside {repo_root}"
    )
    gui_cli._start_tunnel(port=8052, provider="cloudflared")
    assert tunnel_state_path.exists(), "state was not persisted to the tmp path"


def test_stop_tunnel_helper_idempotent(fake_registry: _FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    # Stop a name that was never started - should not raise.
    sent: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        del sig
        sent.append(pid)

    monkeypatch.setattr(os, "kill", fake_kill)
    gui_cli._stop_tunnel("unknown-name")
    assert sent == []


def test_stop_tunnel_helper_kills_and_destroys(fake_registry: _FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    handle = gui_cli._start_tunnel(port=9999, provider="cloudflared")

    sent: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        del sig
        sent.append(pid)

    monkeypatch.setattr(os, "kill", fake_kill)
    gui_cli._stop_tunnel(handle.name)
    assert sent == [handle.pid]


def test_stop_tunnel_helper_survives_oserror(fake_registry: _FakeProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    handle = gui_cli._start_tunnel(port=9999, provider="cloudflared")

    def fake_kill(pid: int, sig: int) -> None:
        del pid, sig
        raise OSError("dead")

    monkeypatch.setattr(os, "kill", fake_kill)
    # Should not raise - _stop_tunnel swallows OSError.
    gui_cli._stop_tunnel(handle.name)


# ---------------------------------------------------------------------------
# Local (no-tunnel) browser seed: authenticate the SPA on loopback
# ---------------------------------------------------------------------------
#
# The SPA's data panels call the SSO-gated general API (``/api/v1/agents``,
# ``/api/v1/tasks``, ...). That surface accepts the process ``BERNSTEIN_AUTH_TOKEN``
# bearer - a #2366 dashboard scoped token only unlocks ``/api/v1/dashboard/*``.
# Without seeding, a bare local ``serve`` opens ``/ui/`` with no token, so every
# panel 401s. These tests pin the seed decision at the helper and CLI level.


def test_resolve_local_open_url_seeds_configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "legacy-secret-xyz")
    url = gui_cli._resolve_local_open_url(host="127.0.0.1", port=8052, echo=lambda *_a: None)
    # The token rides the onboarding fragment the SPA parses on boot.
    assert url == "http://127.0.0.1:8052/ui/#t=legacy-secret-xyz"


def test_resolve_local_open_url_reuses_token_verbatim_no_mint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "  spaced-token  ")
    url = gui_cli._resolve_local_open_url(host="localhost", port=9000, echo=lambda *_a: None)
    # Whitespace is trimmed (matches create_app's resolution) but the token
    # itself is reused, never regenerated per serve.
    assert url == "http://localhost:9000/ui/#t=spaced-token"


def test_resolve_local_open_url_bare_and_hint_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    notes: list[str] = []
    url = gui_cli._resolve_local_open_url(host="127.0.0.1", port=8052, echo=notes.append)
    assert url == "http://127.0.0.1:8052/ui/"
    assert "#t=" not in url
    assert any("BERNSTEIN_AUTH_TOKEN" in n for n in notes), notes


def test_resolve_local_open_url_never_seeds_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    # A configured token must not be baked into a browser-open URL for a
    # routable bind - posture stays at bind-time, and the token never rides a
    # non-loopback URL.
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "legacy-secret-xyz")
    url = gui_cli._resolve_local_open_url(host="0.0.0.0", port=8052, echo=lambda *_a: None)
    assert url == "http://0.0.0.0:8052/ui/"
    assert "#t=" not in url


def test_cli_serve_local_open_seeds_browser_with_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_uvicorn_noop(monkeypatch)
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda u, *a, **k: opened.append(u) or True)
    monkeypatch.setenv("BERNSTEIN_AUTH_TOKEN", "legacy-secret-xyz")
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(gui_cli.gui_group, ["serve", "--minimal"])
    assert result.exit_code == 0, result.output
    assert opened, "browser was never opened"
    assert opened[0] == "http://127.0.0.1:8052/ui/#t=legacy-secret-xyz"
    # The token travels only in the opened browser URL fragment, never in the
    # console/log output.
    assert "legacy-secret-xyz" not in result.output


def test_cli_serve_local_open_bare_when_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_uvicorn_noop(monkeypatch)
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda u, *a, **k: opened.append(u) or True)
    monkeypatch.delenv("BERNSTEIN_AUTH_TOKEN", raising=False)
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(gui_cli.gui_group, ["serve", "--minimal"])
    assert result.exit_code == 0, result.output
    assert opened == ["http://127.0.0.1:8052/ui/"]


# ---------------------------------------------------------------------------
# Provider choice validation (Click choice)
# ---------------------------------------------------------------------------


def test_cli_serve_rejects_unknown_tunnel_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_uvicorn_noop(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        gui_cli.gui_group,
        ["serve", "--minimal", "--no-open", "--tunnel", "--tunnel-provider", "zoom"],
    )
    assert result.exit_code != 0
    assert "zoom" in result.output


def test_cli_provider_choices_match_constant() -> None:
    assert "cloudflared" in gui_cli.PROVIDER_CHOICES
    assert "ngrok" in gui_cli.PROVIDER_CHOICES
    assert "auto" in gui_cli.PROVIDER_CHOICES


def test_provider_not_available_error_propagates_hint() -> None:
    err = ProviderNotAvailable("not installed", hint="brew install cloudflared")
    assert err.hint == "brew install cloudflared"
