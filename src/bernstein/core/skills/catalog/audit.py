"""HMAC-chained audit emission for skill catalog operations.

Every fetch / install / upgrade / uninstall is appended to the existing
:class:`bernstein.core.security.audit.AuditLog`, so a third party can
verify the install lineage with ``bernstein audit verify``.

A no-op fallback is used when an audit directory is not provided
(integration tests on a transient tempdir): the catalog still works but
emits warnings into the standard logger.

The event-type strings live here and are intentionally namespaced
(``skill.catalog.*``) so they do not collide with the MCP catalog events
emitted by :mod:`bernstein.core.protocols.mcp_catalog.audit`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit import AuditEvent

logger = logging.getLogger(__name__)

#: Stable resource type used in audit events.
AUDIT_RESOURCE_TYPE = "skill_catalog"

#: Default actor for audit entries.
AUDIT_ACTOR = "bernstein.skill_catalog"

#: Canonical event-type strings emitted by this module.
EVENT_FETCH = "skill.catalog.fetch"
EVENT_INSTALL = "skill.catalog.install"
EVENT_UPGRADE = "skill.catalog.upgrade"
EVENT_UNINSTALL = "skill.catalog.uninstall"
EVENT_SYNC = "skill.catalog.sync"

#: Public mapping so callers can iterate every event-type this module owns.
EVENT_TYPES: tuple[str, ...] = (
    EVENT_FETCH,
    EVENT_INSTALL,
    EVENT_UPGRADE,
    EVENT_UNINSTALL,
    EVENT_SYNC,
)


class _AuditTarget(Protocol):
    """Subset of :class:`bernstein.core.security.audit.AuditLog`."""

    def log(
        self,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent: ...

    def query(
        self,
        *,
        event_type: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[AuditEvent]: ...


def _resolve_audit_log(audit_dir: Path | None) -> _AuditTarget | None:
    """Construct an :class:`AuditLog` lazily, returning ``None`` on failure."""
    if audit_dir is None:
        return None
    try:
        from bernstein.core.security.audit import AuditLog
    except ImportError:  # pragma: no cover - audit module always present
        return None
    try:
        return AuditLog(audit_dir)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to open audit log at %s: %s", audit_dir, exc)
        return None


def compute_manifest_sha256(manifest_url: str, payload: dict[str, Any]) -> str:
    """Return a deterministic SHA-256 over (url + canonical JSON payload).

    The hash binds the URL of the manifest to its bytes so a replay that
    swaps the upstream while keeping the URL is caught.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    hasher = hashlib.sha256(manifest_url.encode("utf-8"))
    hasher.update(b"\n")
    hasher.update(canonical)
    return hasher.hexdigest()


