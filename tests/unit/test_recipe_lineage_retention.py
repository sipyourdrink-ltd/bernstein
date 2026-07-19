"""Absence of evidence is not evidence of a defect (#2654).

A definition lineage can be incomplete for two very different reasons, and
conflating them turns routine housekeeping into a permanent accusation:

- **contradiction** - two receipts claim the same predecessor, or the chain
  itself does not verify. Something is wrong. Refuse.
- **absence** - a predecessor is not in the live segments. Before concluding
  anything, look where the verifier looks: ``verify`` has replayed archived
  segments since #1835, so an archived predecessor is *present*, just not in
  the default window. Retention then costs nothing at all.

Only when a receipt is in neither the live nor the archived log is there a
real question, and the audit chain - the tamper detector - answers it: chain
broken means evidence, chain intact means an unattributable absence that is
reported rather than refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.security.audit import AuditLog, RetentionPolicy
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.workflows.recipe_registry import (
    RecipePins,
    RecipeRegistry,
    RecipeRegistryError,
)
from bernstein.core.workflows.recipe_spec import load_recipe_spec_from_text

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32
_V1 = 'name: nightly\ndescription: d\nversion: "1.0.0"\nnodes:\n  - id: n\n    command: "echo a"\n'
_V2 = 'name: nightly\ndescription: d\nversion: "2.0.0"\nnodes:\n  - id: n\n    command: "echo b"\n'


def _registry(sdd: Path) -> RecipeRegistry:
    sdd.mkdir(parents=True, exist_ok=True)
    return RecipeRegistry(
        sdd,
        chain=AuditChainStore(sdd / "audit", key=_KEY),
        hmac_key=_KEY,
        lineage_key=_KEY,
    )


def _archive_the_genesis_segment(sdd: Path) -> None:
    """Register v1, age its segment, register v2, then run ordinary retention."""
    audit = sdd / "audit"
    reg = _registry(sdd)
    reg.register(spec=load_recipe_spec_from_text(_V1), pins=RecipePins(git_commit="c1"))
    sorted(audit.glob("*.jsonl"))[0].rename(audit / "2020-01-01.jsonl")
    _registry(sdd).register(spec=load_recipe_spec_from_text(_V2), pins=RecipePins(git_commit="c2"))
    AuditLog(audit_dir=audit, key=_KEY).archive(RetentionPolicy())


class TestRetentionDoesNotBrickAnHonestRecipe:
    def test_every_operation_still_works_after_retention(self, tmp_path: Path) -> None:
        """The operator did nothing wrong; the recipe must keep working."""
        sdd = tmp_path / ".sdd"
        _archive_the_genesis_segment(sdd)
        reg = _registry(sdd)

        chain_ok, _errors = reg._get_chain().verify()
        assert chain_ok, "precondition: the chain is intact, only a segment was archived"

        assert reg.live_hash("nightly")
        assert reg.is_paused("nightly") is False
        assert reg.history("nightly")
        assert reg.declared_schedules("nightly") == {}

    def test_retention_costs_nothing_at_all(self, tmp_path: Path) -> None:
        """Archived receipts are present, so there is nothing to degrade.

        The verifier has replayed archived segments since #1835; making the
        projection read them too means an archived genesis registration is
        simply found, and the lineage re-walks in full.
        """
        sdd = tmp_path / ".sdd"
        _archive_the_genesis_segment(sdd)
        reg = _registry(sdd)

        assert reg.lineage_note("nightly") == "", "retention must not degrade an intact lineage"
        ok, errors = reg.verify_history("nightly")
        assert ok is True, f"an archived-but-intact lineage must verify: {errors}"
        assert len(reg.history("nightly")) == 2, "both receipts re-walked, including the archived one"

    def test_a_fleet_plan_over_an_archived_recipe_still_works(self, tmp_path: Path) -> None:
        from bernstein.core.workflows.recipe_fleet import ManifestEntry, plan_fleet

        sdd = tmp_path / ".sdd"
        _archive_the_genesis_segment(sdd)
        reg = _registry(sdd)
        plan = plan_fleet(reg, [ManifestEntry(spec=load_recipe_spec_from_text(_V2))])
        assert plan.plan_hash


class TestTamperingIsStillRefused:
    def test_missing_receipt_with_a_broken_chain_is_refused(self, tmp_path: Path) -> None:
        """The same absence, but the chain does not verify: that is evidence."""
        sdd = tmp_path / ".sdd"
        sdd.mkdir(parents=True)
        audit = sdd / "audit"
        reg = _registry(sdd)
        reg.register(spec=load_recipe_spec_from_text(_V1), pins=RecipePins(git_commit="c1"))
        _registry(sdd).register(spec=load_recipe_spec_from_text(_V2), pins=RecipePins(git_commit="c2"))

        segment = sorted(audit.glob("*.jsonl"))[0]
        lines = segment.read_text().splitlines()
        segment.write_text("\n".join(lines[1:]) + "\n")  # excise the genesis receipt

        fresh = _registry(sdd)
        chain_ok, _errors = fresh._get_chain().verify()
        assert chain_ok is False, "precondition: excising a line breaks the chain"

        with pytest.raises(RecipeRegistryError, match="tampering"):
            fresh.live_hash("nightly")

    def test_the_refusal_names_which_case_it_hit(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir(parents=True)
        audit = sdd / "audit"
        reg = _registry(sdd)
        reg.register(spec=load_recipe_spec_from_text(_V1), pins=RecipePins(git_commit="c1"))
        _registry(sdd).register(spec=load_recipe_spec_from_text(_V2), pins=RecipePins(git_commit="c2"))
        segment = sorted(audit.glob("*.jsonl"))[0]
        segment.write_text("\n".join(segment.read_text().splitlines()[1:]) + "\n")

        with pytest.raises(RecipeRegistryError) as excinfo:
            _registry(sdd).live_hash("nightly")
        message = str(excinfo.value)
        assert "fails verification" in message
        assert "not of retention" in message


class TestRepairLineageInputValidation:
    @staticmethod
    def _fork(reg: RecipeRegistry) -> list[str]:
        from bernstein.core.security.audit_chain import record_recipe_supersede

        tail = reg._project_name("nightly").last_receipt_hmac
        for new_hash in ("a" * 64, "b" * 64):
            record_recipe_supersede(
                chain=reg._get_chain(),
                name="nightly",
                old_hash="0" * 64,
                new_hash=new_hash,
                spine_anchor="",
                prev_receipt_digest=tail,
                actor="operator",
            )
        return sorted(str(c["hmac"]) for cs in reg.lineage_forks("nightly").values() for c in cs)

    @pytest.fixture
    def forked(self, tmp_path: Path) -> RecipeRegistry:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=load_recipe_spec_from_text(_V1), pins=RecipePins(git_commit="c1"))
        self._fork(reg)
        return reg

    @pytest.mark.parametrize("pick", ["", "   ", "\t", "\n "])
    def test_blank_pick_is_refused(self, forked: RecipeRegistry, pick: str) -> None:
        """A blank pick used to match every branch and resolve to the first.

        The resolution asserts a deliberate operator choice and is not undone
        by removal, so an input naming no branch must never write one.
        """
        with pytest.raises(RecipeRegistryError, match="requires the hmac"):
            forked.repair_lineage("nightly", pick)

    def test_too_short_a_prefix_is_refused(self, forked: RecipeRegistry) -> None:
        with pytest.raises(RecipeRegistryError, match="too short"):
            forked.repair_lineage("nightly", "ab")

    def test_non_hex_pick_is_refused(self, forked: RecipeRegistry) -> None:
        with pytest.raises(RecipeRegistryError, match="not a receipt hmac"):
            forked.repair_lineage("nightly", "zzzzzzzzzz")

    def test_an_ambiguous_prefix_is_refused(self, forked: RecipeRegistry) -> None:
        candidates = sorted(str(c["hmac"]) for cs in forked.lineage_forks("nightly").values() for c in cs)
        shared = ""
        for index in range(_min_len(candidates)):
            if len({c[index] for c in candidates}) > 1:
                break
            shared = candidates[0][: index + 1]
        if len(shared) < 8:
            pytest.skip("candidates diverge before the minimum prefix length")
        with pytest.raises(RecipeRegistryError, match="matches"):
            forked.repair_lineage("nightly", shared)

    def test_a_blank_pick_writes_no_resolution(self, forked: RecipeRegistry) -> None:
        from bernstein.core.security.audit_chain import EVENT_RECIPE_LINEAGE_RESOLVE

        with pytest.raises(RecipeRegistryError):
            forked.repair_lineage("nightly", "  ")
        assert list(forked._get_chain().query(event_type=EVENT_RECIPE_LINEAGE_RESOLVE)) == []

    def test_an_explicit_pick_still_resolves(self, forked: RecipeRegistry) -> None:
        candidates = sorted(str(c["hmac"]) for cs in forked.lineage_forks("nightly").values() for c in cs)
        chosen = forked.repair_lineage("nightly", candidates[0])
        assert chosen == candidates[0]
        assert forked.live_hash("nightly")

    def test_a_wrong_choice_is_correctable(self, forked: RecipeRegistry) -> None:
        """The recovery tool must itself be recoverable from."""
        candidates = sorted(str(c["hmac"]) for cs in forked.lineage_forks("nightly").values() for c in cs)
        forked.repair_lineage("nightly", candidates[0])
        first = forked.live_hash("nightly")
        forked.repair_lineage("nightly", candidates[1])
        assert forked.live_hash("nightly") != first


def _min_len(values: list[str]) -> int:
    return min(len(v) for v in values) if values else 0


class TestWriteLockIsBounded:
    def test_a_second_instance_errors_instead_of_hanging(self, tmp_path: Path, monkeypatch: Any) -> None:
        """flock cannot grant a second descriptor a lock this process holds.

        Blocking forever there leaves a hung process still holding the lock,
        which is the least diagnosable outcome available.
        """
        import threading

        monkeypatch.setattr(
            "bernstein.core.workflows.recipe_registry.WRITE_LOCK_TIMEOUT_S",
            1.0,
        )
        sdd = tmp_path / ".sdd"
        first = _registry(sdd)
        second = _registry(sdd)
        outcome: list[str] = []

        def _nest() -> None:
            try:
                with first.write_lock(), second.write_lock():
                    outcome.append("entered")
            except RecipeRegistryError:
                outcome.append("raised")

        worker = threading.Thread(target=_nest, daemon=True)
        worker.start()
        worker.join(timeout=20)

        assert outcome == ["raised"], f"expected a bounded error, got {outcome or 'a hang'}"
