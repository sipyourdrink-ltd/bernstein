"""
Tests for the central compliance control registry (Issue #5455, piece 1).

The suite control declaration and its enforcement in ``bench`` are the
second piece and carry their own tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.compliance_cmd import compliance_group
from bernstein.compliance.controls import (
    Control,
    get_default_registry,
)


class TestControlRegistry:
    def test_registry_contains_at_least_30_controls(self) -> None:
        registry = get_default_registry()
        controls = registry.list_controls()
        assert len(controls) >= 30

    def test_controls_cover_all_mandated_frameworks(self) -> None:
        registry = get_default_registry()
        controls = registry.list_controls()
        frameworks_present = set()
        for c in controls:
            assert c.control_id.startswith("CTL-")
            assert len(c.title) > 0
            assert len(c.description) > 0
            assert len(c.evidence_kinds) > 0
            for fw in c.references:
                frameworks_present.add(fw)

        expected_frameworks = {
            "eu_ai_act",
            "owasp_asi",
            "owasp_skills",
            "nist_ai_rmf",
            "iso_42001",
            "finos_aigf",
        }
        for fw in expected_frameworks:
            assert fw in frameworks_present, f"Framework {fw} missing from registry"

    def test_control_lookup(self) -> None:
        registry = get_default_registry()
        c = registry.get("CTL-GOV-01")
        assert c is not None
        assert c.control_id == "CTL-GOV-01"
        assert "eu_ai_act" in c.references
        assert "audit_chain" in c.evidence_kinds or "policy" in c.evidence_kinds or len(c.evidence_kinds) > 0

    def test_filter_by_framework(self) -> None:
        registry = get_default_registry()
        eu_controls = registry.list_controls(framework="eu_ai_act")
        assert len(eu_controls) > 0
        for c in eu_controls:
            assert "eu_ai_act" in c.references

    def test_validate_control_ids(self) -> None:
        registry = get_default_registry()
        assert registry.validate_control_ids(["CTL-GOV-01", "CTL-ROB-01"]) == []
        invalid = registry.validate_control_ids(["CTL-GOV-01", "INVALID-99", "NONEXISTENT"])
        assert invalid == ["INVALID-99", "NONEXISTENT"]

    def test_markdown_table_generation(self) -> None:
        registry = get_default_registry()
        md = registry.to_markdown_table()
        assert "| Control ID | Title | Frameworks | Evidence Kinds |" in md
        assert "CTL-GOV-01" in md


class TestComplianceControlsCLI:
    def test_compliance_controls_text(self) -> None:
        """Through the real root ``cli``, not the group object, so a break in
        ``cli.add_command(compliance_group, "compliance")`` is caught here."""
        from bernstein.cli.main import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["compliance", "controls"])
        assert result.exit_code == 0
        assert "CTL-GOV-01" in result.output
        assert "Control ID" in result.output or "Title" in result.output

    def test_compliance_controls_json(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) >= 30
        assert any(c["control_id"] == "CTL-GOV-01" for c in data)

    def test_compliance_controls_framework_filter(self) -> None:
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["controls", "--framework", "eu_ai_act", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) > 0
        for c in data:
            assert "eu_ai_act" in c["references"]


class TestRegistryIsDocumented:
    def test_the_docs_table_is_generated_from_the_registry(self) -> None:
        """``docs/compliance/regulator-mapped-packs.md`` carries the registry table verbatim.

        There is no generator script: this test *is* the drift check, and its
        failure message says how to regenerate.
        """
        doc = (Path(__file__).resolve().parents[3] / "docs" / "compliance" / "regulator-mapped-packs.md").read_text(
            encoding="utf-8"
        )
        start = doc.index("-->", doc.index("<!-- controls-table:start")) + len("-->")
        end = doc.index("<!-- controls-table:end -->")
        in_doc = doc[start:end].strip()

        expected = get_default_registry().to_markdown_table().strip()

        assert in_doc == expected, (
            "regulator-mapped-packs.md controls table has drifted from ControlRegistry. Regenerate with: "
            "get_default_registry().to_markdown_table()"
        )


class TestRegistryExtensionContract:
    """A control is defined once; an extension is visible through the singleton and can be undone."""

    @staticmethod
    def _custom(control_id: str = "CTL-ORG-01"):
        from bernstein.compliance.controls import Control

        return Control(
            control_id=control_id,
            title="Org-specific",
            description="custom",
            references={"iso_42001": "x"},
            evidence_kinds=["policy"],
        )

    def test_registering_an_existing_id_is_refused_not_overwritten(self) -> None:
        from bernstein.compliance.controls import ControlRegistry

        registry = ControlRegistry()
        canonical = registry.get("CTL-SEC-02")
        assert canonical is not None
        with pytest.raises(ValueError, match="already registered"):
            registry.register(self._custom("CTL-SEC-02"))
        assert registry.get("CTL-SEC-02") is canonical

    @pytest.mark.parametrize("bad", ["", " ", " CTL-ORG-01", "CTL-ORG-01 "])
    def test_a_blank_or_padded_id_is_refused(self, bad: str) -> None:
        from bernstein.compliance.controls import ControlRegistry

        with pytest.raises(ValueError, match="non-empty"):
            ControlRegistry().register(self._custom(bad))

    def test_a_custom_control_on_the_default_registry_is_visible_and_can_be_undone(self) -> None:
        from bernstein.compliance.controls import DEFAULT_REGISTRY

        before = len(DEFAULT_REGISTRY.list_controls())
        custom = self._custom()
        DEFAULT_REGISTRY.register(custom)
        try:
            assert DEFAULT_REGISTRY.get("CTL-ORG-01") is custom
            assert custom in DEFAULT_REGISTRY.list_controls(framework="iso_42001")
            assert DEFAULT_REGISTRY.validate_control_ids(["CTL-ORG-01"]) == []
        finally:
            assert DEFAULT_REGISTRY.unregister("CTL-ORG-01") is custom
        assert DEFAULT_REGISTRY.get("CTL-ORG-01") is None
        assert len(DEFAULT_REGISTRY.list_controls()) == before

    def test_unregistering_an_unknown_id_is_an_error(self) -> None:
        from bernstein.compliance.controls import ControlRegistry

        with pytest.raises(ValueError, match="not registered"):
            ControlRegistry().unregister("CTL-NOPE-99")

    def test_an_isolated_registry_holds_only_what_it_was_given(self) -> None:
        from bernstein.compliance.controls import ControlRegistry

        registry = ControlRegistry(controls=[self._custom()])
        assert [c.control_id for c in registry.list_controls()] == ["CTL-ORG-01"]
        assert registry.get("CTL-SEC-02") is None

    def test_a_framework_filter_that_matches_nothing_says_so(self) -> None:
        result = CliRunner().invoke(compliance_group, ["controls", "--framework", "iso_42O01"])
        assert result.exit_code == 0, result.output
        assert "Total: 0 controls" in result.stdout
        assert "No control references framework 'iso_42O01'" in result.stderr
        assert "iso_42001" in result.stderr


class TestRegisteredControlsCannotBeMutated:
    """A registered control is process-wide shared state, so it must be read-only.

    ``@dataclass(frozen=True)`` only stops the attributes being rebound; the
    ``references`` mapping and ``evidence_kinds`` sequence it points at were
    still mutable in place. Because ``ControlRegistry.get`` hands out the very
    object held by ``DEFAULT_REGISTRY``, a caller could rewrite what an
    external framework identifier means for every later reader in the process
    -- a tampering surface in the catalogue whose entire purpose is to be the
    authoritative statement of that meaning.
    """

    def test_references_cannot_be_rewritten_through_a_handed_out_control(self) -> None:
        control = get_default_registry().get("CTL-GOV-01")
        assert control is not None
        with pytest.raises(TypeError):
            control.references["eu_ai_act"] = "TAMPERED"  # type: ignore[index]

    def test_evidence_kinds_cannot_be_appended_to(self) -> None:
        control = get_default_registry().get("CTL-GOV-01")
        assert control is not None
        with pytest.raises(AttributeError):
            control.evidence_kinds.append("forged")  # type: ignore[attr-defined]

    def test_the_callers_own_containers_cannot_reach_in_afterwards(self) -> None:
        """Defensive copy, not just an immutable view over the caller's object."""
        references = {"eu_ai_act": "original"}
        evidence = ["audit_chain"]
        control = Control(
            control_id="CTL-ORG-IMMUTABLE",
            title="t",
            description="d",
            references=references,
            evidence_kinds=evidence,
        )

        references["eu_ai_act"] = "mutated after construction"
        evidence.append("added after construction")

        assert control.references["eu_ai_act"] == "original"
        assert tuple(control.evidence_kinds) == ("audit_chain",)

    def test_to_dict_still_hands_back_plain_mutable_copies(self) -> None:
        """Serialisation and existing consumers are unchanged by the hardening."""
        payload = get_default_registry().get("CTL-GOV-01").to_dict()  # type: ignore[union-attr]
        assert isinstance(payload["references"], dict)
        assert isinstance(payload["evidence_kinds"], list)
        payload["references"]["eu_ai_act"] = "safe to edit a copy"


