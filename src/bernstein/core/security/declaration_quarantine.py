"""One-shot declaration quarantine, separate from threshold-based task quarantine.

Markers never expire. Only an explicit, journaled re-enable releases them.
Workspace writes are atomic and serialized through the audit chain transaction.
The store and entry design follows the one proposed in #5750.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from bernstein.core.persistence.atomic_write import write_atomic_json

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore


@dataclass(frozen=True)
class DeclarationQuarantineEntry:
    """A declaration disabled by its first initialization failure."""

    declaration_id: str
    kind: str
    source_name: str
    origin: str
    skill_name: str | None
    reason: str
    error_type: str
    at: float
    disabled_by_failure: bool = True


def declaration_id(kind: str, source_name: str, origin: str, skill_name: str | None) -> str:
    """Stable identity, including the origin so duplicate names stay distinct."""
    raw = json.dumps([kind, source_name, origin, skill_name], ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class DeclarationQuarantineStore:
    """Workspace-scoped disabled markers and their governance events.

    An absent file means no quarantines; damaged state raises rather than
    silently re-enabling declarations. Construction and reads do not create
    directories or audit keys.
    """

    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir.resolve()
        self.path = self.workdir / ".sdd/runtime/declaration-quarantine.json"

    def load(self) -> list[DeclarationQuarantineEntry]:
        """Read active markers, preserving failures until explicit release."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("Declaration quarantine state must be a list")
        entries = [DeclarationQuarantineEntry(**item) for item in items]
        for entry in entries:
            if entry.disabled_by_failure is not True or entry.declaration_id != declaration_id(
                entry.kind, entry.source_name, entry.origin, entry.skill_name
            ):
                raise ValueError("Invalid declaration quarantine marker")
        return entries

    def _chain(self) -> AuditChainStore:
        from bernstein.core.security.audit_chain import AuditChainStore

        return AuditChainStore(self.workdir / ".sdd/audit")

    def record_failure(self, entry: DeclarationQuarantineEntry) -> None:
        """Disable once and journal the original reason and timestamp once."""
        from bernstein.core.security.audit_chain import EVENT_DECLARATION_QUARANTINED

        chain = self._chain()
        with chain.chain_transaction():
            entries = self.load()
            if any(e.declaration_id == entry.declaration_id for e in entries):
                return
            # Persist the blocking marker first: a failed audit sink must
            # never release a known-broken declaration for another attempt.
            write_atomic_json(self.path, [asdict(e) for e in [*entries, entry]])
            chain.log_with_prev_digest(
                event_type=EVENT_DECLARATION_QUARANTINED,
                actor="declaration-loader",
                resource_type="declaration",
                resource_id=entry.declaration_id,
                details=asdict(entry),
            )

    def reenable(self, entry_id: str, *, actor: str) -> bool:
        """Journal an explicit operator release before removing its marker."""
        from bernstein.core.security.audit_chain import EVENT_DECLARATION_REENABLED

        if not actor.strip():
            raise ValueError("Re-enable requires an actor")
        chain = self._chain()
        with chain.chain_transaction():
            entries = self.load()
            entry = next((e for e in entries if e.declaration_id == entry_id), None)
            if entry is None:
                return False
            chain.log_with_prev_digest(
                event_type=EVENT_DECLARATION_REENABLED,
                actor=actor,
                resource_type="declaration",
                resource_id=entry_id,
                details={"declaration": asdict(entry), "at": time.time()},
            )
            write_atomic_json(self.path, [asdict(e) for e in entries if e.declaration_id != entry_id])
        return True


def quarantine_summary(workdir: Path) -> tuple[str, bool, str, str]:
    """Shared doctor/audit summary; reads state without initializing declarations."""
    store = DeclarationQuarantineStore(workdir)
    try:
        entries = store.load()
    except (OSError, ValueError, TypeError) as exc:
        return "Declaration quarantine", False, f"Cannot read {store.path}: {exc}", "Repair the quarantine state file."
    detail = "No quarantined declarations."
    if entries:
        detail = "; ".join(
            f"{e.kind} {e.source_name} ({e.origin}): {e.reason} [at={e.at}, id={e.declaration_id}]" for e in entries
        )
    return (
        "Declaration quarantine",
        not entries,
        detail,
        "Fix the declaration, then run bernstein quarantine re-enable DECLARATION_ID." if entries else "",
    )
