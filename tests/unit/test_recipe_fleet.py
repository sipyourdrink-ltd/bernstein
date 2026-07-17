"""Declarative fleet manifest plan/apply (#2546, AC7).

- ``plan`` run twice against the same state emits byte-identical plan
  documents with equal ``plan_hash``.
- ``apply`` against mutated state exits non-zero naming the divergent recipe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.workflows.recipe_fleet import (
    FleetDriftError,
    ManifestEntry,
    apply_fleet,
    plan_fleet,
)
from bernstein.core.workflows.recipe_registry import RecipePins, RecipeRegistry
from bernstein.core.workflows.recipe_spec import RecipeSpec, load_recipe_spec_from_text

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32


def _spec(name: str, description: str) -> RecipeSpec:
    return load_recipe_spec_from_text(
        f"""
name: {name}
description: {description}
version: "1.0.0"
nodes:
  - id: n
    command: "echo hi"
""",
    )


def _registry(sdd: Path) -> RecipeRegistry:
    sdd.mkdir(parents=True, exist_ok=True)
    return RecipeRegistry(sdd, chain=AuditChainStore(sdd / "audit", key=_KEY), hmac_key=_KEY, lineage_key=_KEY)


def _manifest() -> list[ManifestEntry]:
    return [
        ManifestEntry(spec=_spec("triage", "Nightly triage"), pins=RecipePins(git_commit="c1")),
        ManifestEntry(spec=_spec("digest", "Daily digest"), pins=RecipePins(git_commit="c1")),
    ]


class TestPlanReproducibility:
    def test_plan_is_byte_reproducible(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        first = plan_fleet(reg, _manifest())
        second = plan_fleet(reg, _manifest())
        assert first.plan_bytes == second.plan_bytes
        assert first.plan_hash == second.plan_hash

    def test_fresh_plan_lists_registrations(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        plan = plan_fleet(reg, _manifest())
        assert set(plan.to_register) == {"triage", "digest"}
        assert plan.to_supersede == ()

    def test_plan_hash_changes_after_target_change(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        base = plan_fleet(reg, _manifest())
        changed = _manifest()
        changed[0] = ManifestEntry(spec=_spec("triage", "Nightly triage v2"), pins=RecipePins(git_commit="c1"))
        assert plan_fleet(reg, changed).plan_hash != base.plan_hash


class TestApply:
    def test_apply_registers_the_fleet(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        manifest = _manifest()
        plan = plan_fleet(reg, manifest)
        applied = apply_fleet(reg, manifest, plan_hash=plan.plan_hash)
        assert set(applied) == {"triage", "digest"}
        assert reg.live_hash("triage") is not None
        assert reg.live_hash("digest") is not None

    def test_replan_after_apply_shows_no_drift(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        manifest = _manifest()
        plan = plan_fleet(reg, manifest)
        apply_fleet(reg, manifest, plan_hash=plan.plan_hash)
        replan = plan_fleet(reg, manifest)
        assert set(replan.unchanged) == {"triage", "digest"}
        assert replan.to_register == ()

    def test_apply_against_mutated_state_refuses(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        manifest = _manifest()
        plan = plan_fleet(reg, manifest)
        # Mutate the registry out of band after the plan was approved.
        reg.register(spec=_spec("triage", "Out of band change"), pins=RecipePins(git_commit="c1"))
        with pytest.raises(FleetDriftError) as excinfo:
            apply_fleet(reg, manifest, plan_hash=plan.plan_hash)
        assert "triage" in str(excinfo.value)

    def test_apply_binds_receipt_to_plan_hash(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        reg = _registry(sdd)
        manifest = _manifest()
        plan = plan_fleet(reg, manifest)
        apply_fleet(reg, manifest, plan_hash=plan.plan_hash)
        chain = AuditChainStore(sdd / "audit", key=_KEY)
        applies = chain.query(event_type="recipe.fleet_apply")
        assert len(applies) == 1
        assert applies[0].details["plan_hash"] == plan.plan_hash