class TestCrosswalkAgreesWithCanonicalMaps:
    """The registry cites external control ids; it never states what they mean.

    ``owasp_asi.py`` and ``owasp_skills.py`` drive the evidence packs, so they
    are what an auditor reads an ASI/AST id against. When this registry also
    spelled the meaning out by hand the two disagreed -- ``ASI08`` was labelled
    "Human-in-the-Loop Bypass / Failure" here and "Unbounded consumption"
    there -- and nothing failed, because nothing compared them. These tests are
    that comparison.
    """

    def test_every_asi_reference_is_the_canonical_label(self) -> None:
        from bernstein.compliance import owasp_asi

        cited = {
            c.control_id: c.references["owasp_asi"]
            for c in get_default_registry().list_controls()
            if "owasp_asi" in c.references
        }
        assert cited, "the registry should cite at least one ASI control"
        for control_id, label in cited.items():
            asi_id = label.split(" - ", 1)[0]
            assert label == owasp_asi.reference_label(asi_id), control_id

    def test_every_ast_reference_is_the_canonical_label(self) -> None:
        from bernstein.compliance import owasp_skills

        cited = {
            c.control_id: c.references["owasp_skills"]
            for c in get_default_registry().list_controls()
            if "owasp_skills" in c.references
        }
        assert cited, "the registry should cite at least one AST control"
        for control_id, label in cited.items():
            ast_id = label.split(" - ", 1)[0]
            assert label == owasp_skills.reference_label(ast_id), control_id

    def test_every_finos_reference_is_a_published_mitigation(self) -> None:
        from bernstein.compliance import finos_aigf

        cited = {
            c.control_id: c.references["finos_aigf"]
            for c in get_default_registry().list_controls()
            if "finos_aigf" in c.references
        }
        assert cited, "the registry should cite at least one FINOS mitigation"
        for control_id, label in cited.items():
            mitigation_id = label.split(" - ", 1)[0]
            assert mitigation_id in finos_aigf.MITIGATIONS, f"{control_id} cites unpublished {mitigation_id!r}"
            assert label == finos_aigf.reference_label(mitigation_id), control_id

    def test_no_control_uses_the_invented_aigf_vocabulary(self) -> None:
        """FINOS numbers its mitigations ``mi-N``; ``AIGF-GOV-01`` resolves to nothing."""
        for c in get_default_registry().list_controls():
            assert not c.references.get("finos_aigf", "").startswith("AIGF-"), c.control_id

    def test_an_unknown_external_id_cannot_be_cited(self) -> None:
        """The label helpers are the gate: a made-up id raises instead of rendering."""
        from bernstein.compliance import finos_aigf, owasp_asi, owasp_skills

        for module, bad in ((owasp_asi, "ASI99"), (owasp_skills, "AST99"), (finos_aigf, "mi-999")):
            with pytest.raises(KeyError):
                module.reference_label(bad)


