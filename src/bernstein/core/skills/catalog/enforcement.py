"""Fleet-wide revocation enforcement for the spawn and doctor paths (issue #2527).

A compromised skill version executes with every spawned worker's privileges, so
containment must be automatic and bounded in time. This module is the shared
enforcement point the doctor path and the spawn/inject path both call:

* it reads the catalog-installed skills from ``skills.lock``;
* it builds a :class:`RevocationChecker` over the *signed* revocations in the
  cached catalog (a forged kill switch is ignored -- enforcement acts only on
  revocations that verify against the catalog signer key);
* it flags every installed version a signed revocation covers, and (on the
  spawn path) records a chain-anchored refusal receipt for each.

Because the checker re-polls on an interval, a revocation published upstream is
honoured fleet-wide within one poll interval. Strip the signed revocations and
the audit chain and there is nothing to enforce -- the guarantee is anchored on
the signed identity and the HMAC chain, not portable to a registry.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.skills.catalog.fetcher import (
    SkillCatalogFetcher,
    default_cache_path,
    legacy_project_cache_path,
)
from bernstein.core.skills.catalog.lockfile import CATALOG_LOCK_FILENAME, read_state
from bernstein.core.skills.catalog.revocation import (
    RevocationChecker,
    RevocationEntry,
    parse_revocations,
)
from bernstein.core.skills.catalog.service import DEFAULT_REVOCATION_POLL_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "RefusedSkill",
    "build_revocation_checker",
    "enforce_spawn_revocations",
    "installed_catalog_skills",
    "revoked_install_report",
    "scan_revoked_installs",
]


@dataclass(frozen=True)
class RefusedSkill:
    """One catalog-installed skill version covered by a signed revocation."""

    skill_id: str
    version: str
    reason: str
    version_range: str


def installed_catalog_skills(workdir: Path) -> list[tuple[str, str]]:
    """Return ``(id, version)`` for every catalog-installed skill in the lockfile."""
    lockfile = workdir / CATALOG_LOCK_FILENAME
    return [(row.id, row.version) for row in read_state(lockfile).catalog]


def build_revocation_checker(
    workdir: Path,
    *,
    poll_interval_seconds: float = DEFAULT_REVOCATION_POLL_SECONDS,
    cache_path: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> RevocationChecker:
    """Build a :class:`RevocationChecker` backed by the cached catalog.

    The loader reads the shared user-level catalog cache on each poll and
    returns only the revocations that verify against the catalog signer key.
    Until that cache holds a valid catalog, a copy left in the project by
    releases that cached per project is read instead, so revocations cached
    before the move keep applying. An explicit ``cache_path`` is read alone.
    """
    candidates = [cache_path] if cache_path is not None else [default_cache_path(), legacy_project_cache_path(workdir)]

    def load() -> list[RevocationEntry]:
        catalog = None
        for candidate in candidates:
            catalog = SkillCatalogFetcher(cache_path=candidate).cached()
            if catalog is not None:
                break
        if catalog is None or not catalog.revocations:
            return []
        return parse_revocations(catalog.revocations, catalog.signer_pubkey)

    return RevocationChecker(
        load_revocations=load,
        poll_interval_seconds=poll_interval_seconds,
        clock=clock,
    )


def scan_revoked_installs(
    workdir: Path,
    checker: RevocationChecker,
) -> list[RefusedSkill]:
    """Return the installed skills covered by a signed revocation."""
    refused: list[RefusedSkill] = []
    for skill_id, version in installed_catalog_skills(workdir):
        hit = checker.is_revoked(skill_id, version)
        if hit is not None:
            refused.append(
                RefusedSkill(
                    skill_id=skill_id,
                    version=version,
                    reason=hit.reason,
                    version_range=hit.version_range,
                )
            )
    return refused


def revoked_install_report(
    workdir: Path,
    *,
    checker: RevocationChecker | None = None,
    poll_interval_seconds: float = DEFAULT_REVOCATION_POLL_SECONDS,
    cache_path: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[RefusedSkill]:
    """Doctor-facing scan: flag revoked installs without recording receipts.

    The doctor surface is advisory; it reports affected installs so an operator
    sees, within one poll interval, every install a revocation covers.
    """
    active = checker or build_revocation_checker(
        workdir,
        poll_interval_seconds=poll_interval_seconds,
        cache_path=cache_path,
        clock=clock,
    )
    return scan_revoked_installs(workdir, active)


def enforce_spawn_revocations(
    workdir: Path,
    *,
    checker: RevocationChecker | None = None,
    poll_interval_seconds: float = DEFAULT_REVOCATION_POLL_SECONDS,
    cache_path: Path | None = None,
    audit_key_path: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[RefusedSkill]:
    """Spawn-path enforcement: flag revoked installs and record refusal receipts.

    Returns the refused skills so the caller can also decline to inject them.
    Never raises: an enforcement hiccup must not wedge a spawn, but the refusal
    receipt records every revoked version that was contained.
    """
    active = checker or build_revocation_checker(
        workdir,
        poll_interval_seconds=poll_interval_seconds,
        cache_path=cache_path,
        clock=clock,
    )
    refused = scan_revoked_installs(workdir, active)
    for item in refused:
        _record_spawn_refusal(workdir, item, audit_key_path=audit_key_path)
    return refused


def _record_spawn_refusal(
    workdir: Path,
    item: RefusedSkill,
    *,
    audit_key_path: Path | None,
) -> None:
    """Append a chain-anchored spawn-side refusal receipt (best-effort)."""
    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.security.audit_chain import (
        AuditChainStore,
        record_skill_verification_refusal,
    )

    try:
        hmac_key = load_or_create_audit_key(audit_key_path)
        chain = AuditChainStore(workdir / ".sdd" / "audit", key=hmac_key)
        record_skill_verification_refusal(
            chain=chain,
            skill_id=item.skill_id,
            stage="spawn",
            reason_code="revoked",
            detail=f"refusing to inject revoked skill {item.skill_id!r} {item.version} ({item.reason})",
            version=item.version,
        )
    except Exception as exc:  # pragma: no cover - refusal receipt is best-effort
        logger.warning("spawn-side refusal receipt failed for %s: %s", item.skill_id, exc)
