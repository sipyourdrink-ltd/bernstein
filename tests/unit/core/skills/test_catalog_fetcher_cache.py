"""Skill catalog cache location and conditional-fetch behaviour.

Parallel workers run in separate worktrees, so a cache keyed on the
current directory gives every worker its own empty cache. The default
cache is one user-level file that every worker shares.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.skills.catalog.enforcement import build_revocation_checker
from bernstein.core.skills.catalog.fetcher import (
    HTTPResponse,
    SkillCatalogFetcher,
    default_cache_path,
)
from bernstein.core.skills.catalog.lockfile import CatalogLockEntry, upsert_catalog_install
from bernstein.core.skills.catalog.manifest import SkillCatalogValidationError
from bernstein.core.skills.catalog.revocation import RevocationEntry, sign_revocation
from bernstein.core.skills.catalog.signature import generate_signer_keypair


class _RecordingTransport:
    def __init__(self, *responses: HTTPResponse) -> None:
        self.queue = list(responses)
        self.calls: list[dict[str, str]] = []

    def get(self, url: str, *, headers: dict[str, str]) -> HTTPResponse:
        self.calls.append(dict(headers))
        if not self.queue:
            raise AssertionError(f"unexpected request to {url}")
        return self.queue.pop(0)


def _catalog(pub: str | None = None, revocations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "generated_at": "2026-05-21T00:00:00Z",
        "entries": [
            {
                "id": "code-review",
                "name": "code-review",
                "version": "1.0.0",
                "description": "Review code.",
                "source": {"kind": "github", "repo": "acme/code-review", "tag": "v1.0.0"},
                "content_digest": "b" * 64,
                "verified": True,
            }
        ],
    }
    if pub is not None:
        payload["signer_pubkey"] = pub
        payload["revocations"] = revocations or []
    return payload


@pytest.fixture
def _user_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("BERNSTEIN_SKILLS_CATALOG_CACHE_PATH", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "bernstein" / "skills-catalog.json"


def test_default_cache_path_is_shared_across_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _user_cache: Path
) -> None:
    worker_a = tmp_path / "wt-a"
    worker_b = tmp_path / "wt-b"
    worker_a.mkdir()
    worker_b.mkdir()

    monkeypatch.chdir(worker_a)
    from_a = default_cache_path()
    monkeypatch.chdir(worker_b)
    from_b = default_cache_path()

    assert from_a == from_b == _user_cache
    assert SkillCatalogFetcher().cache_path == _user_cache


def test_default_cache_path_honours_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "shared" / "skills.json"
    monkeypatch.setenv("BERNSTEIN_SKILLS_CATALOG_CACHE_PATH", str(override))

    assert default_cache_path() == override


def test_rejected_body_is_revalidated_not_redownloaded(tmp_path: Path) -> None:
    t0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    transport = _RecordingTransport(
        HTTPResponse(status=200, body=json.dumps({"name": "x"}).encode(), etag='W/"x"'),
        HTTPResponse(status=304, body=b"", etag=None),
    )
    fetcher = SkillCatalogFetcher(
        primary_url="https://primary.example/skills.json",
        mirror_url="https://mirror.example/skills.json",
        cache_path=tmp_path / "skills.json",
        revalidate_seconds=600,
        transport=transport,
    )

    with pytest.raises(SkillCatalogValidationError):
        fetcher.fetch(now=t0)
    with pytest.raises(SkillCatalogValidationError):
        fetcher.fetch(now=t0 + timedelta(seconds=60))
    assert len(transport.calls) == 1

    with pytest.raises(SkillCatalogValidationError):
        fetcher.fetch(now=t0 + timedelta(seconds=601))
    assert transport.calls[1]["If-None-Match"] == 'W/"x"'


def _install(workdir: Path) -> None:
    upsert_catalog_install(
        workdir / "skills.lock",
        CatalogLockEntry(
            id="code-review",
            name="code-review",
            version="1.0.0",
            manifest_url="github://acme/code-review@v1.0.0",
            manifest_sha256="a" * 64,
            content_digest="b" * 64,
            install_id="deadbeef",
            chain_head="c" * 64,
            installed_at="2026-07-16T00:00:00Z",
        ),
        workdir=workdir,
    )


def _revocation(priv: str) -> dict[str, Any]:
    entry = RevocationEntry(skill_id="code-review", version_range="*", reason="CVE", issued_at="2026-07-16T00:00:00Z")
    return sign_revocation(entry, priv).to_dict()


def test_revocations_in_the_shared_cache_apply_in_every_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _user_cache: Path
) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    priv, pub = generate_signer_keypair()
    SkillCatalogFetcher().write_cache_payload(_catalog(pub, [_revocation(priv)]))
    worktree = tmp_path / "wt-a"
    worktree.mkdir()
    _install(worktree)

    checker = build_revocation_checker(worktree)

    assert checker.is_revoked("code-review", "1.0.0") is not None


def test_revocations_in_a_pre_upgrade_project_cache_still_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _user_cache: Path
) -> None:
    """A project cache written before the move keeps enforcing until a refetch."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    priv, pub = generate_signer_keypair()
    workdir = tmp_path / "project"
    workdir.mkdir()
    legacy = workdir / ".sdd" / "skills_catalog" / "catalog.json"
    SkillCatalogFetcher(cache_path=legacy).write_cache_payload(_catalog(pub, [_revocation(priv)]))
    _install(workdir)

    checker = build_revocation_checker(workdir)

    assert not _user_cache.exists()
    assert checker.is_revoked("code-review", "1.0.0") is not None
