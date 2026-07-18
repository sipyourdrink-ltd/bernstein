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

        def _dispatch(event: Any) -> list[str]:
            submitted.append(event)
            return []  # the sink accepted nothing

        reg = _registry(tmp_path / ".sdd", dispatch=_dispatch)
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)

        assert result.dispatched is False
        assert result.reason
        assert len(submitted) == 1
        # A fire that submitted nothing must not leave a fire receipt behind.
        assert _fire_receipts(reg) == []

    def test_fire_reports_failure_when_dispatcher_raises(self, tmp_path: Path) -> None:
        def _dispatch(_event: Any) -> list[str]:
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

        def _dispatch(event: Any) -> list[str]:
            submitted.append(event)
            return ["T-001"]

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
        assert details["submitted_ids"] == ["T-001"]
        # The chain anchor is the fire receipt itself, not an unrelated tail.
        assert result.chain_anchor == receipts[0].hmac

    def test_paused_recipe_submits_nothing(self, tmp_path: Path) -> None:
        submitted: list[Any] = []
        reg = _registry(tmp_path / ".sdd", dispatch=lambda e: submitted.append(e) or ["T-001"])
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
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: ["T-001"])
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
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: ["T-001"])
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
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: ["T-001"])
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        with pytest.raises(RecipeRegistryError):
            reg.fire("nightly-triage", fire_time=1_800_000_000, schedule_id="sched_deadbeef")

    def test_declared_schedule_ids_are_content_derived(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd", dispatch=lambda _e: ["T-001"])
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

    def test_commit_phase_failure_is_detectable_not_silent(self, tmp_path: Path, monkeypatch: Any) -> None:
        """Pins the documented commit-phase guarantee.

        Phase 2 appends to an append-only chain and cannot be rolled back, so
        the contract promises detectability rather than atomicity: the applied
        prefix is registered, the aggregate receipt is absent, and a re-plan
        surfaces the remainder as still pending.
        """
        import bernstein.core.security.audit_chain as audit_chain

        sdd = tmp_path / ".sdd"
        reg = _registry(sdd)
        manifest = [
            ManifestEntry(spec=_spec()),
            ManifestEntry(spec=_spec(_PLAIN_MANIFEST)),
        ]
        plan = plan_fleet(reg, manifest)

        real_register = audit_chain.record_recipe_register
        calls: list[str] = []

        def _flaky_register(**kwargs: Any) -> Any:
            calls.append(str(kwargs["name"]))
            if len(calls) > 1:
                raise OSError("audit chain IO error")
            return real_register(**kwargs)

        monkeypatch.setattr(audit_chain, "record_recipe_register", _flaky_register)
        with pytest.raises(OSError, match="audit chain IO error"):
            apply_fleet(reg, manifest, plan_hash=plan.plan_hash)
        monkeypatch.undo()

        fresh = _registry(sdd)
        committed = [n for n in ("nightly-triage", "plain") if fresh.live_hash(n)]
        pending = [n for n in ("nightly-triage", "plain") if not fresh.live_hash(n)]
        assert len(committed) == 1, "the appended prefix stays registered"
        assert len(pending) == 1
        # The absent aggregate receipt is what makes the partial apply
        # detectable rather than passing for a completed one.
        assert list(fresh._get_chain().query(event_type=EVENT_RECIPE_FLEET_APPLY)) == []
        # A re-plan surfaces the remainder as still pending.
        replan = plan_fleet(fresh, manifest)
        assert set(replan.to_register) == set(pending)
        assert set(replan.unchanged) == set(committed)

    def test_nested_write_lock_does_not_deadlock(self, tmp_path: Path) -> None:
        """A locking call from inside the locked section must not hang.

        ``flock`` is per file descriptor, so a nested acquisition from the
        same process would block forever on a lock it already holds - no
        error, no timeout, just a hung process still holding it. ``apply_fleet``
        runs its whole body inside the lock, so this is one edit away.
        """
        import threading

        reg = _registry(tmp_path / ".sdd")
        done = threading.Event()

        def _nest() -> None:
            with reg.write_lock(), reg.write_lock():
                reg.register(spec=_spec(_PLAIN_MANIFEST), pins=RecipePins(git_commit="c1"))
            done.set()

        worker = threading.Thread(target=_nest, daemon=True)
        worker.start()
        worker.join(timeout=20)
        assert done.is_set(), "nested write_lock() deadlocked"
        assert reg.live_hash("plain")

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


# ---------------------------------------------------------------------------
# a forked lineage is recoverable, and concurrent writes do not create one
# ---------------------------------------------------------------------------


class TestForkRecovery:
    @staticmethod
    def _fork_by_race(reg: RecipeRegistry) -> str:
        """Fork the lineage the way an unlocked concurrent write would.

        Read the tail (what an unlocked _set_pause does), let another write
        land, then append against the stale tail.
        """
        from bernstein.core.security.audit_chain import record_recipe_pause

        tail = reg._project_name("nightly-triage").last_receipt_hmac
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c2"))
        record_recipe_pause(
            chain=reg._get_chain(),
            name="nightly-triage",
            recipe_hash="a" * 64,
            paused=True,
            prev_receipt_digest=tail,
            actor="operator",
        )
        return tail

    def test_a_forked_name_is_recoverable(self, tmp_path: Path) -> None:
        """Failing closed is right; leaving no way back is not."""
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        self._fork_by_race(reg)

        with pytest.raises(RecipeRegistryError):
            reg.live_hash("nightly-triage")

        forks = reg.lineage_forks("nightly-triage")
        assert forks, "the operator must be able to see the competing branches"
        candidates = next(iter(forks.values()))
        chosen = str(candidates[0]["hmac"])

        reg.repair_lineage("nightly-triage", chosen)
        # The name works again, and nothing was deleted to achieve it.
        assert reg.live_hash("nightly-triage")
        assert reg.is_paused("nightly-triage") in (True, False)
        assert len(reg.history("nightly-triage")) >= 2

    def test_the_fork_error_names_the_recovery_command(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        self._fork_by_race(reg)
        with pytest.raises(RecipeRegistryError, match="repair-lineage"):
            reg.live_hash("nightly-triage")

    def test_repair_rejects_a_receipt_outside_the_fork(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        self._fork_by_race(reg)
        with pytest.raises(RecipeRegistryError, match="not a competing successor"):
            reg.repair_lineage("nightly-triage", "f" * 64)

    def test_repair_without_a_fork_is_refused(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        with pytest.raises(RecipeRegistryError, match="no unresolved"):
            reg.repair_lineage("nightly-triage", "a" * 64)

    def test_pause_and_rollback_serialise_against_register(self, tmp_path: Path) -> None:
        """pause / resume / rollback must hold the same lock register does.

        Without it, read-tail-then-append races an ordinary concurrent
        register and forks the lineage, which costs the operator a repair.
        """
        import inspect

        for method in (RecipeRegistry._set_pause, RecipeRegistry.rollback):
            source = inspect.getsource(method)
            assert "write_lock()" in source, f"{method.__name__} appends without the registry write lock"


class TestWriteLockThreadSafety:
    def test_two_threads_do_not_both_hold_the_lock(self, tmp_path: Path) -> None:
        """Re-entrancy is per thread, not per instance.

        An instance-wide depth counter lets a second thread see depth>0 while
        the first holds the flock, skip acquisition, and run unserialised.
        """
        import threading
        import time

        reg = _registry(tmp_path / ".sdd")
        overlap: list[bool] = []
        inside = threading.Event()

        def _holder() -> None:
            with reg.write_lock():
                inside.set()
                time.sleep(1.0)

        def _contender() -> None:
            inside.wait(timeout=5)
            start = time.time()
            with reg.write_lock():
                overlap.append(time.time() - start < 0.3)

        threads = [threading.Thread(target=_holder), threading.Thread(target=_contender)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert overlap, "contender never acquired the lock"
        assert not overlap[0], "second thread entered while the first held the lock"
