"""Immutable HMAC-chained audit log.

Every audit event carries an HMAC that chains to the previous event's HMAC,
forming a tamper-evident sequence.  Daily log rotation produces one JSONL
file per day; the chain carries across file boundaries.

HMAC key is read from ``.sdd/config/audit-key`` (auto-generated if absent).
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_GENESIS_HMAC = "0" * 64


@dataclass(frozen=True)
class AuditEvent:
    """A single HMAC-chained audit log entry.

    Attributes:
        timestamp: ISO 8601 timestamp of the event.
        event_type: Category of the event (e.g. "task.transition").
        actor: Who/what triggered the event.
        resource_type: Type of resource affected (e.g. "task", "agent").
        resource_id: ID of the affected resource.
        details: Arbitrary structured data about the event.
        prev_hmac: HMAC of the preceding event in the chain.
        hmac: HMAC of this event (covers all fields above).
    """

    timestamp: str
    event_type: str
    actor: str
    resource_type: str
    resource_id: str
    details: dict[str, Any] = field(default_factory=dict)
    prev_hmac: str = _GENESIS_HMAC
    hmac: str = ""


def _compute_hmac(key: bytes, prev_hmac: str, entry: dict[str, Any]) -> str:
    """Compute HMAC-SHA256 over the previous HMAC concatenated with the canonical JSON payload."""
    payload = prev_hmac + json.dumps(entry, sort_keys=True)
    return _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


class AuditLog:
    """Append-only HMAC-chained audit log with daily rotation.

    Args:
        audit_dir: Directory for daily JSONL log files.
        key: HMAC key bytes.  If ``None``, the key is loaded from
            ``<audit_dir>/../config/audit-key`` (created if absent).
    """

    def __init__(self, audit_dir: Path, key: bytes | None = None) -> None:
        self._audit_dir = audit_dir
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        self._key = key if key is not None else self._load_or_create_key()
        self._prev_hmac = self._recover_chain_tail()

    # -- key management -----------------------------------------------------

    def _load_or_create_key(self) -> bytes:
        """Read the HMAC key from disk, or generate one."""
        key_path = self._audit_dir.parent / "config" / "audit-key"
        if key_path.exists():
            return key_path.read_bytes().strip()
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32).encode()
        key_path.write_bytes(key)
        key_path.chmod(0o600)
        return key

    # -- chain recovery -----------------------------------------------------

    def _recover_chain_tail(self) -> str:
        """Walk existing logs to find the last HMAC in the chain."""
        log_files = sorted(self._audit_dir.glob("*.jsonl"))
        if not log_files:
            return _GENESIS_HMAC
        last_file = log_files[-1]
        lines = last_file.read_text().strip().splitlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict) and "hmac" in entry:
                    return str(entry["hmac"])
            except (json.JSONDecodeError, KeyError):
                pass
        return _GENESIS_HMAC

    # -- write --------------------------------------------------------------

    def log(
        self,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Create an audit event, compute its HMAC, and append to the daily log.

        Args:
            event_type: Category of the event.
            actor: Who/what triggered the event.
            resource_type: Type of resource affected.
            resource_id: ID of the affected resource.
            details: Optional structured data about the event.

        Returns:
            The newly created AuditEvent with computed HMAC.
        """
        ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        entry_dict: dict[str, Any] = {
            "timestamp": ts,
            "event_type": event_type,
            "actor": actor,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "details": details or {},
            "prev_hmac": self._prev_hmac,
        }
        computed_hmac = _compute_hmac(self._key, self._prev_hmac, entry_dict)

        event = AuditEvent(
            timestamp=ts,
            event_type=event_type,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
            prev_hmac=self._prev_hmac,
            hmac=computed_hmac,
        )

        entry_dict["hmac"] = computed_hmac
        day = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        log_path = self._audit_dir / f"{day}.jsonl"

        with log_path.open("a") as fh:
            fh.write(json.dumps(entry_dict, sort_keys=True) + "\n")

        self._prev_hmac = computed_hmac
        return event

    # -- verify -------------------------------------------------------------

    def verify(self) -> tuple[bool, list[str]]:
        """Walk all JSONL files and verify the HMAC chain.

        Returns:
            ``(valid, errors)`` where *valid* is True when the entire chain
            is intact and *errors* lists any violations found.
        """
        errors: list[str] = []
        log_files = sorted(self._audit_dir.glob("*.jsonl"))
        if not log_files:
            return True, []

        prev_hmac = _GENESIS_HMAC
        for log_path in log_files:
            for line_no, raw in enumerate(log_path.read_text().splitlines(), start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError as exc:
                    errors.append(f"{log_path.name}:{line_no}: invalid JSON — {exc}")
                    continue

                stored_hmac = entry.pop("hmac", "")
                if entry.get("prev_hmac") != prev_hmac:
                    errors.append(
                        f"{log_path.name}:{line_no}: prev_hmac mismatch "
                        f"(expected {prev_hmac[:16]}…, got {str(entry.get('prev_hmac', ''))[:16]}…)"
                    )

                expected_hmac = _compute_hmac(self._key, prev_hmac, entry)
                if stored_hmac != expected_hmac:
                    errors.append(
                        f"{log_path.name}:{line_no}: HMAC mismatch "
                        f"(expected {expected_hmac[:16]}…, got {stored_hmac[:16]}…)"
                    )

                prev_hmac = stored_hmac

        return len(errors) == 0, errors

    # -- query --------------------------------------------------------------

    def query(
        self,
        *,
        event_type: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[AuditEvent]:
        """Filter audit events by type, actor, and/or time range.

        Args:
            event_type: If set, only return events matching this type.
            actor: If set, only return events from this actor.
            since: ISO 8601 lower bound (inclusive).
            until: ISO 8601 upper bound (inclusive).

        Returns:
            List of matching AuditEvent instances (chronological order).
        """
        results: list[AuditEvent] = []
        log_files = sorted(self._audit_dir.glob("*.jsonl"))

        for log_path in log_files:
            for raw in log_path.read_text().splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if event_type and entry.get("event_type") != event_type:
                    continue
                if actor and entry.get("actor") != actor:
                    continue
                ts = entry.get("timestamp", "")
                if since and ts < since:
                    continue
                if until and ts > until:
                    continue

                results.append(
                    AuditEvent(
                        timestamp=entry.get("timestamp", ""),
                        event_type=entry.get("event_type", ""),
                        actor=entry.get("actor", ""),
                        resource_type=entry.get("resource_type", ""),
                        resource_id=entry.get("resource_id", ""),
                        details=entry.get("details", {}),
                        prev_hmac=entry.get("prev_hmac", ""),
                        hmac=entry.get("hmac", ""),
                    )
                )

        return results