class SkillCatalogAuditor:
    """Thin wrapper that records skill catalog operations as audit events.

    Args:
        audit_dir: Directory containing the daily JSONL HMAC audit log.
            ``None`` disables auditing (tests and offline development).
        target: Optional pre-built audit target (testing).
    """

    def __init__(
        self,
        audit_dir: Path | None = None,
        *,
        target: _AuditTarget | None = None,
    ) -> None:
        if target is not None:
            self._target: _AuditTarget | None = target
        else:
            self._target = _resolve_audit_log(audit_dir)

    @property
    def enabled(self) -> bool:
        """Whether audit emission is wired up."""
        return self._target is not None

    @property
    def target(self) -> _AuditTarget | None:
        """The underlying audit target (read-only)."""
        return self._target

    def _emit(self, event_type: str, resource_id: str, details: dict[str, Any]) -> AuditEvent | None:
        target = self._target
        if target is None:
            logger.debug(
                "Audit disabled; would record %s on %s: %s",
                event_type,
                resource_id,
                details,
            )
            return None
        try:
            return target.log(
                event_type=event_type,
                actor=AUDIT_ACTOR,
                resource_type=AUDIT_RESOURCE_TYPE,
                resource_id=resource_id,
                details=details,
            )
        except Exception as exc:  # pragma: no cover - audit must never crash callers
            logger.warning("Audit log emission failed for %s: %s", event_type, exc)
            return None

    def fetch(
        self,
        *,
        source_url: str,
        from_cache: bool,
        revalidated: bool,
    ) -> AuditEvent | None:
        """Record a catalog fetch."""
        return self._emit(
            EVENT_FETCH,
            source_url,
            {
                "source_url": source_url,
                "from_cache": from_cache,
                "revalidated": revalidated,
            },
        )

    def install(
        self,
        *,
        entry_id: str,
        manifest_url: str,
        manifest_sha256: str,
        manifest_signer_pubkey: str | None,
        install_id: str,
        prior_install_chain_head: str,
    ) -> AuditEvent | None:
        """Record a catalog install.

        Args:
            entry_id: Stable id of the installed catalog entry.
            manifest_url: URL-shaped locator pointing at the source.
            manifest_sha256: SHA-256 of the resolved manifest bytes.
            manifest_signer_pubkey: PEM-encoded signer key, or ``None``
                when the install was performed with ``--allow-unverified``.
            install_id: Per-install unique identifier; ties the audit
                event to the lockfile receipt.
            prior_install_chain_head: HMAC of the previous install event for
                this entry, so a replay can detect that the per-entry install
                history advanced identically. Arbitrarily many unrelated
                records sit between that event and this one, so it is *not*
                this record's chain predecessor and must not be recorded under
                the ``prev_chain_digest`` key, which
                :func:`bernstein.core.security.audit._stated_predecessor`
                reads as exactly that claim and the verifier now checks.
        """
        return self._emit(
            EVENT_INSTALL,
            entry_id,
            {
                "manifest_url": manifest_url,
                "manifest_sha256": manifest_sha256,
                "manifest_signer_pubkey": manifest_signer_pubkey,
                "install_id": install_id,
                "prior_install_chain_head": prior_install_chain_head,
            },
        )

    def upgrade(
        self,
        *,
        entry_id: str,
        from_version: str,
        to_version: str,
        manifest_url: str,
        manifest_sha256: str,
        install_id: str,
        prior_install_chain_head: str,
    ) -> AuditEvent | None:
        """Record a catalog upgrade.

        ``prior_install_chain_head`` carries the same per-entry link as
        :meth:`install`; see there for why it is not the chain predecessor.
        """
        return self._emit(
            EVENT_UPGRADE,
            entry_id,
            {
                "from_version": from_version,
                "to_version": to_version,
                "manifest_url": manifest_url,
                "manifest_sha256": manifest_sha256,
                "install_id": install_id,
                "prior_install_chain_head": prior_install_chain_head,
            },
        )

    def uninstall(self, *, entry_id: str) -> AuditEvent | None:
        """Record a catalog uninstall."""
        return self._emit(EVENT_UNINSTALL, entry_id, {})

    def sync(self, *, lockfile_digest: str, lineage_receipt: str) -> AuditEvent | None:
        """Record a sync operation (drift detection / cross-worktree adopt)."""
        return self._emit(
            EVENT_SYNC,
            lockfile_digest,
            {
                "lockfile_digest": lockfile_digest,
                "lineage_receipt": lineage_receipt,
            },
        )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def known_good_manifest_shas(self) -> set[str]:
        """Return every manifest_sha256 the chain has ever installed.

        Reading this set lets the CI lineage gate accept a lockfile entry
        whose digest is anchored in the chain and reject one that is not.
        """
        target = self._target
        if target is None:
            return set()
        results: set[str] = set()
        try:
            events = target.query(event_type=EVENT_INSTALL)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Audit query failed: %s", exc)
            return set()
        for ev in events:
            sha = ev.details.get("manifest_sha256")
            if isinstance(sha, str) and sha:
                results.add(sha)
        try:
            events = target.query(event_type=EVENT_UPGRADE)
        except Exception:  # pragma: no cover - defensive
            return results
        for ev in events:
            sha = ev.details.get("manifest_sha256")
            if isinstance(sha, str) and sha:
                results.add(sha)
        return results

    def last_install_for_entry(self, entry_id: str) -> AuditEvent | None:
        """Return the most recent install audit event for ``entry_id``."""
        target = self._target
        if target is None:
            return None
        try:
            events = target.query(event_type=EVENT_INSTALL)
        except Exception:  # pragma: no cover - defensive
            return None
        for ev in reversed(events):
            if ev.resource_id == entry_id:
                return ev
        return None


__all__ = [
    "AUDIT_ACTOR",
    "AUDIT_RESOURCE_TYPE",
    "EVENT_FETCH",
    "EVENT_INSTALL",
    "EVENT_SYNC",
    "EVENT_TYPES",
    "EVENT_UNINSTALL",
    "EVENT_UPGRADE",
    "SkillCatalogAuditor",
    "compute_manifest_sha256",
]
