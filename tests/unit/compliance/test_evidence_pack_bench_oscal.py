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
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.signer import StubSigner

if TYPE_CHECKING:
    from bernstein.eval.bench.bundle import SubmissionBundle


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
    suite = build_golden_suite_v1()
    adapter = MockReplayAdapter()
    runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config={})
    raw_bundle = runner.run()
    bundle = StubSigner().sign(raw_bundle)

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
