"""Persisted record of declaration-init quarantines (#5108, slice 2).

``SkillLoader`` already isolates a source or artifact that fails to load: the
rest of the set still registers, and the failure is available in-process as
:class:`~bernstein.core.skills.loader.QuarantinedSkill` via the loader's
``on_quarantine`` hook. What that isolation does not do on its own is leave
anything behind for an operator to read later -- restart the process and the
in-memory record is gone.

This is a sibling of :class:`~bernstein.core.security.quarantine.QuarantineStore`,
not a shared instance of it: that store is threshold-based (a task is
quarantined after ``QUARANTINE_THRESHOLD`` repeated failures across runs), while
a declaration that throws on init is quarantined on the first failure -- there
is no "try it a few more times" for a source that cannot enumerate itself.

:func:`journaled_quarantine_hook` composes the two things slice 2 needs into
one ``on_quarantine`` callback: a persisted entry via :class:`DeclarationQuarantineStore`,
and exactly one ``declaration.quarantined`` audit-chain event.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.atomic_write import write_atomic_json

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.skills.loader import QuarantinedSkill

#: Declaration kinds this store expects. Not enforced -- a caller extending
#: quarantine to adapters or routines (issue #5108 slice 4) passes its own
#: string, and the store persists whatever it is given.
KIND_SKILL = "skill"


@dataclass(frozen=True)
class DeclarationQuarantineEntry:
    """One declaration this install refused to load, and why.

    Attributes:
        kind: What was quarantined -- ``"skill"`` today; a plugin, adapter or
            routine kind string once slice 4 extends this store to them.
        source_name: Label of the source (module, plugin, source label) the
            declaration came from.
        origin: The declaration's origin (a path, plugin name, or entry
            point), or the source label when the whole source failed before
            producing anything to name.
        name: The declaration's own name, or ``None`` for a whole-source
            failure that never got far enough to name one.
        reason: The exception text.
        error_type: The exception class name.
        at: Unix timestamp of the failure.
    """

    kind: str
    source_name: str
    origin: str
    name: str | None
    reason: str
    error_type: str
    at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DeclarationQuarantineEntry:
        return cls(
            kind=str(raw["kind"]),
            source_name=str(raw["source_name"]),
            origin=str(raw["origin"]),
            name=raw.get("name"),
            reason=str(raw["reason"]),
            error_type=str(raw["error_type"]),
            at=float(raw["at"]),
        )


class DeclarationQuarantineStore:
    """Append-only, persisted log of declaration-init quarantines.

    One-shot, unlike the threshold-based task quarantine: every failure
    reported to :meth:`record` is written, with no dedup and no expiry --
    each entry is a fact about a load that happened at a specific time, kept
    for ``doctor``/``status``/re-enable (slices 3+) to read.

    Args:
        path: Full path to the quarantine JSON file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> list[DeclarationQuarantineEntry]:
        """Return every persisted entry, oldest first.

        Returns:
            Entries in the file, or an empty list if it does not exist or is
            unreadable -- a damaged file must not stop a load from
            proceeding; it only means the operator loses the history of past
            quarantines, not that this run's own isolation is affected.
        """
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return [DeclarationQuarantineEntry.from_dict(item) for item in raw]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.error(
                "declaration quarantine: %s is unreadable (%s); treating history as empty",
                self._path,
                exc,
            )
            return []

    def record(self, entry: DeclarationQuarantineEntry) -> None:
        """Append *entry* and persist the full list.

        Args:
            entry: The quarantine record to add.
        """
        entries = self.load()
        entries.append(entry)
        write_atomic_json(self._path, [e.to_dict() for e in entries])


def journaled_quarantine_hook(
    store: DeclarationQuarantineStore,
    chain: AuditChainStore,
    *,
    kind: str = KIND_SKILL,
    actor: str = "skill_loader",
) -> Callable[[QuarantinedSkill], None]:
    """Return an ``on_quarantine`` callback that persists and journals.

    Composes the two things slice 2 needs into the one hook
    :class:`~bernstein.core.skills.loader.SkillLoader` calls at the moment of
    failure: a :class:`DeclarationQuarantineEntry` written to *store*, and
    exactly one ``declaration.quarantined`` event appended to *chain*.

    Args:
        store: Where the persisted record is written.
        chain: The audit chain store accepting the journal entry.
        kind: Declaration kind to stamp on every entry this hook records.
        actor: Recorded actor for the journal entry.

    Returns:
        A callable suitable for ``SkillLoader(..., on_quarantine=...)``.
    """

    def _on_quarantine(record: QuarantinedSkill) -> None:
        from bernstein.core.security.audit_chain import record_declaration_quarantined

        entry = DeclarationQuarantineEntry(
            kind=kind,
            source_name=record.source_name,
            origin=record.origin,
            name=record.skill_name,
            reason=record.reason,
            error_type=record.error_type,
            at=record.at,
        )
        store.record(entry)
        record_declaration_quarantined(
            chain=chain,
            kind=kind,
            source_name=record.source_name,
            origin=record.origin,
            name=record.skill_name or "",
            reason=record.reason,
            error_type=record.error_type,
            actor=actor,
        )

    return _on_quarantine


__all__ = [
    "KIND_SKILL",
    "DeclarationQuarantineEntry",
    "DeclarationQuarantineStore",
    "journaled_quarantine_hook",
]
