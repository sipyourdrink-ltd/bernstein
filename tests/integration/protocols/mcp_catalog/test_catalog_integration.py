"""Integration test: end-to-end install with real audit log + real subprocess.

This test exercises the full pipeline (validate -> sandbox preview -> user
config write -> HMAC audit log) using a real Python subprocess as the
``install_command``. The subprocess writes a single text file inside the
sandbox so the diff capture is observable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bernstein.core.protocols.mcp_catalog.audit import CatalogAuditor
from bernstein.core.protocols.mcp_catalog.fetcher import (
    CatalogFetcher,
    HTTPResponse,
)
from bernstein.core.protocols.mcp_catalog.service import (
    CatalogService,
    CatalogServiceConfig,
)

CATALOG_PAYLOAD = {
    "version": 1,
    "generated_at": "2026-04-25T00:00:00Z",
    "entries": [
        {
            "id": "fs-readonly",
            "name": "fs-readonly",
            "description": "fs-readonly integration entry",
            "homepage": "https://example.com",
            "repository": "https://example.com/repo.git",
            "install_command": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('install-marker.txt').write_text('hi')",
            ],
            "version_pin": "1.0.0",
            "transports": ["stdio"],
            "verified_by_bernstein": True,
            "command": "fs-readonly",
            "args": ["--root", "/tmp"],
        }
    ],
}


class _OneShotTransport:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.calls = 0

    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        self.calls += 1
        return HTTPResponse(status=200, body=self._body, etag='"v1"')


@pytest.fixture
def isolated_audit_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use a tempdir-scoped audit key so the test never reads the host one."""
    key_path = tmp_path / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    return audit_dir


def test_full_install_writes_managed_block_and_audit(
    tmp_path: Path, isolated_audit_dir: Path
) -> None:
    cache = tmp_path / "cache.json"
    user_config = tmp_path / "mcp.json"
    user_config.write_text(
        json.dumps({"mcpServers": {"manual": {"command": "x"}}})
    )

    transport = _OneShotTransport(json.dumps(CATALOG_PAYLOAD).encode())
    fetcher = CatalogFetcher(
        primary_url="https://primary.example/cat.json",
        mirror_url="https://mirror.example/cat.json",
        cache_path=cache,
        revalidate_seconds=600,
        transport=transport,
    )
    auditor = CatalogAuditor(audit_dir=isolated_audit_dir)
    assert auditor.enabled

    service = CatalogService(
        fetcher=fetcher,
        user_config_path=user_config,
        auditor=auditor,
        config=CatalogServiceConfig(check_interval_seconds=3600),
        confirm_callback=lambda _preview: True,
    )

    outcome = service.install("fs-readonly", skip_confirmation=True)
    assert outcome.installed is not None
    assert outcome.preview.exit_code == 0
    assert any(
        change.path == "install-marker.txt" for change in outcome.preview.diff
    )

    payload = json.loads(user_config.read_text())
    assert payload["mcpServers"]["manual"] == {"command": "x"}
    managed = payload["bernstein-managed"]["mcpServers"]
    assert "fs-readonly" in managed
    assert managed["fs-readonly"]["version_pin"] == "1.0.0"
    assert managed["fs-readonly"]["command"] == "fs-readonly"

    audit_files = list(isolated_audit_dir.glob("*.jsonl"))
    assert audit_files, "expected at least one daily audit file"
    audit_lines = [
        json.loads(line)
        for fp in audit_files
        for line in fp.read_text().splitlines()
        if line.strip()
    ]
    event_types = {entry["event_type"] for entry in audit_lines}
    assert "mcp_catalog.fetch" in event_types
    assert "mcp_catalog.install" in event_types

    # Cache stamped with ETag from primary.
    cache_payload = json.loads(cache.read_text())
    assert cache_payload["etag"] == '"v1"'


def test_install_failure_does_not_write_user_config(tmp_path: Path) -> None:
    bad_payload = json.loads(json.dumps(CATALOG_PAYLOAD))
    bad_payload["entries"][0]["install_command"] = [
        sys.executable,
        "-c",
        "import sys; sys.exit(7)",
    ]
    transport = _OneShotTransport(json.dumps(bad_payload).encode())
    user_config = tmp_path / "mcp.json"
    fetcher = CatalogFetcher(
        primary_url="https://primary.example/cat.json",
        mirror_url="https://mirror.example/cat.json",
        cache_path=tmp_path / "cache.json",
        revalidate_seconds=600,
        transport=transport,
    )
    service = CatalogService(
        fetcher=fetcher,
        user_config_path=user_config,
        auditor=CatalogAuditor(target=None),
        config=CatalogServiceConfig(check_interval_seconds=3600),
        confirm_callback=lambda _preview: True,
    )
    outcome = service.install("fs-readonly", skip_confirmation=True)
    assert outcome.installed is None
    assert outcome.preview.exit_code == 7
    assert not user_config.exists()
