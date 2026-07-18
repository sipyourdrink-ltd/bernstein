"""Registry, lineage, and fleet hardening regressions (#2654).

One test per hardened finding:

- ``fire`` never reports ``dispatched=True`` without submitting work and
  appending a fire receipt; a failed submission reports the reason.
- A fire is projected under the schedule that actually triggered it; a
  manual fire stays schedule-neutral.
- ``rollback`` accepts only a hash from the name's own lifecycle lineage.
- Canonical hashing rejects non-string dict keys instead of collapsing
  distinct keys onto one hash.
- A forked receipt lineage fails closed rather than being silently resolved.
- A fleet apply that fails mid-way leaves no half-registered state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.orchestration.schedule_projection import project_schedule_fire
from bernstein.core.security.audit_chain import (
    EVENT_RECIPE_FIRE,
    EVENT_RECIPE_FLEET_APPLY,
    AuditChainStore,
    record_recipe_supersede,
)
from bernstein.core.workflows.recipe_fleet import ManifestEntry, apply_fleet, plan_fleet
from bernstein.core.workflows.recipe_registry import (
    RecipePins,
    RecipeRegistry,
    RecipeRegistryError,
    _canonical_json_value,
)
from bernstein.core.workflows.recipe_spec import load_recipe_spec_from_text

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.workflows.recipe_spec import RecipeSpec

_KEY = b"0" * 32

_MANIFEST = """
name: nightly-triage
description: Nightly triage recipe
version: "1.2.0"
nodes:
  - id: triage
    agent: backend
    prompt: "Triage issues. Goal: {goal}"
schedules:
  - kind: cron
    recurrence: "0 9 * * *"
    timezone: America/New_York
    dst_policy: post_transition
  - kind: cron
    recurrence: "0 21 * * *"
    timezone: Europe/Berlin
    dst_policy: pre_transition
"""

_PLAIN_MANIFEST = """
name: plain
description: A plain zero-block recipe
version: "1.0.0"
nodes:
  - id: n
    command: "echo hi"
