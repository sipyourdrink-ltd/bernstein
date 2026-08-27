"""MCP catalog fetcher SSRF protection tests.

Verifies that the MCP catalog fetcher rejects third-party-derived URLs
pointing to internal addresses (loopback, link-local, private ranges).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.protocols.mcp_catalog.fetcher import (
    CatalogFetcher,
    HTTPResponse,
)
from bernstein.core.security.url_allowlist import (
    UrlSchemeError,
    ensure_public_http_url,
)


class _FakeTransport:
    """Test double for HTTPTransport that runs the URL guard first.

    Mirrors :class:`_UrllibTransport` by running the URL guard before
    any request is recorded, so hostile destinations are rejected before
    the request is made.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.responses: list[HTTPResponse] = []

    def push(self, resp: HTTPResponse) -> None:
        self.responses.append(resp)

    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        # The URL guard runs before any request is recorded
        # This mirrors _UrllibTransport behavior
        ensure_public_http_url(url, allow_http=True, source="mcp_catalog.fetcher")
        self.calls.append((url, headers.copy()))
        if not self.responses:
            raise AssertionError(f"unexpected request to {url}")
        return self.responses.pop(0)


def _resolves_to(*addresses: str):
    """A resolver that answers with fixed addresses, so no DNS is needed."""
    return lambda _host: list(addresses)


def test_mcp_catalog_fetcher_rejects_loopback_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CatalogFetcher must reject catalog URLs pointing to loopback."""
    cache_path = tmp_path / "mcp-catalog.json"

    def resolver(host: str) -> list[str]:
        if host == "catalog.internal":
            return ["127.0.0.1"]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    fetcher = CatalogFetcher(
        primary_url="https://catalog.internal/mcp-catalog.json",
        cache_path=cache_path,
        transport=transport,
    )
    with pytest.raises(UrlSchemeError, match="internal address"):
        fetcher.fetch()

    assert transport.calls == []


def test_mcp_catalog_fetcher_rejects_link_local_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CatalogFetcher must reject catalog URLs pointing to link-local."""
    cache_path = tmp_path / "mcp-catalog.json"

    def resolver(host: str) -> list[str]:
        if host == "metadata.local":
            return ["169.254.169.254"]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    fetcher = CatalogFetcher(
        primary_url="https://metadata.local/mcp-catalog.json",
        cache_path=cache_path,
        transport=transport,
    )
    with pytest.raises(UrlSchemeError, match="internal address"):
        fetcher.fetch()

    assert transport.calls == []


def test_mcp_catalog_fetcher_rejects_private_v4_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CatalogFetcher must reject catalog URLs pointing to private IPv4."""
    cache_path = tmp_path / "mcp-catalog.json"

    def resolver(host: str) -> list[str]:
        if host == "private.repo":
            return ["10.0.0.5"]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    fetcher = CatalogFetcher(
        primary_url="https://private.repo/mcp-catalog.json",
        cache_path=cache_path,
        transport=transport,
    )
    with pytest.raises(UrlSchemeError, match="internal address"):
        fetcher.fetch()

    assert transport.calls == []


def test_mcp_catalog_fetcher_rejects_private_v6_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CatalogFetcher must reject catalog URLs pointing to private IPv6."""
    cache_path = tmp_path / "mcp-catalog.json"

    def resolver(host: str) -> list[str]:
        if host == "private.v6":
            return ["fc00::1"]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    fetcher = CatalogFetcher(
        primary_url="https://private.v6/mcp-catalog.json",
        cache_path=cache_path,
        transport=transport,
    )
    with pytest.raises(UrlSchemeError, match="internal address"):
        fetcher.fetch()

    assert transport.calls == []


def test_mcp_catalog_fetcher_rejects_rebinding_hostname(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CatalogFetcher must reject hostnames resolving to mixed public+internal."""
    cache_path = tmp_path / "mcp-catalog.json"

    def resolver(host: str) -> list[str]:
        if host == "rebind.example":
            return ["93.184.216.34", "127.0.0.1"]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    fetcher = CatalogFetcher(
        primary_url="https://rebind.example/mcp-catalog.json",
        cache_path=cache_path,
        transport=transport,
    )
    with pytest.raises(UrlSchemeError, match="internal address"):
        fetcher.fetch()

    assert transport.calls == []


def test_mcp_catalog_fetcher_allows_public_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CatalogFetcher must allow public catalog URLs."""
    cache_path = tmp_path / "mcp-catalog.json"

    def resolver(host: str) -> list[str]:
        if host == "example.com":
            return ["93.184.216.34"]
        return ["93.184.216.34"]

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    catalog_body = json.dumps(
        {
            "version": 1,
            "generated_at": "2026-05-21T00:00:00Z",
            "entries": [],
        }
    ).encode()
    transport.push(HTTPResponse(status=200, body=catalog_body, etag=None))

    fetcher = CatalogFetcher(
        primary_url="https://example.com/mcp-catalog.json",
        cache_path=cache_path,
        transport=transport,
    )

    result = fetcher.fetch()

    assert result.from_cache is False
    assert result.catalog is not None
    assert len(transport.calls) == 1
