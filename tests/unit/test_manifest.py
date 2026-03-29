"""Tests for the Agent Run Manifest — hashable workflow spec.

Covers:
- RunManifest dataclass creation and field defaults
- Canonical JSON serialization (deterministic, sorted, compact)
- SHA-256 hash computation and stability
- Manifest round-trip: to_dict -> from_dict preserves all fields
- Hash verification: recomputed hash matches stored hash
- build_manifest() factory with OrchestratorConfig
- save_manifest() / load_manifest() file persistence
- list_manifests() directory listing
- diff_manifests() comparison
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.manifest import (
    AgentAdapterConfig,
    ApprovalGateConfig,
    ModelRoutingPolicy,
    Provenance,
    RunManifest,
    build_manifest,
    diff_manifests,
    list_manifests,
    load_manifest,
    save_manifest,
)
from bernstein.core.models import OrchestratorConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(**overrides: object) -> RunManifest:
    defaults: dict = {
        "run_id": "20240315-143022",
        "workflow_definition_hash": "abc123",
        "workflow_name": "governed-default",
        "model_routing": ModelRoutingPolicy(default_model="sonnet"),
        "budget_ceiling_usd": 10.0,
        "approval_gates": ApprovalGateConfig(mode="review", plan_mode=True),
        "agent_adapter": AgentAdapterConfig(cli="claude", model="sonnet", max_agents=4, max_tasks_per_agent=2),
        "provenance": Provenance(
            triggered_by="testuser",
            triggered_at_iso="2024-03-15T14:30:22+00:00",
            commit_sha="deadbeef1234567890",
        ),
        "orchestrator_config": {"max_agents": 4, "budget_usd": 10.0},
    }
    defaults.update(overrides)
    m = RunManifest(**defaults)
    h = m.compute_hash()
    object.__setattr__(m, "manifest_hash", h)
    return m


# ---------------------------------------------------------------------------
# RunManifest basics
# ---------------------------------------------------------------------------


class TestRunManifest:
    def test_defaults(self) -> None:
        m = RunManifest(run_id="20240101-000000")
        assert m.run_id == "20240101-000000"
        assert m.workflow_definition_hash == ""
        assert m.workflow_name == ""
        assert m.budget_ceiling_usd == 0.0
        assert m.manifest_hash == ""

    def test_frozen(self) -> None:
        m = RunManifest(run_id="20240101-000000")
        with pytest.raises(AttributeError):
            m.run_id = "other"  # type: ignore[misc]

    def test_canonical_json_deterministic(self) -> None:
        m = _make_manifest()
        j1 = m.canonical_json()
        j2 = m.canonical_json()
        assert j1 == j2
        # Must be compact (no spaces)
        assert " " not in j1

    def test_canonical_json_sorted_keys(self) -> None:
        m = _make_manifest()
        parsed = json.loads(m.canonical_json())
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_canonical_json_excludes_manifest_hash(self) -> None:
        m = _make_manifest()
        parsed = json.loads(m.canonical_json())
        assert "manifest_hash" not in parsed

    def test_hash_is_64_hex(self) -> None:
        m = _make_manifest()
        assert len(m.manifest_hash) == 64
        assert all(c in "0123456789abcdef" for c in m.manifest_hash)

    def test_hash_stable_across_calls(self) -> None:
        m = _make_manifest()
        h1 = m.compute_hash()
        h2 = m.compute_hash()
        assert h1 == h2

    def test_hash_changes_on_field_change(self) -> None:
        m1 = _make_manifest(budget_ceiling_usd=10.0)
        m2 = _make_manifest(budget_ceiling_usd=20.0)
        assert m1.manifest_hash != m2.manifest_hash

    def test_hash_changes_on_workflow(self) -> None:
        m1 = _make_manifest(workflow_name="alpha")
        m2 = _make_manifest(workflow_name="beta")
        assert m1.manifest_hash != m2.manifest_hash


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_to_dict_includes_hash(self) -> None:
        m = _make_manifest()
        d = m.to_dict()
        assert "manifest_hash" in d
        assert d["manifest_hash"] == m.manifest_hash

    def test_round_trip(self) -> None:
        m = _make_manifest()
        d = m.to_dict()
        m2 = RunManifest.from_dict(d)
        assert m2.run_id == m.run_id
        assert m2.manifest_hash == m.manifest_hash
        assert m2.workflow_name == m.workflow_name
        assert m2.budget_ceiling_usd == m.budget_ceiling_usd
        assert m2.provenance.triggered_by == m.provenance.triggered_by
        assert m2.provenance.commit_sha == m.provenance.commit_sha
        assert m2.agent_adapter.cli == m.agent_adapter.cli
        assert m2.agent_adapter.model == m.agent_adapter.model
        assert m2.approval_gates.mode == m.approval_gates.mode

    def test_round_trip_preserves_hash(self) -> None:
        m = _make_manifest()
        m2 = RunManifest.from_dict(m.to_dict())
        assert m2.compute_hash() == m.manifest_hash

    def test_from_dict_handles_missing_fields(self) -> None:
        minimal = {"run_id": "20240101-000000"}
        m = RunManifest.from_dict(minimal)
        assert m.run_id == "20240101-000000"
        assert m.workflow_definition_hash == ""
        assert m.manifest_hash == ""


# ---------------------------------------------------------------------------
# build_manifest factory
# ---------------------------------------------------------------------------


class TestBuildManifest:
    def test_builds_with_config(self) -> None:
        config = OrchestratorConfig(
            max_agents=4,
            budget_usd=15.0,
            approval="review",
            plan_mode=True,
        )
        m = build_manifest(
            run_id="20240315-143022",
            config=config,
            cli="claude",
            model="sonnet",
        )
        assert m.run_id == "20240315-143022"
        assert m.agent_adapter.cli == "claude"
        assert m.agent_adapter.model == "sonnet"
        assert m.agent_adapter.max_agents == 4
        assert m.budget_ceiling_usd == 15.0
        assert m.approval_gates.mode == "review"
        assert m.approval_gates.plan_mode is True
        assert m.manifest_hash != ""

    def test_hash_is_valid(self) -> None:
        config = OrchestratorConfig()
        m = build_manifest(run_id="20240101-000000", config=config)
        assert m.compute_hash() == m.manifest_hash

    def test_provenance_populated(self) -> None:
        config = OrchestratorConfig()
        m = build_manifest(run_id="20240101-000000", config=config)
        assert m.provenance.triggered_by != ""
        assert m.provenance.triggered_at_iso != ""

    def test_workflow_fields(self) -> None:
        config = OrchestratorConfig(workflow="governed")
        m = build_manifest(
            run_id="20240101-000000",
            config=config,
            workflow_name="governed-default",
            workflow_definition_hash="abc123def456",
        )
        assert m.workflow_name == "governed-default"
        assert m.workflow_definition_hash == "abc123def456"

    def test_orchestrator_config_snapshot(self) -> None:
        config = OrchestratorConfig(
            max_agents=8,
            evolution_enabled=False,
            merge_strategy="direct",
        )
        m = build_manifest(run_id="20240101-000000", config=config)
        assert m.orchestrator_config["max_agents"] == 8
        assert m.orchestrator_config["evolution_enabled"] is False
        assert m.orchestrator_config["merge_strategy"] == "direct"


# ---------------------------------------------------------------------------
# File persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_and_load(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        m = _make_manifest()
        path = save_manifest(m, sdd)
        assert Path(path).exists()

        loaded = load_manifest(sdd, m.run_id)
        assert loaded is not None
        assert loaded.run_id == m.run_id
        assert loaded.manifest_hash == m.manifest_hash

    def test_load_nonexistent_returns_none(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        assert load_manifest(sdd, "nonexistent") is None

    def test_list_manifests_empty(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        assert list_manifests(sdd) == []

    def test_list_manifests_returns_sorted(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        m1 = _make_manifest(run_id="20240101-000000")
        m2 = _make_manifest(run_id="20240315-143022")
        save_manifest(m2, sdd)
        save_manifest(m1, sdd)
        result = list_manifests(sdd)
        assert result == ["20240101-000000", "20240315-143022"]

    def test_saved_file_is_valid_json(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        sdd.mkdir()
        m = _make_manifest()
        path = save_manifest(m, sdd)
        data = json.loads(Path(path).read_text())
        assert data["run_id"] == m.run_id
        assert data["manifest_hash"] == m.manifest_hash


# ---------------------------------------------------------------------------
# diff_manifests
# ---------------------------------------------------------------------------


class TestDiffManifests:
    def test_identical_manifests(self) -> None:
        m = _make_manifest()
        assert diff_manifests(m, m) == {}

    def test_budget_change(self) -> None:
        m1 = _make_manifest(budget_ceiling_usd=10.0)
        m2 = _make_manifest(budget_ceiling_usd=20.0)
        diffs = diff_manifests(m1, m2)
        assert "budget_ceiling_usd" in diffs
        assert diffs["budget_ceiling_usd"] == (10.0, 20.0)

    def test_multiple_changes(self) -> None:
        m1 = _make_manifest(
            budget_ceiling_usd=10.0,
            workflow_name="alpha",
        )
        m2 = _make_manifest(
            budget_ceiling_usd=20.0,
            workflow_name="beta",
        )
        diffs = diff_manifests(m1, m2)
        assert "budget_ceiling_usd" in diffs
        assert "workflow_name" in diffs

    def test_run_id_change(self) -> None:
        m1 = _make_manifest(run_id="20240101-000000")
        m2 = _make_manifest(run_id="20240315-143022")
        diffs = diff_manifests(m1, m2)
        assert "run_id" in diffs
