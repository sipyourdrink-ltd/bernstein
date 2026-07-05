"""HMAC-chained audit emission for team manifest operations (issue #2248).

Every run that expands a ``team_manifest:`` reference appends a
``team.manifest.resolve`` event to the existing HMAC-chained audit log
under ``.sdd/audit/``, so "which team produced this run" is answerable
from the chain (AC3). The event's HMAC becomes the ``chain_head`` of the
matching ``teams.lock`` row, anchoring the pin exactly like a skills
catalog install.

Event types are namespaced ``team.manifest.*`` so they never collide
with the ``skill.catalog.*`` events of
:mod:`bernstein.core.skills.catalog.audit`, whose auditor shape this
module mirrors.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bernstein.core.teams.lockfile import (
    GENESIS_CHAIN_HEAD,
    TEAMS_LOCK_FILENAME,
    TeamLockEntry,
    fresh_install_id,
    upsert_team_pin,
)
from bernstein.core.teams.manifest import TeamManifestError, resolve_team_manifest

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.skills.catalog.audit import _AuditTarget

logger = logging.getLogger(__name__)

#: Stable resource type used in audit events.
AUDIT_RESOURCE_TYPE = "team_manifest"

#: Default actor for audit entries.
AUDIT_ACTOR = "bernstein.team_manifest"

#: Emitted when a run expands a ``team_manifest:`` reference.
EVENT_RESOLVE = "team.manifest.resolve"

#: Emitted when a drift check finds diverged role templates.
EVENT_DRIFT = "team.manifest.drift"

#: Public mapping so callers can iterate every event-type this module owns.
EVENT_TYPES: tuple[str, ...] = (EVENT_RESOLVE, EVENT_DRIFT)


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


class TeamManifestAuditor:
    """Records team manifest operations as HMAC-chained audit events.

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

    def resolve(self, *, name: str, digest: str, version: str, source: str) -> AuditEvent | None:
        """Record that a run resolved and expanded a team manifest."""
        return self._emit(
            EVENT_RESOLVE,
            name,
            {"name": name, "digest": digest, "version": version, "source": source},
        )

    def drift(self, *, name: str, digest: str, drifted_roles: list[str]) -> AuditEvent | None:
        """Record a drift check that found diverged role templates."""
        return self._emit(
            EVENT_DRIFT,
            name,
            {"name": name, "digest": digest, "drifted_roles": drifted_roles},
        )


def record_run_team_manifest(
    workdir: Path,
    *,
    name: str,
    digest: str,
    audit_dir: Path | None = None,
) -> AuditEvent | None:
    """Anchor a run's team manifest in the audit chain and ``teams.lock``.

    Called from run bootstrap after the seed parser expanded a
    ``team_manifest:`` reference. Best-effort by design: lineage recording
    must never abort a run, so lockfile write failures are logged and
    swallowed (the audit event itself is the primary record).

    Args:
        workdir: Project root; the audit log lives at
            ``<workdir>/.sdd/audit`` unless *audit_dir* overrides it, and
            ``teams.lock`` is written at the project root.
        name: Resolved manifest name.
        digest: Resolved manifest canonical digest.
        audit_dir: Optional audit directory override (tests).

    Returns:
        The appended audit event, or ``None`` when auditing is disabled.
    """
    version = "unknown"
    source = "unknown"
    try:
        manifest = resolve_team_manifest(name, workdir=workdir)
        version = manifest.version
        source = str(manifest.source_path) if manifest.source_path is not None else "builtin"
    except TeamManifestError as exc:
        logger.warning("team manifest %r not re-resolvable for lineage details: %s", name, exc)

    auditor = TeamManifestAuditor(audit_dir=audit_dir or workdir / ".sdd" / "audit")
    event = auditor.resolve(name=name, digest=digest, version=version, source=source)
    chain_head = event.hmac if event is not None and event.hmac else GENESIS_CHAIN_HEAD

    entry = TeamLockEntry(
        name=name,
        version=version,
        manifest_digest=digest,
        source=source,
        install_id=fresh_install_id(),
        chain_head=chain_head,
        installed_at=_utc_now_iso(),
    )
    try:
        upsert_team_pin(workdir / TEAMS_LOCK_FILENAME, entry, workdir=workdir)
    except OSError as exc:
        logger.warning("teams.lock update failed for manifest %r: %s", name, exc)
    return event


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC).isoformat()


__all__ = [
    "AUDIT_ACTOR",
    "AUDIT_RESOURCE_TYPE",
    "EVENT_DRIFT",
    "EVENT_RESOLVE",
    "EVENT_TYPES",
    "TeamManifestAuditor",
    "record_run_team_manifest",
]
