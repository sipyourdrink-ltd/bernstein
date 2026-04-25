"""Unit tests for :mod:`bernstein.core.preview.tunnel_bridge`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bernstein.core.preview.tunnel_bridge import TunnelBridge, TunnelBridgeError
from bernstein.core.tunnels.protocol import (
    ProviderNotAvailable,
    TunnelHandle,
    TunnelProvider,
)
from bernstein.core.tunnels.registry import TunnelRegistry


class _FakeProvider(TunnelProvider):
    """In-memory tunnel provider for bridge tests."""

    def __init__(self, name: str, *, available: bool = True) -> None:
        self.name = name
        self.binary = name
        self._available = available
        self.started: list[tuple[int, str]] = []
        self.stopped: list[str] = []

    def detect(self) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    def start(self, port: int, name: str) -> TunnelHandle:
        if not self._available:
            raise ProviderNotAvailable(f"{self.name} not available", hint="install it")
        self.started.append((port, name))
        return TunnelHandle(
            name=name,
            provider=self.name,
            port=port,
            public_url=f"https://{self.name}.example.com",
            pid=0,
        )

    def stop(self, name: str) -> None:
        self.stopped.append(name)


def _bridge_with(*providers: TunnelProvider, state_path: Path) -> TunnelBridge:
    def factory() -> TunnelRegistry:
        reg = TunnelRegistry(state_path=state_path)
        for prov in providers:
            reg.register(prov)
        return reg

    return TunnelBridge(registry_factory=factory)


def test_open_calls_registry_with_explicit_provider(tmp_path: Path) -> None:
    cf = _FakeProvider("cloudflared")
    bridge = _bridge_with(cf, state_path=tmp_path / "tunnels.json")
    handle = bridge.open(port=5173, provider="cloudflared", name="prv-1")
    assert handle.provider == "cloudflared"
    assert cf.started == [(5173, "prv-1")]


def test_open_raises_when_explicit_provider_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing binary on an explicit provider produces a TunnelBridgeError."""
    bad = _FakeProvider("ngrok", available=False)
    bridge = _bridge_with(bad, state_path=tmp_path / "tunnels.json")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/ngrok")
    with pytest.raises(TunnelBridgeError):
        bridge.open(port=8080, provider="ngrok", name="x")


def test_open_falls_back_to_cloudflared_when_auto_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``auto`` with no binary on PATH retries cloudflared explicitly."""
    cf = _FakeProvider("cloudflared")

    def factory() -> TunnelRegistry:
        # Fresh registry per call so the first ``create`` (auto) sees a
        # registry that *can't* resolve a binary, while the retry path
        # sees one with cloudflared accepted.
        reg = TunnelRegistry(state_path=tmp_path / "tunnels.json")
        reg.register(cf)
        return reg

    # First call must raise ProviderNotAvailable from auto-pick (no binary on PATH).
    # Second call (provider="cloudflared") must succeed.
    monkeypatch.setattr("shutil.which", lambda _: None)
    bridge = TunnelBridge(registry_factory=factory)

    # Patch the auto-pick to fail by removing PATH lookup, but keep the
    # explicit "cloudflared" path open by stubbing the provider's start.
    # Simpler: the registry's auto-pick fails when shutil.which returns
    # None, and explicit provider lookup ignores PATH — so we just call
    # bridge.open with provider="auto" and rely on the fallback.
    handle = bridge.open(port=4321, provider="auto", name="fallback")
    assert handle.provider == "cloudflared"
    assert cf.started == [(4321, "fallback")]


def test_close_destroys_tunnel(tmp_path: Path) -> None:
    cf = _FakeProvider("cloudflared")
    bridge = _bridge_with(cf, state_path=tmp_path / "tunnels.json")
    bridge.open(port=5173, provider="cloudflared", name="prv-2")
    assert bridge.close("prv-2") is True
    assert cf.stopped == ["prv-2"]


def test_close_returns_false_for_unknown_tunnel(tmp_path: Path) -> None:
    cf = _FakeProvider("cloudflared")
    bridge = _bridge_with(cf, state_path=tmp_path / "tunnels.json")
    assert bridge.close("nope") is False
