"""HMAC-audited vault operations.

Every connect / read / revoke goes through :func:`audit_event` which writes
an immutable entry into the project's audit chain
(``.sdd/audit/YYYY-MM-DD.jsonl``) using
:class:`bernstein.core.security.audit.AuditLog`. Secret material is never
written — only the provider id, account label, fingerprint hash, and
backend.

Audit failures are non-fatal for read paths (we fall back to logging a
warning) so a broken audit setup cannot lock a user out of their own
credentials. The ``connect`` and ``revoke`` write paths re-raise the
underlying error to make the misconfiguration visible.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Default audit directory mirroring the rest of the codebase. Overridable
#: per-call so tests can isolate.
DEFAULT_AUDIT_DIR = Path(".sdd/audit")


def default_audit_log(audit_dir: Path | None = None) -> Any:
    """Return an :class:`bernstein.core.security.audit.AuditLog` for vault use.

    Importing the audit module is deferred so vault helpers can be imported
    in environments where the HMAC key path is not yet provisioned (e.g.
    during a fresh ``bernstein connect`` on a brand-new install).
    """
    from bernstein.core.security.audit import AuditLog

    target = audit_dir if audit_dir is not None else DEFAULT_AUDIT_DIR
    target.mkdir(parents=True, exist_ok=True)
    return AuditLog(target)


def audit_event(
    *,
    action: str,
    provider_id: str,
    account: str,
    fingerprint: str,
    backend: str,
    audit_dir: Path | None = None,
    extra: dict[str, Any] | None = None,
    raise_on_failure: bool = False,
) -> None:
    """Write a vault audit entry.

    Args:
        action: One of ``"connect"``, ``"read"``, ``"revoke"``, ``"test"``.
        provider_id: The provider whose credential is involved.
        account: Account label associated with the credential. Pass the
            empty string when no account is known (e.g. for a failed
            ``read`` lookup).
        fingerprint: SHA-256 fingerprint of the secret. Pass the empty
            string for events that don't have a secret loaded.
        backend: Backend identifier, e.g. ``"keyring"`` or ``"file"``.
        audit_dir: Optional override for tests.
        extra: Optional structured fields merged into the event details.
        raise_on_failure: When ``True`` re-raise exceptions; otherwise log
            and swallow. Read paths use ``False`` so audit faults cannot
            wedge ``bernstein from-ticket``.
    """
    details: dict[str, Any] = {
        "action": action,
        "backend": backend,
        "account": account,
        "fingerprint": fingerprint,
    }
    if extra:
        details.update(extra)
    try:
        log = default_audit_log(audit_dir)
        log.log(
            event_type=f"vault.{action}",
            actor="cli:bernstein",
            resource_type="credential",
            resource_id=provider_id,
            details=details,
        )
    except Exception as exc:
        if raise_on_failure:
            raise
        logger.warning("vault: audit write for %s/%s failed: %s", action, provider_id, exc)
