"""Spawn / doctor revocation enforcement (issue #2527).

Acceptance: after a signed revocation of skill X in range V, doctor flags every
affected install within one poll interval and the spawn path refuses X in V
with a signed refusal receipt; versions outside V are unaffected.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.security.audit import AuditLog
from bernstein.core.skills.catalog.enforcement import (
    build_revocation_checker,
    enforce_spawn_revocations,
    revoked_install_report,
    scan_revoked_installs,
)
from bernstein.core.skills.catalog.fetcher import SkillCatalogFetcher, default_cache_path
from bernstein.core.skills.catalog.lockfile import CatalogLockEntry, upsert_catalog_install
from bernstein.core.skills.catalog.revocation import RevocationEntry, sign_revocation
from bernstein.core.skills.catalog.signature import generate_signer_keypair


@pytest.fixture(autouse=True)
def isolate_audit_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))


def _install_row(entry_id: str, version: str) -> CatalogLockEntry:
    return CatalogLockEntry(
        id=entry_id,
        name=entry_id,
        version=version,
        manifest_url=f"github://acme/{entry_id}@v{version}",
        manifest_sha256="a" * 64,
        content_digest="b" * 64,
        install_id="deadbeef",
        chain_head="c" * 64,
        installed_at="2026-07-16T00:00:00Z",
    )


def _catalog_payload(pub: str, revocations: list[dict[str, object]]) -> dict[str, object]:
    return {
        "version": 1,
        "generated_at": "2026-07-16T00:00:00Z",
        "signer_pubkey": pub,
        "entries": [
            {
                "id": "code-review",
                "name": "code-review",
                "version": "1.5.0",
                "description": "Review code.",
                "source": {"kind": "github", "repo": "acme/code-review", "tag": "v1.5.0"},
                "content_digest": "b" * 64,
                "verified": True,
            }
        ],
        "revocations": revocations,
    }


def _seed(tmp_path: Path, *, installed_version: str, revocations: list[dict[str, object]], pub: str) -> None:
    """Write a lockfile install row and a cached catalog with revocations."""
    upsert_catalog_install(
        tmp_path / "skills.lock",
        _install_row("code-review", installed_version),
        workdir=tmp_path,
    )
    fetcher = SkillCatalogFetcher(cache_path=default_cache_path())
    fetcher.write_cache_payload(_catalog_payload(pub, revocations))


def test_doctor_report_flags_revoked_install(tmp_path: Path) -> None:
    priv, pub = generate_signer_keypair()
    revocation = sign_revocation(
        RevocationEntry(
            skill_id="code-review",
            version_range=">=1.0.0,<2.0.0",
            reason="CVE-2026-1234",
            issued_at="2026-07-16T00:00:00Z",
        ),
        priv,
    )
    _seed(tmp_path, installed_version="1.5.0", revocations=[revocation.to_dict()], pub=pub)

    report = revoked_install_report(tmp_path)
    assert len(report) == 1
    assert report[0].skill_id == "code-review"
    assert report[0].version == "1.5.0"
    assert report[0].reason == "CVE-2026-1234"


def test_version_outside_range_is_unaffected(tmp_path: Path) -> None:
    priv, pub = generate_signer_keypair()
    revocation = sign_revocation(
        RevocationEntry(
            skill_id="code-review",
            version_range=">=1.0.0,<2.0.0",
            reason="CVE-2026-1234",
            issued_at="2026-07-16T00:00:00Z",
        ),
        priv,
    )
    _seed(tmp_path, installed_version="2.0.0", revocations=[revocation.to_dict()], pub=pub)
    assert revoked_install_report(tmp_path) == []


def test_spawn_enforcement_records_signed_refusal_receipt(tmp_path: Path) -> None:
    priv, pub = generate_signer_keypair()
    revocation = sign_revocation(
        RevocationEntry(
            skill_id="code-review",
            version_range="*",
            reason="key compromise",
            issued_at="2026-07-16T00:00:00Z",
        ),
        priv,
    )
    _seed(tmp_path, installed_version="1.5.0", revocations=[revocation.to_dict()], pub=pub)

    refused = enforce_spawn_revocations(tmp_path)
    assert [r.skill_id for r in refused] == ["code-review"]

    log = AuditLog(tmp_path / ".sdd" / "audit")
    events = log.query(event_type="skill.verification_refusal")
    assert len(events) == 1
    assert events[0].details["stage"] == "spawn"
    assert events[0].details["reason_code"] == "revoked"
    ok, _errors = log.verify()
    assert ok


def test_forged_revocation_is_ignored(tmp_path: Path) -> None:
    _priv, pub = generate_signer_keypair()
    attacker_priv, _attacker_pub = generate_signer_keypair()
    forged = sign_revocation(
        RevocationEntry(
            skill_id="code-review",
            version_range="*",
            reason="forged",
            issued_at="2026-07-16T00:00:00Z",
        ),
        attacker_priv,  # signed by a key the catalog does not trust
    )
    _seed(tmp_path, installed_version="1.5.0", revocations=[forged.to_dict()], pub=pub)
    assert revoked_install_report(tmp_path) == []


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_enforcement_honours_revocation_within_one_poll_interval(tmp_path: Path) -> None:
    priv, pub = generate_signer_keypair()
    # Seed with no revocations yet.
    _seed(tmp_path, installed_version="1.5.0", revocations=[], pub=pub)

    clock = _FakeClock()
    checker = build_revocation_checker(tmp_path, poll_interval_seconds=60, clock=clock)
    assert scan_revoked_installs(tmp_path, checker) == []

    # Publisher issues a signed revocation and republishes the cached catalog.
    revocation = sign_revocation(
        RevocationEntry(
            skill_id="code-review",
            version_range=">=1.0.0,<2.0.0",
            reason="CVE-2026-9999",
            issued_at="2026-07-16T01:00:00Z",
        ),
        priv,
    )
    fetcher = SkillCatalogFetcher(cache_path=default_cache_path())
    fetcher.write_cache_payload(_catalog_payload(pub, [revocation.to_dict()]))

    # Still within the interval: stale cache, not yet enforced.
    clock.advance(30)
    assert scan_revoked_installs(tmp_path, checker) == []

    # After one poll interval: re-polled and enforced.
    clock.advance(30)
    refused = scan_revoked_installs(tmp_path, checker)
    assert [r.skill_id for r in refused] == ["code-review"]
