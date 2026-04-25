"""ETag-aware fetcher behaviour tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.protocols.mcp_catalog.fetcher import (
    CatalogFetcher,
    HTTPResponse,
)
from bernstein.core.protocols.mcp_catalog.manifest import CatalogValidationError


def _good_catalog() -> dict[str, Any]:
    return {
        "version": 1,
        "generated_at": "2026-04-25T12:00:00Z",
        "entries": [
            {
                "id": "fs-readonly",
                "name": "FS",
                "description": "fs",
                "homepage": "https://x",
                "repository": "https://x.git",
                "install_command": ["true"],
                "version_pin": "1.0.0",
                "transports": ["stdio"],
                "verified_by_bernstein": True,
            }
        ],
    }


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.queue: list[HTTPResponse] = []

    def push(self, resp: HTTPResponse) -> None:
        self.queue.append(resp)

    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        self.calls.append((url, dict(headers)))
        if not self.queue:
            raise AssertionError(f"unexpected request to {url}")
        return self.queue.pop(0)


def _build_fetcher(tmp_path: Path, transport: _FakeTransport, **kwargs: Any) -> CatalogFetcher:
    return CatalogFetcher(
        primary_url="https://primary.example/mcp.json",
        mirror_url="https://mirror.example/mcp.json",
        cache_path=tmp_path / "catalog.json",
        revalidate_seconds=kwargs.get("revalidate_seconds", 600),
        transport=transport,
    )


def test_first_fetch_caches_with_etag(tmp_path: Path) -> None:
    transport = _FakeTransport()
    body = json.dumps(_good_catalog()).encode()
    transport.push(HTTPResponse(status=200, body=body, etag='"abc"'))

    fetcher = _build_fetcher(tmp_path, transport)
    result = fetcher.fetch()

    assert result.from_cache is False
    assert result.revalidated is True
    cache = json.loads(fetcher.cache_path.read_text())
    assert cache["etag"] == '"abc"'
    assert cache["catalog"]["entries"][0]["id"] == "fs-readonly"


def test_fresh_cache_skips_network(tmp_path: Path) -> None:
    transport = _FakeTransport()
    body = json.dumps(_good_catalog()).encode()
    transport.push(HTTPResponse(status=200, body=body, etag='"abc"'))

    fetcher = _build_fetcher(tmp_path, transport, revalidate_seconds=3600)
    fetcher.fetch()
    assert len(transport.calls) == 1
    # Within freshness window: no network call.
    fetcher.fetch()
    assert len(transport.calls) == 1


def test_stale_cache_revalidates_and_handles_304(tmp_path: Path) -> None:
    transport = _FakeTransport()
    body = json.dumps(_good_catalog()).encode()
    transport.push(HTTPResponse(status=200, body=body, etag='"abc"'))

    fetcher = _build_fetcher(tmp_path, transport, revalidate_seconds=3600)
    fetcher.fetch()

    # Force the cache to look stale by rewriting fetched_at.
    cache_data = json.loads(fetcher.cache_path.read_text())
    cache_data["fetched_at"] = (datetime.now(tz=UTC) - timedelta(days=1)).isoformat()
    fetcher.cache_path.write_text(json.dumps(cache_data))

    transport.push(HTTPResponse(status=304, body=b"", etag='"abc"'))
    result = fetcher.fetch()
    assert result.from_cache is True
    assert result.revalidated is True
    headers = transport.calls[-1][1]
    assert headers["If-None-Match"] == '"abc"'


def test_5xx_falls_back_to_mirror(tmp_path: Path) -> None:
    transport = _FakeTransport()
    transport.push(HTTPResponse(status=502, body=b"", etag=None))
    body = json.dumps(_good_catalog()).encode()
    transport.push(HTTPResponse(status=200, body=body, etag='"mirror"'))

    fetcher = _build_fetcher(tmp_path, transport)
    result = fetcher.fetch()
    assert result.source_url == "https://mirror.example/mcp.json"
    assert result.from_cache is False


def test_invalid_payload_preserves_cache(tmp_path: Path) -> None:
    transport = _FakeTransport()
    # First successful fetch primes the cache.
    transport.push(
        HTTPResponse(
            status=200, body=json.dumps(_good_catalog()).encode(), etag='"v1"'
        )
    )
    fetcher = _build_fetcher(tmp_path, transport, revalidate_seconds=1)
    fetcher.fetch()
    cached_text = fetcher.cache_path.read_text()

    # Force stale and serve a malformed payload (extra field).
    cache_data = json.loads(cached_text)
    cache_data["fetched_at"] = (datetime.now(tz=UTC) - timedelta(days=1)).isoformat()
    fetcher.cache_path.write_text(json.dumps(cache_data))

    bad = _good_catalog()
    bad["unknown_field"] = True
    transport.push(
        HTTPResponse(status=200, body=json.dumps(bad).encode(), etag='"v2"')
    )
    with pytest.raises(CatalogValidationError):
        fetcher.fetch()

    # Cache still contains the previous good copy.
    after = json.loads(fetcher.cache_path.read_text())
    assert "unknown_field" not in after["catalog"]
    assert after["catalog"]["entries"][0]["id"] == "fs-readonly"


def test_fetch_raises_when_no_cache_and_4xx(tmp_path: Path) -> None:
    transport = _FakeTransport()
    transport.push(HTTPResponse(status=404, body=b"", etag=None))
    fetcher = _build_fetcher(tmp_path, transport)
    with pytest.raises(RuntimeError, match="HTTP 404"):
        fetcher.fetch()
    assert not fetcher.cache_path.exists()
