"""Conditional-fetch behaviour of the MCP catalog fetcher.

The fetcher must not download a body it already holds: within the
revalidation window it makes no request at all, and after the window it
sends ``If-None-Match`` and treats ``304`` as "use what you have". That
holds for a body that failed schema validation too. A rejected body is
remembered by its ``ETag`` so an unchanged invalid document is neither
re-downloaded nor re-parsed on every run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.protocols.mcp_catalog.fetcher import (
    DEFAULT_REVALIDATE_SECONDS,
    CatalogFetcher,
    HTTPResponse,
    default_cache_path,
)
from bernstein.core.protocols.mcp_catalog.manifest import CatalogValidationError

_PRIMARY = "https://primary.example/mcp-catalog.json"
_MIRROR = "https://mirror.example/mcp-catalog.json"


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


def _not_a_catalog() -> dict[str, Any]:
    """A well-formed JSON document of a different kind (no ``entries``)."""
    return {"name": "some-server", "version": "1.0.0", "tools": []}


class _RecordingTransport:
    """Returns queued responses and records every request it sees."""

    def __init__(self, *responses: HTTPResponse) -> None:
        self.queue = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def push(self, resp: HTTPResponse) -> None:
        self.queue.append(resp)

    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        self.calls.append((url, dict(headers)))
        if not self.queue:
            raise AssertionError(f"unexpected request to {url}")
        return self.queue.pop(0)


def _fetcher(cache_path: Path, transport: _RecordingTransport) -> CatalogFetcher:
    return CatalogFetcher(
        primary_url=_PRIMARY,
        mirror_url=_MIRROR,
        cache_path=cache_path,
        revalidate_seconds=DEFAULT_REVALIDATE_SECONDS,
        transport=transport,
    )


def _ok(body: dict[str, Any], etag: str | None) -> HTTPResponse:
    return HTTPResponse(status=200, body=json.dumps(body).encode(), etag=etag)


_NOT_MODIFIED = HTTPResponse(status=304, body=b"", etag=None)


def test_within_ttl_no_request_after_ttl_conditional_304_uses_cache(tmp_path: Path) -> None:
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    transport = _RecordingTransport(_ok(_good_catalog(), 'W/"v1"'))
    fetcher = _fetcher(tmp_path / "mcp-catalog.json", transport)

    first = fetcher.fetch(now=t0)
    assert first.from_cache is False
    assert "If-None-Match" not in transport.calls[0][1]

    second = fetcher.fetch(now=t0 + timedelta(hours=1))
    assert len(transport.calls) == 1, "a fetch inside the window must not touch the network"
    assert second.from_cache is True
    assert second.revalidated is False

    transport.push(_NOT_MODIFIED)
    third = fetcher.fetch(now=t0 + timedelta(seconds=DEFAULT_REVALIDATE_SECONDS + 1))
    assert len(transport.calls) == 2
    assert transport.calls[1][1]["If-None-Match"] == 'W/"v1"'
    assert third.from_cache is True
    assert third.revalidated is True
    assert [e.id for e in third.catalog.entries] == ["fs-readonly"]

    # The 304 restarts the window: the next run inside it stays offline.
    fetcher.fetch(now=t0 + timedelta(seconds=DEFAULT_REVALIDATE_SECONDS + 60))
    assert len(transport.calls) == 2


def test_rejected_body_is_not_downloaded_again_within_ttl(tmp_path: Path) -> None:
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    transport = _RecordingTransport(_ok(_not_a_catalog(), 'W/"card"'))
    fetcher = _fetcher(tmp_path / "mcp-catalog.json", transport)

    with pytest.raises(CatalogValidationError, match="entries"):
        fetcher.fetch(now=t0)
    with pytest.raises(CatalogValidationError, match="entries"):
        fetcher.fetch(now=t0 + timedelta(hours=1))

    assert len(transport.calls) == 1
    assert fetcher.cached() is None


def test_rejected_body_is_revalidated_conditionally_after_ttl(tmp_path: Path) -> None:
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    stale = t0 + timedelta(seconds=DEFAULT_REVALIDATE_SECONDS + 1)
    transport = _RecordingTransport(_ok(_not_a_catalog(), 'W/"card"'), _NOT_MODIFIED)
    fetcher = _fetcher(tmp_path / "mcp-catalog.json", transport)

    with pytest.raises(CatalogValidationError):
        fetcher.fetch(now=t0)
    with pytest.raises(CatalogValidationError, match="entries"):
        fetcher.fetch(now=stale)

    assert transport.calls[1][1]["If-None-Match"] == 'W/"card"'
    # The 304 restarts the window for the rejection as well.
    with pytest.raises(CatalogValidationError):
        fetcher.fetch(now=stale + timedelta(minutes=5))
    assert len(transport.calls) == 2


def test_valid_body_after_a_rejection_is_accepted_and_clears_it(tmp_path: Path) -> None:
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    transport = _RecordingTransport(_ok(_not_a_catalog(), 'W/"card"'), _ok(_good_catalog(), '"v2"'))
    fetcher = _fetcher(tmp_path / "mcp-catalog.json", transport)

    with pytest.raises(CatalogValidationError):
        fetcher.fetch(now=t0)
    result = fetcher.fetch(force=True, now=t0 + timedelta(minutes=1))

    assert result.from_cache is False
    assert transport.calls[1][1]["If-None-Match"] == 'W/"card"'
    fetcher.fetch(now=t0 + timedelta(minutes=2))
    assert len(transport.calls) == 2
    assert fetcher.cached() is not None


def test_rejection_keeps_the_previous_valid_catalog(tmp_path: Path) -> None:
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    stale = t0 + timedelta(seconds=DEFAULT_REVALIDATE_SECONDS + 1)
    transport = _RecordingTransport(_ok(_good_catalog(), '"v1"'), _ok(_not_a_catalog(), '"v2"'))
    fetcher = _fetcher(tmp_path / "mcp-catalog.json", transport)

    fetcher.fetch(now=t0)
    with pytest.raises(CatalogValidationError):
        fetcher.fetch(now=stale)

    cached = fetcher.cached()
    assert cached is not None
    assert [e.id for e in cached.entries] == ["fs-readonly"]


def test_cache_write_leaves_a_concurrent_writers_temp_file_alone(tmp_path: Path) -> None:
    """Parallel workers share one cache file, so the temp name must be unique."""
    cache = tmp_path / "mcp-catalog.json"
    other_writer_tmp = cache.with_suffix(".json.tmp")
    other_writer_tmp.write_text("in flight")
    fetcher = _fetcher(cache, _RecordingTransport(_ok(_good_catalog(), '"v1"')))

    fetcher.fetch()

    assert other_writer_tmp.read_text() == "in flight"
    assert json.loads(cache.read_text())["etag"] == '"v1"'


def test_unwritable_cache_does_not_fail_a_successful_fetch(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    fetcher = _fetcher(blocker / "mcp-catalog.json", _RecordingTransport(_ok(_good_catalog(), '"v1"')))

    result = fetcher.fetch()

    assert [e.id for e in result.catalog.entries] == ["fs-readonly"]


def test_default_cache_path_is_user_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BERNSTEIN_MCP_CATALOG_CACHE_PATH", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)

    assert default_cache_path() == tmp_path / "xdg" / "bernstein" / "mcp-catalog.json"


def test_default_cache_path_honours_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "shared" / "catalog.json"
    monkeypatch.setenv("BERNSTEIN_MCP_CATALOG_CACHE_PATH", str(override))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))

    assert default_cache_path() == override
    assert CatalogFetcher(transport=_RecordingTransport()).cache_path == override


def test_serve_startup_check_uses_a_fresh_cache_without_a_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``bernstein mcp serve`` runs once per agent; a fresh cache must keep it offline."""
    from bernstein.cli.commands import mcp_catalog_cmd

    cache = tmp_path / "cache" / "mcp-catalog.json"
    monkeypatch.setenv("BERNSTEIN_MCP_CATALOG_CACHE_PATH", str(cache))
    monkeypatch.setenv("BERNSTEIN_MCP_USER_CONFIG_PATH", str(tmp_path / "mcp.json"))
    monkeypatch.setenv("BERNSTEIN_MCP_CATALOG_AUDIT_DIR", str(tmp_path / "audit"))

    transport = _RecordingTransport(_ok(_good_catalog(), '"v1"'), _NOT_MODIFIED, _NOT_MODIFIED)
    real_fetcher = mcp_catalog_cmd.CatalogFetcher
    monkeypatch.setattr(
        mcp_catalog_cmd,
        "CatalogFetcher",
        lambda **kwargs: real_fetcher(transport=transport, **kwargs),
    )

    first = mcp_catalog_cmd.maybe_run_background_check(on_serve_startup=True)
    second = mcp_catalog_cmd.maybe_run_background_check(on_serve_startup=True)

    assert first["checked"] is True
    assert second["checked"] is True
    assert len(transport.calls) == 1