"""


def _spec(manifest: str = _MANIFEST) -> RecipeSpec:
    return load_recipe_spec_from_text(manifest)


def _registry(sdd: Path, *, dispatch: Any = None) -> RecipeRegistry:
    sdd.mkdir(parents=True, exist_ok=True)
    return RecipeRegistry(
        sdd,
        chain=AuditChainStore(sdd / "audit", key=_KEY),
        hmac_key=_KEY,
        lineage_key=_KEY,
        dispatch=dispatch,
    )


def _fire_receipts(reg: RecipeRegistry) -> list[Any]:
    return list(reg._get_chain().query(event_type=EVENT_RECIPE_FIRE))


# ---------------------------------------------------------------------------
# critical: fire must submit work and receipt it, or report failure
# ---------------------------------------------------------------------------


class TestFireSubmitsWork:
    def test_fire_reports_failure_when_no_work_is_submitted(self, tmp_path: Path) -> None:
        submitted: list[Any] = []

        def _dispatch(event: Any) -> int:
            submitted.append(event)
            return 0  # the trigger pipeline produced no task

        reg = _registry(tmp_path / ".sdd", dispatch=_dispatch)
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is False
        assert result.reason
        assert len(submitted) == 1
        # A fire that submitted nothing must not leave a fire receipt behind.
        assert _fire_receipts(reg) == []

    def test_fire_reports_failure_when_dispatcher_raises(self, tmp_path: Path) -> None:
        def _dispatch(_event: Any) -> int:
            raise RuntimeError("pipeline down")

        reg = _registry(tmp_path / ".sdd", dispatch=_dispatch)
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is False
        assert "pipeline down" in result.reason
        assert _fire_receipts(reg) == []

    def test_fire_without_a_dispatcher_reports_failure(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        # No dispatcher wired and no trigger pipeline resolvable in a bare
        # .sdd directory: the fire must report failure, never dispatched.
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)
        if result.dispatched:
            pytest.fail("a fire with no submitted work reported dispatched=True")
        assert result.reason
        assert _fire_receipts(reg) == []

    def test_dispatched_fire_submits_and_appends_a_receipt(self, tmp_path: Path) -> None:
        submitted: list[Any] = []

        def _dispatch(event: Any) -> int:
            submitted.append(event)
            return 1

        reg = _registry(tmp_path / ".sdd", dispatch=_dispatch)
        registered = reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is True
        assert result.projection_hash
        assert len(submitted) == 1
        assert submitted[0].metadata["projection_hash"] == result.projection_hash

        receipts = _fire_receipts(reg)
        assert len(receipts) == 1
        details = dict(receipts[0].details)
        assert details["recipe_hash"] == registered.recipe_hash
        assert details["projection_hash"] == result.projection_hash
        assert details["fire_time"] == 1_800_000_000
        assert details["submitted"] == 1
        # The chain anchor is the fire receipt itself, not an unrelated tail.
        assert result.chain_anchor == receipts[0].hmac

    def test_paused_recipe_submits_nothing(self, tmp_path: Path) -> None:
        submitted: list[Any] = []
        reg = _registry(tmp_path / ".sdd", dispatch=lambda e: submitted.append(e) or 1)
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        reg.pause("nightly-triage")
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)
        assert result.dispatched is False
        assert submitted == []
        assert _fire_receipts(reg) == []


# ---------------------------------------------------------------------------
# a fire is projected under the schedule that triggered it
# ---------------------------------------------------------------------------


class TestFireScheduleAttribution:
    def test_fire_projects_under_the_triggering_schedule(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: 1)
        registered = reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        first, second = registered.schedules

        result = reg.fire(
            "nightly-triage",
            fire_time=1_800_000_000,
            schedule_id=second.schedule_id,
        )
        assert result.dispatched is True
        assert result.schedule_id == second.schedule_id

        expected = project_schedule_fire(
            schedule_id=registered.recipe_hash,
            fire_time=1_800_000_000,
            last_state=None,
            goal="",
            recurrence=second.recurrence,
            timezone=second.timezone,
            dst_policy=second.dst_policy,
        )
        assert result.projection_hash == expected.projection_hash

        # The old behaviour projected every fire under schedules[0]; the
        # second schedule must not collapse onto the first.
        collapsed = project_schedule_fire(
            schedule_id=registered.recipe_hash,
            fire_time=1_800_000_000,
            last_state=None,
            goal="",
            recurrence=first.recurrence,
            timezone=first.timezone,
            dst_policy=first.dst_policy,
        )
        assert result.projection_hash != collapsed.projection_hash

    def test_manual_fire_is_schedule_neutral(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: 1)
        registered = reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.schedule_id == ""
        neutral = project_schedule_fire(
            schedule_id=registered.recipe_hash,
            fire_time=1_800_000_000,
            last_state=None,
            goal="",
        )
        assert result.projection_hash == neutral.projection_hash

    def test_unknown_schedule_id_is_rejected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: 1)
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        with pytest.raises(RecipeRegistryError):
            reg.fire("nightly-triage", fire_time=1_800_000_000, schedule_id="sched_deadbeef")

    def test_declared_schedule_ids_are_content_derived(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: 1)
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        declared = reg.declared_schedules("nightly-triage")
        assert len(declared) == 2
        # Distinct schedules get distinct ids and are stable across reads.
        assert len(set(declared)) == 2
        assert declared == reg.declared_schedules("nightly-triage")


# ---------------------------------------------------------------------------
# rollback is confined to the name's own lineage
# ---------------------------------------------------------------------------


class TestRollbackLineage:
    def test_rollback_refuses_a_blob_outside_the_names_lineage(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        other = reg.register(spec=_spec(_PLAIN_MANIFEST), pins=RecipePins(git_commit="c1"))

        # ``other.recipe_hash`` is a globally known blob but belongs to a
        # different name; rolling back onto it must be refused.
        assert reg.get_canonical_bytes(other.recipe_hash) is not None
        with pytest.raises(RecipeRegistryError):
            reg.rollback("nightly-triage", other.recipe_hash)

    def test_rollback_accepts_a_hash_from_the_names_own_history(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        first = reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        second = reg.register(spec=_spec(), pins=RecipePins(git_commit="c2"))
        assert first.recipe_hash != second.recipe_hash

        reg.rollback("nightly-triage", first.recipe_hash)
        assert reg.live_hash("nightly-triage") == first.recipe_hash


# ---------------------------------------------------------------------------
# canonical hashing does not collapse distinct keys
# ---------------------------------------------------------------------------


class TestCanonicalKeys:
    def test_distinct_non_string_keys_do_not_collapse(self) -> None:
        # ``1`` and ``"1"`` used to both stringify to ``"1"`` and collapse
        # onto a single entry, silently dropping one of the values.
        with pytest.raises(RecipeRegistryError):
            _canonical_json_value({1: "a", "1": "b"})

    def test_non_string_key_is_rejected(self) -> None:
        with pytest.raises(RecipeRegistryError):
            _canonical_json_value({"triggers": [{2: "kind"}]})

    def test_string_keyed_bodies_still_canonicalise(self) -> None:
        assert _canonical_json_value({"b": 1, "a": {"d": 2, "c": 3}}) == {"a": {"c": 3, "d": 2}, "b": 1}

    def test_registering_a_non_string_keyed_trigger_is_rejected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        spec = _spec(_PLAIN_MANIFEST)
        object.__setattr__(spec, "triggers", [{3: "webhook"}])
        with pytest.raises(RecipeRegistryError):
            reg.register(spec=spec, pins=RecipePins(git_commit="c1"))


# ---------------------------------------------------------------------------
# a forked receipt lineage fails closed
# ---------------------------------------------------------------------------


class TestLineageForkFailsClosed:
    @staticmethod
    def _fork(reg: RecipeRegistry, name: str) -> None:
        """Append a second receipt claiming the same predecessor."""
        record_recipe_supersede(
            chain=reg._get_chain(),
            name=name,
            old_hash="a" * 64,
            new_hash="b" * 64,
            spine_anchor="",
            prev_receipt_digest="",  # genesis is already taken by the register
            actor="operator",
        )

    def test_forked_lineage_is_rejected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        self._fork(reg, "nightly-triage")
        with pytest.raises(RecipeRegistryError):
            reg.live_hash("nightly-triage")

    def test_verify_history_reports_the_fork(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        self._fork(reg, "nightly-triage")
        ok, errors = reg.verify_history("nightly-triage")
        assert ok is False
        assert any("fork" in e.lower() for e in errors)

    def test_unreachable_receipt_is_rejected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        record_recipe_supersede(
            chain=reg._get_chain(),
            name="nightly-triage",
            old_hash="a" * 64,
            new_hash="b" * 64,
            spine_anchor="",
            prev_receipt_digest="f" * 64,  # links to a receipt that does not exist
            actor="operator",
        )
        with pytest.raises(RecipeRegistryError):
            reg.live_hash("nightly-triage")

    def test_intact_lineage_still_projects(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        second = reg.register(spec=_spec(), pins=RecipePins(git_commit="c2"))
        reg.pause("nightly-triage")
        assert reg.live_hash("nightly-triage") == second.recipe_hash
        assert reg.is_paused("nightly-triage") is True
        ok, errors = reg.verify_history("nightly-triage")
        assert ok, errors


# ---------------------------------------------------------------------------
# fleet apply is all-or-nothing
# ---------------------------------------------------------------------------


class TestFleetAtomicity:
    def test_partial_apply_leaves_no_half_registered_state(self, tmp_path: Path, monkeypatch: Any) -> None:
        sdd = tmp_path / ".sdd"
        reg = _registry(sdd)
        manifest = [
            ManifestEntry(spec=_spec()),
            ManifestEntry(spec=_spec(_PLAIN_MANIFEST)),
        ]
        plan = plan_fleet(reg, manifest)
        assert set(plan.to_register) == {"nightly-triage", "plain"}

        real_seal = RecipeRegistry._seal
        calls: list[str] = []

        def _flaky_seal(self: RecipeRegistry, canonical_bytes: bytes, recipe_hash: str) -> str:
            calls.append(recipe_hash)
            if len(calls) > 1:
                raise OSError("lineage spine unavailable")
            return real_seal(self, canonical_bytes, recipe_hash)

        monkeypatch.setattr(RecipeRegistry, "_seal", _flaky_seal)

        with pytest.raises(OSError, match="lineage spine unavailable"):
            apply_fleet(reg, manifest, plan_hash=plan.plan_hash)

        # Nothing half-registered: neither name has a live hash and no
        # aggregate receipt claims the apply happened.
        fresh = _registry(sdd)
        assert fresh.live_hash("nightly-triage") is None
        assert fresh.live_hash("plain") is None
        assert list(fresh._get_chain().query(event_type=EVENT_RECIPE_FLEET_APPLY)) == []

    def test_successful_apply_registers_everything_with_a_receipt(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        reg = _registry(sdd)
        manifest = [
            ManifestEntry(spec=_spec()),
            ManifestEntry(spec=_spec(_PLAIN_MANIFEST)),
        ]
        plan = plan_fleet(reg, manifest)
        applied = apply_fleet(reg, manifest, plan_hash=plan.plan_hash)
        assert set(applied) == {"nightly-triage", "plain"}

        fresh = _registry(sdd)
        assert fresh.live_hash("nightly-triage")
        assert fresh.live_hash("plain")
        receipts = list(fresh._get_chain().query(event_type=EVENT_RECIPE_FLEET_APPLY))
        assert len(receipts) == 1
        assert receipts[0].details["plan_hash"] == plan.plan_hash
