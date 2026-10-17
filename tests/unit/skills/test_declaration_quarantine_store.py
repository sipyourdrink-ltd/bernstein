"""Issue #5108 slice 2: a persisted record and one journaled event per quarantine.

``SkillLoader``'s in-process isolation (tested in ``test_loader_quarantine.py``)
already keeps one bad source from taking the rest down, and reports the
failure through its ``on_quarantine`` hook. What vanishes when the process
exits is the record of it -- nothing durable, nothing ``doctor`` or ``status``
can read later. ``DeclarationQuarantineStore`` and ``journaled_quarantine_hook``
are that missing half: a persisted entry, and one ``declaration.quarantined``
audit-chain event, per quarantine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.skills.loader import QuarantinedSkill
from bernstein.core.skills.quarantine_store import (
    KIND_SKILL,
    DeclarationQuarantineEntry,
    DeclarationQuarantineStore,
    journaled_quarantine_hook,
)

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"test-hmac-key-not-a-secret-0123456789"


def _record(reason: str = "bad manifest", skill_name: str | None = "widget") -> QuarantinedSkill:
    return QuarantinedSkill(
        source_name="local",
        origin="templates/skills/widget",
        skill_name=skill_name,
        reason=reason,
        error_type="ValueError",
        at=1234.5,
    )


class TestDeclarationQuarantineStore:
    def test_a_fresh_store_has_no_history(self, tmp_path: Path) -> None:
        store = DeclarationQuarantineStore(tmp_path / "quarantine.json")
        assert store.load() == []

    def test_record_persists_across_store_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "quarantine.json"
        entry = DeclarationQuarantineEntry(
            kind=KIND_SKILL,
            source_name="local",
            origin="templates/skills/widget",
            name="widget",
            reason="bad manifest",
            error_type="ValueError",
            at=100.0,
        )
        DeclarationQuarantineStore(path).record(entry)

        reloaded = DeclarationQuarantineStore(path).load()
        assert reloaded == [entry]

    def test_record_appends_rather_than_replaces(self, tmp_path: Path) -> None:
        path = tmp_path / "quarantine.json"
        store = DeclarationQuarantineStore(path)
        for i in range(3):
            store.record(
                DeclarationQuarantineEntry(
                    kind=KIND_SKILL,
                    source_name="local",
                    origin=f"templates/skills/widget-{i}",
                    name=f"widget-{i}",
                    reason="bad manifest",
                    error_type="ValueError",
                    at=float(i),
                )
            )
        assert [e.name for e in store.load()] == ["widget-0", "widget-1", "widget-2"]

    def test_an_unreadable_file_degrades_to_empty_history(self, tmp_path: Path) -> None:
        path = tmp_path / "quarantine.json"
        path.write_text("not json at all", encoding="utf-8")
        assert DeclarationQuarantineStore(path).load() == []


class TestJournaledQuarantineHook:
    def test_a_quarantine_produces_exactly_one_persisted_entry(self, tmp_path: Path) -> None:
        store = DeclarationQuarantineStore(tmp_path / "quarantine.json")
        chain = AuditChainStore(tmp_path / "audit", key=_KEY)
        hook = journaled_quarantine_hook(store, chain)

        hook(_record())

        (entry,) = store.load()
        assert entry.kind == KIND_SKILL
        assert entry.name == "widget"
        assert entry.reason == "bad manifest"
        assert entry.error_type == "ValueError"
        assert entry.at == 1234.5

    def test_a_quarantine_journals_exactly_one_governance_event(self, tmp_path: Path) -> None:
        store = DeclarationQuarantineStore(tmp_path / "quarantine.json")
        chain = AuditChainStore(tmp_path / "audit", key=_KEY)
        hook = journaled_quarantine_hook(store, chain)

        hook(_record())

        events = [e for e in chain.query() if e.event_type == "declaration.quarantined"]
        assert len(events) == 1
        assert events[0].details["name"] == "widget"
        assert events[0].details["reason"] == "bad manifest"
        assert events[0].details["kind"] == KIND_SKILL

    def test_a_whole_source_failure_journals_an_empty_name_not_a_fabricated_one(self, tmp_path: Path) -> None:
        """A source that throws in ``iter_skills`` never named a skill; nothing here should invent one."""
        store = DeclarationQuarantineStore(tmp_path / "quarantine.json")
        chain = AuditChainStore(tmp_path / "audit", key=_KEY)
        hook = journaled_quarantine_hook(store, chain)

        hook(_record(skill_name=None))

        (entry,) = store.load()
        assert entry.name is None
        events = [e for e in chain.query() if e.event_type == "declaration.quarantined"]
        assert events[0].details["name"] == ""

    def test_two_quarantines_produce_two_entries_and_two_events(self, tmp_path: Path) -> None:
        store = DeclarationQuarantineStore(tmp_path / "quarantine.json")
        chain = AuditChainStore(tmp_path / "audit", key=_KEY)
        hook = journaled_quarantine_hook(store, chain)

        hook(_record(reason="first failure", skill_name="alpha"))
        hook(_record(reason="second failure", skill_name="beta"))

        assert [e.name for e in store.load()] == ["alpha", "beta"]
        events = [e for e in chain.query() if e.event_type == "declaration.quarantined"]
        assert len(events) == 2


class TestLoaderWiredToTheJournaledHook:
    """The end-to-end wiring the loader's own tests stop short of: a real store and chain."""

    def test_a_loader_quarantine_reaches_the_store_and_the_chain(self, tmp_path: Path) -> None:
        from bernstein.core.skills.loader import SkillLoader
        from bernstein.core.skills.source import SkillSource

        class _ThrowingSource(SkillSource):
            @property
            def name(self) -> str:
                return "broken"

            def iter_skills(self) -> list:  # type: ignore[type-arg]
                raise RuntimeError("cannot enumerate")

        store = DeclarationQuarantineStore(tmp_path / "quarantine.json")
        chain = AuditChainStore(tmp_path / "audit", key=_KEY)

        SkillLoader([_ThrowingSource()], on_quarantine=journaled_quarantine_hook(store, chain))

        (entry,) = store.load()
        assert entry.source_name == "broken"
        assert entry.reason == "cannot enumerate"
        events = [e for e in chain.query() if e.event_type == "declaration.quarantined"]
        assert len(events) == 1