class TestRegisterRefusesUnusableControls:
    """A control that registers cleanly and cannot be assessed is worse than none."""

    @staticmethod
    def _control(**overrides: object) -> Control:
        kwargs: dict[str, object] = {
            "control_id": "CTL-ORG-99",
            "title": "Org-specific",
            "description": "custom",
            "references": {"iso_42001": "A.1"},
            "evidence_kinds": ["policy"],
        }
        kwargs.update(overrides)
        return Control(**kwargs)  # type: ignore[arg-type]

    def test_a_blank_title_is_refused(self) -> None:
        from bernstein.compliance.controls import ControlRegistry

        with pytest.raises(ValueError, match="non-empty title"):
            ControlRegistry(controls=[]).register(self._control(title="   "))

    def test_a_blank_description_is_refused(self) -> None:
        from bernstein.compliance.controls import ControlRegistry

        with pytest.raises(ValueError, match="non-empty description"):
            ControlRegistry(controls=[]).register(self._control(description=""))

    def test_a_control_with_no_evidence_kind_is_refused(self) -> None:
        from bernstein.compliance.controls import ControlRegistry

        with pytest.raises(ValueError, match="no evidence kinds"):
            ControlRegistry(controls=[]).register(self._control(evidence_kinds=[]))

    def test_an_unknown_framework_key_is_refused(self) -> None:
        """A typo in a framework key hides the reference instead of reporting it."""
        from bernstein.compliance.controls import ControlRegistry

        with pytest.raises(ValueError, match="iso_42O01"):
            ControlRegistry(controls=[]).register(self._control(references={"iso_42O01": "A.1"}))

    def test_every_standard_control_satisfies_the_same_rules(self) -> None:
        from bernstein.compliance.controls import KNOWN_FRAMEWORKS

        for c in get_default_registry().list_controls():
            assert c.title.strip(), c.control_id
            assert c.description.strip(), c.control_id
            assert c.evidence_kinds, c.control_id
            assert set(c.references) <= KNOWN_FRAMEWORKS, c.control_id


class TestMarkdownHonoursTheFrameworkFilter:
    """``--framework`` meant different things per ``--format``."""

    def test_the_markdown_table_is_filtered_like_json(self) -> None:
        runner = CliRunner()
        markdown = runner.invoke(compliance_group, ["controls", "--format", "markdown", "--framework", "owasp_asi"])
        rendered = runner.invoke(compliance_group, ["controls", "--format", "json", "--framework", "owasp_asi"])
        assert markdown.exit_code == 0, markdown.output
        assert rendered.exit_code == 0, rendered.output

        expected = [c["control_id"] for c in json.loads(rendered.stdout)]
        body = [ln for ln in markdown.stdout.splitlines() if ln.startswith("| CTL-")]
        assert [ln.split("|")[1].strip() for ln in body] == expected

    def test_an_unfiltered_table_still_holds_every_control(self) -> None:
        registry = get_default_registry()
        assert registry.to_markdown_table().count("\n| CTL-") == len(registry.list_controls())
