"""Tests for bernstein.core.vertical_packs (road-020)."""

from __future__ import annotations

import pytest
import yaml
from bernstein.core.vertical_packs import (
    BUILTIN_PACKS,
    QualityGateSpec,
    RoleSpec,
    VerticalPack,
    generate_pack_config,
    get_pack,
    list_packs,
)

# ---------------------------------------------------------------------------
# QualityGateSpec
# ---------------------------------------------------------------------------


class TestQualityGateSpec:
    def test_frozen(self) -> None:
        gate = QualityGateSpec(
            name="pci-dss-scan",
            command="bernstein gate pci-dss-scan",
            description="Scans for PCI-DSS violations.",
            severity="error",
        )
        with pytest.raises(AttributeError):
            gate.name = "other"  # type: ignore[misc]

    def test_fields(self) -> None:
        gate = QualityGateSpec(
            name="phi-detection",
            command="bernstein gate phi-detection",
            description="Detects PHI.",
            severity="warning",
        )
        assert gate.name == "phi-detection"
        assert gate.command == "bernstein gate phi-detection"
        assert gate.description == "Detects PHI."
        assert gate.severity == "warning"


# ---------------------------------------------------------------------------
# RoleSpec
# ---------------------------------------------------------------------------


class TestRoleSpec:
    def test_frozen(self) -> None:
        role = RoleSpec(
            name="pci-auditor",
            model="anthropic/claude-sonnet-4-20250514",
            effort="high",
            description="Audits PCI-DSS.",
        )
        with pytest.raises(AttributeError):
            role.name = "other"  # type: ignore[misc]

    def test_fields(self) -> None:
        role = RoleSpec(
            name="hipaa-auditor",
            model="anthropic/claude-sonnet-4-20250514",
            effort="high",
            description="Audits HIPAA.",
        )
        assert role.name == "hipaa-auditor"
        assert role.model == "anthropic/claude-sonnet-4-20250514"
        assert role.effort == "high"
        assert role.description == "Audits HIPAA."


# ---------------------------------------------------------------------------
# VerticalPack
# ---------------------------------------------------------------------------


class TestVerticalPack:
    def test_frozen(self) -> None:
        pack = VerticalPack(
            pack_id="test",
            display_name="Test Pack",
            description="desc",
            industry="Testing",
        )
        with pytest.raises(AttributeError):
            pack.pack_id = "other"  # type: ignore[misc]

    def test_default_lists(self) -> None:
        pack = VerticalPack(
            pack_id="test",
            display_name="Test Pack",
            description="desc",
            industry="Testing",
        )
        assert pack.roles == []
        assert pack.quality_gates == []
        assert pack.compliance_tags == []


# ---------------------------------------------------------------------------
# BUILTIN_PACKS
# ---------------------------------------------------------------------------


class TestBuiltinPacks:
    def test_all_three_packs_exist(self) -> None:
        assert "fintech" in BUILTIN_PACKS
        assert "healthtech" in BUILTIN_PACKS
        assert "govtech" in BUILTIN_PACKS

    def test_fintech_has_roles_and_gates(self) -> None:
        pack = BUILTIN_PACKS["fintech"]
        assert len(pack.roles) >= 2
        assert len(pack.quality_gates) >= 2
        role_names = {r.name for r in pack.roles}
        assert "pci-auditor" in role_names
        assert "sox-compliance" in role_names
        gate_names = {g.name for g in pack.quality_gates}
        assert "pci-dss-scan" in gate_names
        assert "sox-audit-trail" in gate_names
        assert "PCI-DSS" in pack.compliance_tags
        assert "SOX" in pack.compliance_tags

    def test_healthtech_has_roles_and_gates(self) -> None:
        pack = BUILTIN_PACKS["healthtech"]
        assert len(pack.roles) >= 2
        assert len(pack.quality_gates) >= 2
        role_names = {r.name for r in pack.roles}
        assert "hipaa-auditor" in role_names
        assert "phi-detector" in role_names
        gate_names = {g.name for g in pack.quality_gates}
        assert "phi-detection" in gate_names
        assert "hipaa-audit" in gate_names
        assert "HIPAA" in pack.compliance_tags

    def test_govtech_has_roles_and_gates(self) -> None:
        pack = BUILTIN_PACKS["govtech"]
        assert len(pack.roles) >= 2
        assert len(pack.quality_gates) >= 2
        role_names = {r.name for r in pack.roles}
        assert "fedramp-auditor" in role_names
        assert "stig-reviewer" in role_names
        gate_names = {g.name for g in pack.quality_gates}
        assert "stig-check" in gate_names
        assert "fedramp-controls" in gate_names
        assert "FedRAMP" in pack.compliance_tags

    def test_all_gates_have_valid_severity(self) -> None:
        for pack in BUILTIN_PACKS.values():
            for gate in pack.quality_gates:
                assert gate.severity in ("error", "warning")

    def test_all_roles_have_nonempty_fields(self) -> None:
        for pack in BUILTIN_PACKS.values():
            for role in pack.roles:
                assert role.name
                assert role.model
                assert role.effort
                assert role.description


