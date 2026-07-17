"""Registered-recipe registry: content-addressing, lineage, pause (#2546).

Covers the acceptance criteria that live at the registry layer:

- Registration determinism (AC1): the same manifest at the same commit
  derives byte-identical canonical bytes and the same recipe_hash; changing
  any pinned input changes the hash.
- Offline verifiability (AC5): ``verify_history`` walks the lifecycle
  receipts against the HMAC chain with no server and fails on a broken /
  reordered link.
- Pause is stateful, not destructive (AC6): a paused recipe fires nothing,
  keeps its identity, and its pause window reconstructs from receipts alone.
- No behaviour change for zero-block manifests (AC8).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.workflows.recipe_registry import (
    RecipePins,
    RecipeRegistry,
    RecipeRegistryError,
    compute_recipe_registration,
    recipe_content_hash,
)
from bernstein.core.workflows.recipe_spec import RecipeSpec, load_recipe_spec_from_text

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"0" * 32

_MANIFEST = """
name: nightly-triage
description: Nightly triage recipe
version: "1.2.0"
params:
  - name: severity
    type: string
    default: high
    help: "Severity to triage."
nodes:
  - id: triage
    agent: backend
    prompt: "Triage {severity} issues. Goal: {goal}"
schedules:
  - kind: cron
    recurrence: "0 9 * * *"
    timezone: America/New_York
    dst_policy: post_transition
