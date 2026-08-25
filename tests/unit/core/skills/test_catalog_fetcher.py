"""Skill catalog fetcher URL-safety tests.

Verifies that the skills-catalog fetcher rejects internal-host destinations
(loopback, link-local, private) before any HTTP request is made, by routing
its scheme check through ``ensure_public_http_url``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.security.url_allowlist import UrlSchemeError, ensure_public_http_url
from bernstein.core.skills.catalog.fetcher import (
    HTTPResponse,
    SkillCatalogFetcher,
)


def _good_catalog() -> dict[str, Any]:
    """A minimal valid skills catalog payload."""
    return {
        "version": 1,
        "generated_at": "2026-05-21T00:00:00Z",
        "entries": [
            {
                "id": "code-review",
                "name": "code-review",
                "version": "1.0.0",
                "description": "Review code.",
                "source": {
                    "kind": "github",
                    "repo": "acme/code-review",
                    "tag": "v1.0.0",
                },
                "content_digest": "f" * 64,
                "verified": True,
            }
        ],
    }


class _FakeTransport:
    """Records calls and returns queued responses.

    Mirrors :class:`_UrllibTransport` by running the URL guard first, so a
    hostile destination is rejected before any request is recorded.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.queue: list[HTTPResponse] = []

    def push(self, resp: HTTPResponse) -> None:
        self.queue.append(resp)

    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        ensure_public_http_url(url, allow_http=False, source="skills_catalog.fetcher")
        self.calls.append((url, headers.copy()))
        if not self.queue:
            raise AssertionError(f"unexpected request to {url}")
        return self.queue.pop(0)


def _build_fetcher(
    tmp_path: Path,
    transport: _FakeTransport,
    *,
    primary_url: str = "https://bernstein.run/skills-catalog.json",
) -> SkillCatalogFetcher:
    return SkillCatalogFetcher(
        primary_url=primary_url,
        mirror_url="https://mirror.example/skills-catalog.json",
        cache_path=tmp_path / "catalog.json",
        revalidate_seconds=600,
        transport=transport,
    )


def test_hostile_catalog_url_is_rejected_before_fetch(tmp_path: Path) -> None:
    transport = _FakeTransport()
    fetcher = _build_fetcher(
        tmp_path,
        transport,
        primary_url="https://127.0.0.1/skills.json",
    )
    with pytest.raises(UrlSchemeError):
        fetcher.fetch()
    assert transport.calls == []


def test_public_catalog_url_fetches_successfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeTransport()
    body = json.dumps(_good_catalog()).encode()
    transport.push(HTTPResponse(status=200, body=body, etag='"abc"'))
    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        lambda host: ["93.184.216.34"],
    )

    fetcher = _build_fetcher(tmp_path, transport)
    result = fetcher.fetch()

    assert result.from_cache is False
    assert result.catalog.entries[0].id == "code-review"
    assert len(transport.calls) == 1


def test_link_local_metadata_url_is_rejected(tmp_path: Path) -> None:
    transport = _FakeTransport()
    fetcher = _build_fetcher(
        tmp_path,
        transport,
        primary_url="https://169.254.169.254/latest/meta-data/",
    )
    with pytest.raises(UrlSchemeError):
        fetcher.fetch()
    assert transport.calls == []


def test_rebinding_hostname_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate a hostname that resolves to a private address (rebinding attack)
    def resolver(host: str) -> list[str]:
        if host == "rebind.example":
            # Return a private IP (loopback) and a public IP
            return ["127.0.0.1", "93.184.216.34"]
        return ["93.184.216.34"]  # default to public for any other host

    monkeypatch.setattr(
        "bernstein.core.security.url_allowlist._default_resolver",
        resolver,
    )

    transport = _FakeTransport()
    fetcher = _build_fetcher(
        tmp_path,
        transport,
        primary_url="https://rebind.example/skills.json",
    )
    with pytest.raises(UrlSchemeError):
        fetcher.fetch()
    assert transport.calls == []