# ---------------------------------------------------------------------------
# get_pack
# ---------------------------------------------------------------------------


class TestGetPack:
    def test_known_pack(self) -> None:
        pack = get_pack("fintech")
        assert pack is not None
        assert pack.pack_id == "fintech"

    def test_unknown_pack(self) -> None:
        assert get_pack("nonexistent") is None

    def test_all_builtin_packs_retrievable(self) -> None:
        for pack_id in BUILTIN_PACKS:
            assert get_pack(pack_id) is not None


# ---------------------------------------------------------------------------
# list_packs
# ---------------------------------------------------------------------------


class TestListPacks:
    def test_returns_all_pack_ids(self) -> None:
        ids = list_packs()
        assert set(ids) == {"fintech", "govtech", "healthtech"}

    def test_returns_sorted(self) -> None:
        ids = list_packs()
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# generate_pack_config
# ---------------------------------------------------------------------------


class TestGeneratePackConfig:
    def test_produces_valid_yaml(self) -> None:
        pack = get_pack("fintech")
        assert pack is not None
        snippet = generate_pack_config(pack)
        parsed = yaml.safe_load(snippet)
        assert isinstance(parsed, dict)

    def test_contains_roles_section(self) -> None:
        pack = get_pack("healthtech")
        assert pack is not None
        parsed = yaml.safe_load(generate_pack_config(pack))
        assert "roles" in parsed
        assert isinstance(parsed["roles"], list)
        assert len(parsed["roles"]) == len(pack.roles)

    def test_contains_quality_gates_section(self) -> None:
        pack = get_pack("govtech")
        assert pack is not None
        parsed = yaml.safe_load(generate_pack_config(pack))
        assert "quality_gates" in parsed
        assert isinstance(parsed["quality_gates"], list)
        assert len(parsed["quality_gates"]) == len(pack.quality_gates)

    def test_role_fields_match(self) -> None:
        pack = get_pack("fintech")
        assert pack is not None
        parsed = yaml.safe_load(generate_pack_config(pack))
        first_role = parsed["roles"][0]
        assert first_role["name"] == pack.roles[0].name
        assert first_role["model"] == pack.roles[0].model
        assert first_role["effort"] == pack.roles[0].effort
        assert first_role["description"] == pack.roles[0].description

    def test_gate_fields_match(self) -> None:
        pack = get_pack("fintech")
        assert pack is not None
        parsed = yaml.safe_load(generate_pack_config(pack))
        first_gate = parsed["quality_gates"][0]
        assert first_gate["name"] == pack.quality_gates[0].name
        assert first_gate["command"] == pack.quality_gates[0].command
        assert first_gate["severity"] == pack.quality_gates[0].severity

    def test_compliance_tags_present(self) -> None:
        pack = get_pack("fintech")
        assert pack is not None
        parsed = yaml.safe_load(generate_pack_config(pack))
        assert "compliance_tags" in parsed
        assert parsed["compliance_tags"] == pack.compliance_tags

    def test_pack_id_and_industry_present(self) -> None:
        pack = get_pack("govtech")
        assert pack is not None
        parsed = yaml.safe_load(generate_pack_config(pack))
        assert parsed["vertical_pack"] == "govtech"
        assert parsed["industry"] == "Government Technology"