"""


def _spec(manifest: str = _MANIFEST) -> RecipeSpec:
    return load_recipe_spec_from_text(manifest)


def _registry(sdd: Path) -> RecipeRegistry:
    sdd.mkdir(parents=True, exist_ok=True)
    return RecipeRegistry(
        sdd,
        chain=AuditChainStore(sdd / "audit", key=_KEY),
        hmac_key=_KEY,
        lineage_key=_KEY,
    )


# ---------------------------------------------------------------------------
# AC1: registration determinism
# ---------------------------------------------------------------------------


class TestRegistrationDeterminism:
    def test_same_manifest_same_commit_same_hash(self) -> None:
        pins = RecipePins(git_commit="abc123", adapter="claude", model="opus", prompt_pack_sha256="deadbeef")
        b1, h1, *_ = compute_recipe_registration(_spec(), pins=pins)
        b2, h2, *_ = compute_recipe_registration(_spec(), pins=pins)
        assert b1 == b2
        assert h1 == h2
        assert recipe_content_hash(b1) == h1

    @pytest.mark.parametrize(
        "changed",
        [
            RecipePins(git_commit="OTHER", adapter="claude", model="opus", prompt_pack_sha256="deadbeef"),
            RecipePins(git_commit="abc123", adapter="codex", model="opus", prompt_pack_sha256="deadbeef"),
            RecipePins(git_commit="abc123", adapter="claude", model="sonnet", prompt_pack_sha256="deadbeef"),
            RecipePins(git_commit="abc123", adapter="claude", model="opus", prompt_pack_sha256="feedface"),
        ],
    )
    def test_changing_any_pin_changes_hash(self, changed: RecipePins) -> None:
        base = RecipePins(git_commit="abc123", adapter="claude", model="opus", prompt_pack_sha256="deadbeef")
        _, base_hash, *_ = compute_recipe_registration(_spec(), pins=base)
        _, changed_hash, *_ = compute_recipe_registration(_spec(), pins=changed)
        assert base_hash != changed_hash

    def test_changing_param_schema_changes_hash(self) -> None:
        pins = RecipePins(git_commit="abc123")
        _, h1, *_ = compute_recipe_registration(_spec(), pins=pins)
        altered = _MANIFEST.replace("default: high", "default: low")
        _, h2, *_ = compute_recipe_registration(_spec(altered), pins=pins)
        assert h1 != h2

    def test_recipe_hash_is_identity(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        rr = reg.register(spec=_spec(), pins=RecipePins(git_commit="abc123"))
        assert reg.live_hash("nightly-triage") == rr.recipe_hash
        assert rr.recipe_id == f"recipe_{rr.recipe_hash[:12]}"
        assert reg.get_canonical_bytes(rr.recipe_hash) == rr.canonical_bytes


# ---------------------------------------------------------------------------
# Registration seals canonical bytes into the lineage spine
# ---------------------------------------------------------------------------


class TestRegistrationSeal:
    def test_register_seals_into_spine(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        rr = reg.register(spec=_spec(), pins=RecipePins(git_commit="abc123"))
        assert rr.spine_anchor.startswith("sha256:")

    def test_register_is_idempotent(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        pins = RecipePins(git_commit="abc123")
        first = reg.register(spec=_spec(), pins=pins)
        second = reg.register(spec=_spec(), pins=pins)
        assert first.recipe_hash == second.recipe_hash
        # Idempotent re-register writes no extra receipt.
        assert len(reg.history("nightly-triage")) == 1


# ---------------------------------------------------------------------------
# AC5: definition lineage + offline verifiability
# ---------------------------------------------------------------------------


class TestDefinitionLineage:
    def test_supersede_then_rollback_lineage(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        first = reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        changed = _MANIFEST.replace("Nightly triage recipe", "Nightly triage recipe v2")
        second = reg.register(spec=_spec(changed), pins=RecipePins(git_commit="c1"))
        assert reg.live_hash("nightly-triage") == second.recipe_hash
        assert second.superseded_hash == first.recipe_hash

        reg.rollback("nightly-triage", first.recipe_hash)
        assert reg.live_hash("nightly-triage") == first.recipe_hash
        # Nothing deleted: the superseding hash is still a known definition.
        assert reg.get_canonical_bytes(second.recipe_hash) is not None
        assert len(reg.history("nightly-triage")) == 3

    def test_verify_history_passes_on_intact_chain(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        changed = _MANIFEST.replace("Nightly triage recipe", "v2")
        reg.register(spec=_spec(changed), pins=RecipePins(git_commit="c1"))
        ok, errors = reg.verify_history("nightly-triage")
        assert ok, errors

    def test_verify_history_detects_byte_tamper(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        reg = _registry(sdd)
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        # Flip a byte in the on-disk audit log.
        audit_file = next((sdd / "audit").glob("*.jsonl"))
        lines = audit_file.read_text().splitlines()
        obj = json.loads(lines[0])
        obj["details"]["name"] = "hacked"
        lines[0] = json.dumps(obj)
        audit_file.write_text("\n".join(lines) + "\n")

        reopened = _registry(sdd)
        ok, errors = reopened.verify_history("nightly-triage")
        assert not ok
        assert errors

    def test_verify_history_detects_reorder(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        reg = _registry(sdd)
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        changed = _MANIFEST.replace("Nightly triage recipe", "v2")
        reg.register(spec=_spec(changed), pins=RecipePins(git_commit="c1"))
        audit_file = next((sdd / "audit").glob("*.jsonl"))
        lines = audit_file.read_text().splitlines()
        assert len(lines) >= 2
        lines[0], lines[1] = lines[1], lines[0]
        audit_file.write_text("\n".join(lines) + "\n")

        reopened = _registry(sdd)
        ok, errors = reopened.verify_history("nightly-triage")
        assert not ok
        assert errors

    def test_rollback_unknown_hash_rejected(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        with pytest.raises(RecipeRegistryError):
            reg.rollback("nightly-triage", "0" * 64)


# ---------------------------------------------------------------------------
# AC6: pause is stateful, not destructive
# ---------------------------------------------------------------------------


class TestPause:
    def test_paused_recipe_fires_nothing_but_keeps_identity(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        rr = reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        reg.pause("nightly-triage")
        assert reg.is_paused("nightly-triage")
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)
        assert result.dispatched is False
        assert result.projection_hash == ""
        # Identity preserved through the pause.
        assert reg.live_hash("nightly-triage") == rr.recipe_hash

    def test_resume_restores_firing(self, tmp_path: Path) -> None:
        reg = _registry(tmp_path / ".sdd")
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        reg.pause("nightly-triage")
        reg.resume("nightly-triage")
        assert reg.is_paused("nightly-triage") is False
        result = reg.fire("nightly-triage", fire_time=1_800_000_000)
        assert result.dispatched is True
        assert result.projection_hash

    def test_pause_window_reconstructs_from_receipts(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        reg = _registry(sdd)
        reg.register(spec=_spec(), pins=RecipePins(git_commit="c1"))
        reg.pause("nightly-triage")
        reg.resume("nightly-triage")
        # A fresh registry (no in-memory state) reconstructs the pause window
        # purely from the receipts on disk.
        reopened = _registry(sdd)
        history = reopened.history("nightly-triage")
        types = [ev["event_type"] for ev in history]
        assert "recipe.pause" in types
        assert "recipe.resume" in types
        assert reopened.is_paused("nightly-triage") is False


# ---------------------------------------------------------------------------
# AC8: zero-block manifests keep working unchanged
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    def test_zero_block_manifest_registers(self, tmp_path: Path) -> None:
        manifest = """
name: plain
description: A plain zero-block recipe
version: "1.0.0"
nodes:
  - id: n
    command: "echo hi"
"""
        spec = load_recipe_spec_from_text(manifest)
        assert spec.schedules == []
        assert spec.triggers == []
        assert spec.sandbox_pool == ""
        reg = _registry(tmp_path / ".sdd")
        rr = reg.register(spec=spec, pins=RecipePins(git_commit="c1"))
        assert reg.live_hash("plain") == rr.recipe_hash
        # A zero-schedule recipe fires with a plain (tz-less) projection.
        result = reg.fire("plain", fire_time=1_800_000_000)
        assert result.dispatched is True
