"""End-to-end service-level tests (no real network, no real subprocess)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.protocols.mcp_catalog.audit import CatalogAuditor
from bernstein.core.protocols.mcp_catalog.fetcher import (
    CatalogFetcher,
    HTTPResponse,
)
from bernstein.core.protocols.mcp_catalog.sandbox_preview import (
    FileDiff,
    InstallPreview,
)
from bernstein.core.protocols.mcp_catalog.service import (
    CatalogService,
    CatalogServiceConfig,
)
from bernstein.core.protocols.mcp_catalog.user_config import list_installed

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.protocols.mcp_catalog.manifest import CatalogEntry


def _good_catalog(version: str = "1.0.0") -> dict[str, Any]:
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
                "version_pin": version,
                "transports": ["stdio"],
                "verified_by_bernstein": True,
            },
            {
                "id": "auto-upgradable",
                "name": "AU",
                "description": "auto",
                "homepage": "https://x",
                "repository": "https://x.git",
                "install_command": ["true"],
                "version_pin": version,
                "transports": ["stdio"],
                "verified_by_bernstein": True,
                "auto_upgrade": True,
            },
        ],
    }


class _FakeTransport:
    def __init__(self, responses: list[HTTPResponse]) -> None:
        self._responses = list(responses)

    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        if not self._responses:
            raise AssertionError(f"no fake response queued for {url}")
        return self._responses.pop(0)


class _FakeAuditor(CatalogAuditor):
    """Auditor that records calls in memory rather than writing files."""

    def __init__(self) -> None:
        super().__init__(target=None)
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def fetch(self, *, source_url: str, from_cache: bool, revalidated: bool) -> None:
        self.events.append(
            (
                "mcp_catalog.fetch",
                source_url,
                {
                    "source_url": source_url,
                    "from_cache": from_cache,
                    "revalidated": revalidated,
                },
            )
        )

    def install(
        self,
        *,
        entry_id: str,
        version_pin: str,
        verified: bool,
        exit_code: int,
    ) -> None:
        self.events.append(
            (
                "mcp_catalog.install",
                entry_id,
                {
                    "version_pin": version_pin,
                    "verified": verified,
                    "exit_code": exit_code,
                },
            )
        )

    def upgrade(
        self,
        *,
        entry_id: str,
        from_version: str,
        to_version: str,
        verified: bool,
        exit_code: int,
    ) -> None:
        self.events.append(
            (
                "mcp_catalog.upgrade",
                entry_id,
                {
                    "from_version": from_version,
                    "to_version": to_version,
                    "verified": verified,
                    "exit_code": exit_code,
                },
            )
        )

    def uninstall(self, *, entry_id: str) -> None:
        self.events.append(("mcp_catalog.uninstall", entry_id, {}))


def _stub_preview(
    monkeypatch: pytest.MonkeyPatch, *, exit_code: int = 0
) -> list[CatalogEntry]:
    """Replace ``run_install_preview`` with a deterministic stub.

    Returns the list that captures every entry the service tries to install.
    """
    captured: list[CatalogEntry] = []

    def _fake_preview(entry: CatalogEntry, **_kwargs: Any) -> InstallPreview:
        captured.append(entry)
        return InstallPreview(
            exit_code=exit_code,
            stdout=b"ok",
            stderr=b"",
            duration_seconds=0.01,
            diff=(FileDiff(path="hello.txt", change_type="added", size_bytes=2),),
            sandbox_root="/tmp/fake",
        )

    monkeypatch.setattr(
        "bernstein.core.protocols.mcp_catalog.service.run_install_preview",
        _fake_preview,
    )
    return captured


def _build_service(
    tmp_path: Path,
    *,
    catalog_payload: dict[str, Any] | None = None,
    auditor: CatalogAuditor | None = None,
) -> tuple[CatalogService, _FakeAuditor]:
    payload = catalog_payload or _good_catalog()
    transport = _FakeTransport(
        [HTTPResponse(status=200, body=json.dumps(payload).encode(), etag='"v1"')]
    )
    fetcher = CatalogFetcher(
        primary_url="https://primary.example/x.json",
        mirror_url="https://mirror.example/x.json",
        cache_path=tmp_path / "cache.json",
        revalidate_seconds=1,
        transport=transport,
    )
    fake_auditor = auditor or _FakeAuditor()
    service = CatalogService(
        fetcher=fetcher,
        user_config_path=tmp_path / "mcp.json",
        auditor=fake_auditor,
        config=CatalogServiceConfig(check_interval_seconds=3600),
        confirm_callback=lambda _preview: True,
    )
    assert isinstance(fake_auditor, _FakeAuditor)
    return service, fake_auditor


def test_install_emits_audit_and_writes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_preview(monkeypatch, exit_code=0)
    service, auditor = _build_service(tmp_path)

    outcome = service.install("fs-readonly", skip_confirmation=True)
    assert outcome.installed is not None
    assert outcome.preview.succeeded is True
    assert captured and captured[0].id == "fs-readonly"

    assert any(e[0] == "mcp_catalog.fetch" for e in auditor.events)
    assert any(
        e[0] == "mcp_catalog.install" and e[1] == "fs-readonly"
        for e in auditor.events
    )
    persisted = list_installed(service.user_config_path)
    assert len(persisted) == 1
    assert persisted[0].id == "fs-readonly"


def test_install_failure_leaves_config_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_preview(monkeypatch, exit_code=1)
    service, auditor = _build_service(tmp_path)
    outcome = service.install("fs-readonly", skip_confirmation=True)
    assert outcome.installed is None
    assert outcome.preview.exit_code == 1
    assert not service.user_config_path.exists()
    assert any(
        e[0] == "mcp_catalog.install" and e[2]["exit_code"] == 1
        for e in auditor.events
    )


def test_unverified_entry_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_preview(monkeypatch, exit_code=0)
    payload = _good_catalog()
    payload["entries"][0]["verified_by_bernstein"] = False
    service, _ = _build_service(tmp_path, catalog_payload=payload)
    outcome = service.install("fs-readonly", skip_confirmation=True)
    assert outcome.warning_unverified is True


def test_install_aborted_when_confirm_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_preview(monkeypatch, exit_code=0)
    service, _ = _build_service(tmp_path)
    service.confirm_callback = lambda _preview: False
    outcome = service.install("fs-readonly", skip_confirmation=False)
    assert outcome.installed is None
    assert outcome.confirmed is False
    assert not service.user_config_path.exists()


def test_upgrade_skipped_when_versions_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_preview(monkeypatch, exit_code=0)
    service, _ = _build_service(tmp_path)
    service.install("fs-readonly", skip_confirmation=True)

    # Re-fetch returns the same version. Stub the network for the upgrade.
    service.fetcher = CatalogFetcher(
        primary_url="https://primary.example/x.json",
        mirror_url="https://mirror.example/x.json",
        cache_path=tmp_path / "cache.json",
        revalidate_seconds=1,
        transport=_FakeTransport(
            [
                HTTPResponse(
                    status=200,
                    body=json.dumps(_good_catalog()).encode(),
                    etag='"v1"',
                )
            ]
        ),
    )
    outcome = service.upgrade("fs-readonly", force_refresh=True)
    assert outcome.applied is False
    assert outcome.skipped_reason == "already on latest version"


def test_upgrade_applies_when_version_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_preview(monkeypatch, exit_code=0)
    service, auditor = _build_service(tmp_path)
    service.install("fs-readonly", skip_confirmation=True)

    # Replace the fetcher with one returning v2.
    service.fetcher = CatalogFetcher(
        primary_url="https://primary.example/x.json",
        mirror_url="https://mirror.example/x.json",
        cache_path=tmp_path / "cache.json",
        revalidate_seconds=1,
        transport=_FakeTransport(
            [
                HTTPResponse(
                    status=200,
                    body=json.dumps(_good_catalog("2.0.0")).encode(),
                    etag='"v2"',
                )
            ]
        ),
    )
    outcome = service.upgrade(
        "fs-readonly", skip_confirmation=True, force_refresh=True
    )
    assert outcome.applied is True
    assert outcome.from_version == "1.0.0"
    assert outcome.to_version == "2.0.0"
    assert any(captured) and captured[-1].version_pin == "2.0.0"
    assert any(
        e[0] == "mcp_catalog.upgrade"
        and e[2]["from_version"] == "1.0.0"
        and e[2]["to_version"] == "2.0.0"
        for e in auditor.events
    )


def test_background_check_due_respects_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_preview(monkeypatch, exit_code=0)
    service, _ = _build_service(tmp_path)
    # No installs yet => the first check is allowed.
    assert service.background_check_due() is True

    service.install("fs-readonly", skip_confirmation=True)
    # Just installed => last_upgrade_check is now; the cadence has not
    # elapsed.
    assert service.background_check_due() is False

    # Push the wall clock forward past the configured interval.
    future = datetime.now(tz=UTC) + timedelta(seconds=4000)
    assert service.background_check_due(now=future) is True


def test_uninstall_emits_audit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_preview(monkeypatch, exit_code=0)
    service, auditor = _build_service(tmp_path)
    service.install("fs-readonly", skip_confirmation=True)
    assert service.uninstall("fs-readonly") is True
    assert any(
        e[0] == "mcp_catalog.uninstall" and e[1] == "fs-readonly"
        for e in auditor.events
    )
    assert service.uninstall("fs-readonly") is False
