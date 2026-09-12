"""
Tests for evidence pack benchmark bundle integration and OSCAL export (Issue #5456).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bernstein.compliance.evidence_pack import (
    build_evidence_pack,
    verify_evidence_pack,
)
from bernstein.compliance.oscal import (
    build_oscal_assessment_results,
    validate_oscal_assessment_results,
)
from bernstein.eval.bench.bundle import SubmissionBundle
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.signer import StubSigner
from bernstein.eval.bench.tool_surface_suite import build_tool_surface_suite

if TYPE_CHECKING:
    from bernstein.eval.bench.suite import BenchSuite


def _signed_bundle(suite: BenchSuite) -> SubmissionBundle:
    runner = BenchRunner(suite=suite, adapter=MockReplayAdapter(), scheduler_config={})
    return StubSigner().sign(runner.run())


@pytest.fixture()
def sample_sdd_with_bundle(tmp_path: Path) -> tuple[Path, SubmissionBundle]:
    sdd = tmp_path / ".sdd"
    audit_dir = sdd / "audit"
    lineage_dir = sdd / "lineage"
    metrics_dir = sdd / "metrics"
    bundles_dir = sdd / "bench" / "bundles"
    audit_dir.mkdir(parents=True, exist_ok=True)
    lineage_dir.mkdir(parents=True, exist_ok=True)
    metrics_dir.mkdir(parents=True, exist_ok=True)
    bundles_dir.mkdir(parents=True, exist_ok=True)

    # Write minimal audit event
    (audit_dir / "events.jsonl").write_text(
        json.dumps({"timestamp": "2026-01-01T00:00:00Z", "event_type": "tool_call", "hmac": "abc"}) + "\n",
        encoding="utf-8",
    )

    # Build and sign a bench bundle
    bundle = _signed_bundle(build_golden_suite_v1())

    bundle_file = bundles_dir / f"{bundle.bundle_hash()}.json"
    bundle.save(bundle_file)

    return sdd, bundle


class TestEvidencePackBenchBundles:
    def test_pack_includes_signed_bench_bundles_keyed_by_control(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]
    ) -> None:
        sdd, bundle = sample_sdd_with_bundle
        zip_path = sdd / "evidence.zip"
        b_hash = bundle.bundle_hash()

        build_evidence_pack(
            sdd_dir=sdd,
            standard="ai-act",
            output_path=zip_path,
        )

        assert zip_path.exists()
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            bundle_rel = f"bench-bundles/{b_hash}.json"
            assert bundle_rel in names

            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            assert bundle_rel in manifest["artefacts"]

            controls_data = json.loads(zf.read("controls.json").decode("utf-8"))
            assert "bench_assessment" in controls_data

            # Check that suite controls (e.g. CTL-ROB-01) are mapped to the bundle
            assessment = controls_data["bench_assessment"]
            assert "CTL-ROB-01" in assessment
            ctl_entry = assessment["CTL-ROB-01"]
            assert ctl_entry["status"] == "measured"
            assert ctl_entry["bundle_hash"] == b_hash
            assert ctl_entry["score"] == bundle.overall_score

    def test_per_control_status_reporting(self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]) -> None:
        sdd, _ = sample_sdd_with_bundle
        zip_path = sdd / "evidence.zip"

        build_evidence_pack(
            sdd_dir=sdd,
            standard="ai-act",
            output_path=zip_path,
        )

        with zipfile.ZipFile(zip_path, "r") as zf:
            controls_data = json.loads(zf.read("controls.json").decode("utf-8"))
            assessment = controls_data["bench_assessment"]

            # Measured control from golden-v1
            assert assessment["CTL-ROB-01"]["status"] == "measured"
            assert "measured by suite golden-v1" in assessment["CTL-ROB-01"]["reason"]

            # Declared in registry but unmeasured in this run
            assert assessment["CTL-SEC-03"]["status"] in ("declared_not_measured", "unmeasured")
            assert len(assessment["CTL-SEC-03"]["reason"]) > 0

    def test_tampered_bundle_fails_pack_verification(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]
    ) -> None:
        sdd, _ = sample_sdd_with_bundle
        zip_path = sdd / "evidence.zip"

        build_evidence_pack(
            sdd_dir=sdd,
            standard="ai-act",
            output_path=zip_path,
        )

        # Verification passes on honest pack
        assert verify_evidence_pack(zip_path) is True

        # Tamper with the bundle inside the zip
        tampered_zip = sdd / "tampered.zip"
        with zipfile.ZipFile(zip_path, "r") as src, zipfile.ZipFile(tampered_zip, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if "bench-bundles/" in item.filename:
                    bundle_dict = json.loads(data.decode("utf-8"))
                    bundle_dict["task_results"][0]["score"] = 0.9999
                    data = json.dumps(bundle_dict).encode("utf-8")
                dst.writestr(item, data)

        assert verify_evidence_pack(tampered_zip) is False

    def test_embedded_bundle_is_the_signed_file_verbatim_and_reloads(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]
    ) -> None:
        """Regression: re-serialising through the pack's JCS canonicaliser
        rewrote ``1.0`` as ``1`` and the embedded copy failed
        ``SubmissionBundle.from_dict`` -- the check ``bench verify`` starts with."""
        sdd, bundle = sample_sdd_with_bundle
        b_hash = bundle.bundle_hash()
        on_disk = (sdd / "bench" / "bundles" / f"{b_hash}.json").read_bytes()
        zip_path = sdd / "evidence.zip"
        build_evidence_pack(sdd_dir=sdd, standard="ai-act", output_path=zip_path)

        with zipfile.ZipFile(zip_path, "r") as zf:
            embedded = zf.read(f"bench-bundles/{b_hash}.json")
        assert embedded == on_disk
        assert SubmissionBundle.from_dict(json.loads(embedded.decode("utf-8"))).bundle_hash() == b_hash

    def test_unreadable_bundle_is_recorded_not_dropped(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]
    ) -> None:
        sdd, bundle = sample_sdd_with_bundle
        bundles_dir = sdd / "bench" / "bundles"
        good_hash = bundle.bundle_hash()
        # A bundle whose stored hash no longer matches its content.
        tampered = json.loads((bundles_dir / f"{good_hash}.json").read_text(encoding="utf-8"))
        tampered["task_results"][0]["score"] = 0.5
        (bundles_dir / "tampered.json").write_text(json.dumps(tampered), encoding="utf-8")
        (bundles_dir / "garbage.json").write_text("{not json", encoding="utf-8")

        zip_path = sdd / "evidence.zip"
        build_evidence_pack(sdd_dir=sdd, standard="ai-act", output_path=zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            controls = json.loads(zf.read("controls.json").decode("utf-8"))
            embedded = [n for n in zf.namelist() if n.startswith("bench-bundles/")]

        assert embedded == [f"bench-bundles/{good_hash}.json"]
        unreadable = {e["path"]: e["reason"] for e in controls["bench_bundles_unreadable"]}
        assert set(unreadable) == {"tampered.json", "garbage.json"}
        assert unreadable["tampered.json"].startswith("ValueError: Bundle hash mismatch")
        assert unreadable["garbage.json"].startswith("JSONDecodeError")

    def test_controls_resolve_through_the_bundles_suite_not_only_golden(self, tmp_path: Path) -> None:
        """A tool-surface-v1 bundle measures the controls tool-surface-v1
        declares. Bundles carry no ``controls`` field, so a lookup on the bundle
        finds nothing and only a hardcoded golden-v1 list ever mapped."""
        sdd = tmp_path / ".sdd"
        (sdd / "audit").mkdir(parents=True)
        (sdd / "lineage").mkdir()
        (sdd / "metrics").mkdir()
        bundles_dir = sdd / "bench" / "bundles"
        bundles_dir.mkdir(parents=True)
        bundle = _signed_bundle(build_tool_surface_suite())
        bundle.save(bundles_dir / f"{bundle.bundle_hash()}.json")
        declared = build_tool_surface_suite().controls
        assert declared, "fixture suite must declare controls for this test to mean anything"

        zip_path = sdd / "evidence.zip"
        build_evidence_pack(sdd_dir=sdd, standard="ai-act", output_path=zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            assessment = json.loads(zf.read("controls.json").decode("utf-8"))["bench_assessment"]
        for cid in declared:
            assert assessment[cid]["status"] == "measured", cid
            assert assessment[cid]["suite_version"] == "tool-surface-v1"
        assert "_unresolvable_suites" not in assessment

        doc = build_oscal_assessment_results(standard="ai-act", bundles=[bundle])
        by_target = {f["target"]["target-id"]: f for f in doc["assessment-results"]["results"][0]["findings"]}
        for cid in declared:
            assert by_target[cid]["target"]["status"]["state"] in ("satisfied", "not-satisfied"), cid
            assert by_target[cid]["related-observations"], cid
        assert "remarks" not in doc["assessment-results"]["results"][0]

    def test_bundle_from_unknown_suite_is_named_not_silently_uncovered(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]
    ) -> None:
        _, bundle = sample_sdd_with_bundle
        raw = bundle.to_dict()
        raw["suite_version"] = "vendor-suite-v9"
        # Re-derive the hash so the bundle is internally consistent.
        unknown = SubmissionBundle(
            suite_hash=raw["suite_hash"],
            suite_version=raw["suite_version"],
            task_results=bundle.task_results,
            scheduler_config=raw["scheduler_config"],
            submitted_at=raw["submitted_at"],
        )
        doc = build_oscal_assessment_results(standard="ai-act", bundles=[unknown])
        result = doc["assessment-results"]["results"][0]
        assert "vendor-suite-v9" in result["remarks"]
        # Nothing was mapped, so every finding is the unmeasured shape.
        assert all(not f.get("related-observations") for f in result["findings"])
        assert validate_oscal_assessment_results(doc) is True


class TestOSCALExport:
    def test_oscal_assessment_results_structure_and_validation(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]
    ) -> None:
        _, bundle = sample_sdd_with_bundle
        oscal_doc = build_oscal_assessment_results(
            standard="ai-act",
            bundles=[bundle],
        )

        assert "assessment-results" in oscal_doc
        results_obj = oscal_doc["assessment-results"]
        assert "metadata" in results_obj
        assert "results" in results_obj
        assert results_obj["metadata"]["oscal-version"] == "1.1.0"

        # Validate with internal schema validator
        assert validate_oscal_assessment_results(oscal_doc) is True

        # Check findings contain control IDs and status
        findings = results_obj["results"][0]["findings"]
        finding_targets = [f["target"]["target-id"] for f in findings]
        assert "CTL-ROB-01" in finding_targets

    def test_findings_carry_the_clause_of_the_requested_standard(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]
    ) -> None:
        from bernstein.compliance.controls import get_default_registry

        _, bundle = sample_sdd_with_bundle
        registry = get_default_registry()
        for standard, key in (("ai-act", "eu_ai_act"), ("iso-42001", "iso_42001"), ("owasp-asi", "owasp_asi")):
            doc = build_oscal_assessment_results(standard=standard, bundles=[bundle])
            findings = doc["assessment-results"]["results"][0]["findings"]
            assert len(findings) == len(registry.list_controls())
            for f in findings:
                control = registry.get(f["target"]["target-id"])
                assert control is not None
                (clause,) = [p["value"] for p in f["props"] if p["name"] == "clause"]
                assert clause == control.references.get(key, "unmapped"), (standard, control.control_id)

    def test_unsupported_standard_is_refused_not_labelled(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]
    ) -> None:
        _, bundle = sample_sdd_with_bundle
        with pytest.raises(ValueError, match="unsupported standard"):
            build_oscal_assessment_results(standard="soc2", bundles=[bundle])

    def test_latest_bundle_by_submitted_at_is_reported_regardless_of_order(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]
    ) -> None:
        _, bundle = sample_sdd_with_bundle
        older = SubmissionBundle(
            suite_hash=bundle.suite_hash,
            suite_version=bundle.suite_version,
            task_results=bundle.task_results,
            scheduler_config=bundle.scheduler_config,
            submitted_at=bundle.submitted_at - 3600,
        )
        for order in ([older, bundle], [bundle, older]):
            doc = build_oscal_assessment_results(standard="ai-act", bundles=order)
            result = doc["assessment-results"]["results"][0]
            (obs,) = [o for o in result["observations"] if "CTL-ROB-01" in o["title"]]
            assert bundle.bundle_hash()[:12] in obs["description"]
            assert obs["collected"].startswith("20")  # the bundle's own time, not the 1970 sentinel

    def test_oscal_export_is_deterministic(self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]) -> None:
        _, bundle = sample_sdd_with_bundle
        doc1 = build_oscal_assessment_results(standard="ai-act", bundles=[bundle])
        doc2 = build_oscal_assessment_results(standard="ai-act", bundles=[bundle])

        json1 = json.dumps(doc1, sort_keys=True, indent=2)
        json2 = json.dumps(doc2, sort_keys=True, indent=2)
        assert json1 == json2


class TestOSCALCLI:
    def test_compliance_oscal_stdout(self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]) -> None:
        from click.testing import CliRunner

        from bernstein.cli.commands.compliance_cmd import compliance_group

        sdd, _ = sample_sdd_with_bundle
        workdir = sdd.parent
        runner = CliRunner()
        result = runner.invoke(compliance_group, ["oscal", "--workdir", str(workdir), "--standard", "ai-act"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "assessment-results" in data

    def test_oscal_is_reachable_through_the_installed_entry_point(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]
    ) -> None:
        """Invoke ``bernstein compliance oscal`` the way a user does, not the
        group object directly, so a registration problem cannot hide."""
        from click.testing import CliRunner

        from bernstein.cli.main import cli

        sdd, _ = sample_sdd_with_bundle
        result = CliRunner().invoke(cli, ["compliance", "oscal", "--workdir", str(sdd.parent), "--standard", "ai-act"])
        assert result.exit_code == 0, result.output
        assert validate_oscal_assessment_results(json.loads(result.output)) is True

    def test_oscal_refuses_to_export_over_a_bundle_it_could_not_read(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle]
    ) -> None:
        from click.testing import CliRunner

        from bernstein.cli.commands.compliance_cmd import compliance_group

        sdd, _ = sample_sdd_with_bundle
        (sdd / "bench" / "bundles" / "broken.json").write_text("{", encoding="utf-8")
        result = CliRunner().invoke(compliance_group, ["oscal", "--workdir", str(sdd.parent), "--standard", "ai-act"])
        assert result.exit_code != 0
        assert "broken.json" in result.output
        assert "refusing to export" in result.output

    def test_compliance_oscal_file_output(
        self, sample_sdd_with_bundle: tuple[Path, SubmissionBundle], tmp_path: Path
    ) -> None:
        from click.testing import CliRunner

        from bernstein.cli.commands.compliance_cmd import compliance_group

        sdd, _ = sample_sdd_with_bundle
        workdir = sdd.parent
        out_file = tmp_path / "oscal.json"
        runner = CliRunner()
        result = runner.invoke(
            compliance_group, ["oscal", "--workdir", str(workdir), "--standard", "ai-act", "--out", str(out_file)]
        )
        assert result.exit_code == 0
        assert out_file.exists()
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert "assessment-results" in data
