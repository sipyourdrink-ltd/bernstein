"""HMAC-chained audit emission for catalog operations.

Every fetch / install / upgrade / uninstall is appended to the existing
:class:`~bernstein.core.security.audit.AuditLog`, so the catalog's
behaviour is verifiable by ``bernstein audit verify``.

A no-op fallback is used when an audit log directory is not provided
(e.g. integration tests on a transient tempdir): the catalog still
works but emits warnings into the standard logger.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Stable resource type used in audit events.
AUDIT_RESOURCE_TYPE = "mcp_catalog"

#: Default actor for audit entries.
AUDIT_ACTOR = "bernstein.mcp_catalog"


class _AuditTarget(Protocol):
    """Subset of :class:`AuditLog` we depend on."""

    def log(
        self,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> Any: ...


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


class CatalogAuditor:
    """Thin wrapper that records catalog operations as HMAC audit events.

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

    def _emit(self, event_type: str, resource_id: str, details: dict[str, Any]) -> None:
        target = self._target
        if target is None:
            logger.debug(
                "Audit disabled; would record %s on %s: %s",
                event_type,
                resource_id,
                details,
            )
            return
        try:
            target.log(
                event_type=event_type,
                actor=AUDIT_ACTOR,
                resource_type=AUDIT_RESOURCE_TYPE,
                resource_id=resource_id,
                details=details,
            )
        except Exception as exc:  # pragma: no cover - audit must never crash callers
            logger.warning("Audit log emission failed for %s: %s", event_type, exc)

    def fetch(self, *, source_url: str, from_cache: bool, revalidated: bool) -> None:
        """Record a catalog fetch."""
        self._emit(
            "mcp_catalog.fetch",
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
        version_pin: str,
        verified: bool,
        exit_code: int,
    ) -> None:
        """Record a catalog install."""
        self._emit(
            "mcp_catalog.install",
            entry_id,
            {
                "version_pin": version_pin,
                "verified": verified,
                "exit_code": exit_code,
            },
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
        """Record a catalog upgrade."""
        self._emit(
            "mcp_catalog.upgrade",
            entry_id,
            {
                "from_version": from_version,
                "to_version": to_version,
                "verified": verified,
                "exit_code": exit_code,
            },
        )

    def uninstall(self, *, entry_id: str) -> None:
        """Record a catalog uninstall."""
        self._emit("mcp_catalog.uninstall", entry_id, {})


__all__ = ["AUDIT_ACTOR", "AUDIT_RESOURCE_TYPE", "CatalogAuditor"]
