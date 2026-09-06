"""
Tests for evidence pack signed bench bundles and OSCAL assessment-results export (Issue #5456).

Acceptance criteria:
- AC-1: Manifest lists bundles with hashes; tampered bundle causes verify to fail.
- AC-2: OSCAL output validates against published schema structure.
- AC-3: Same inputs -> byte-identical pack and OSCAL export.
- AC-4: Per-control status (measured, declared-not-measured, not-applicable) with reasons.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from bernstein.compliance.evidence_pack import (
    build_evidence_pack,
    export_oscal_assessment_results,
    validate_oscal_assessment_results,
    verify_evidence_pack,
)
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.signer import StubSigner
from bernstein.eval.bench.suite import BenchSuite, BenchTask

if TYPE_CHECKING:
    from bernstein.eval.bench.bundle import SubmissionBundle


@pytest.fixture()
def mock_sdd(tmp_path: Path) -> Path:
    sdd = tmp_path / ".sdd"
    (sdd / "audit").mkdir(parents=True)
    (sdd / "lineage").mkdir(parents=True)
    (sdd / "metrics").mkdir(parents=True)
    (sdd / "bench").mkdir(parents=True)

    # Sample audit event
    audit_ev = {
        "timestamp": "2026-03-01T12:00:00Z",
        "event_type": "task_start",
        "resource_type": "task",
        "resource_id": "task_1",
        "hmac": "deadbeef1234",
    }
    (sdd / "audit" / "audit_001.jsonl").write_text(json.dumps(audit_ev) + "\n", encoding="utf-8")
    return sdd


@pytest.fixture()
def sample_bench_bundle() -> SubmissionBundle:
    suite = BenchSuite(
        version="audit-v1",
        tasks=[
            BenchTask(
                id="task_audit_log",
                description="Audit logging verification",
                steps=("step 1",),
                assertions=({"kind": "audit_valid"},),
                category="audit",
            )
        ],
        controls=["CTRL-AUDIT-TRAIL", "art-12(1)"],
    )
    adapter = MockReplayAdapter()
    runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config={"scheduler": "default"})
    bundle = runner.run()
    return StubSigner().sign(bundle)


# ===========================================================================
# AC-1: Bench Bundles & Pack Verification
# ===========================================================================


class TestEvidencePackBenchBundles:
    def test_pack_includes_signed_bench_bundles_and_manifest_hashes(
        self, mock_sdd: Path, sample_bench_bundle: SubmissionBundle, tmp_path: Path
    ) -> None:
        """AC-1: Pack contains bench/*.json, manifest lists bundles with hashes."""
        bundle_path = mock_sdd / "bench" / "audit_bundle.json"
        sample_bench_bundle.save(bundle_path)

        out_zip = tmp_path / "evidence.zip"
        build_evidence_pack(
            sdd_dir=mock_sdd,
            standard="ai-act",
            output_path=out_zip,
            bench_bundles=[sample_bench_bundle],
        )

        assert out_zip.exists()
        with zipfile.ZipFile(out_zip) as zf:
            namelist = zf.namelist()
            assert any(name.startswith("bench/") and name.endswith(".json") for name in namelist)
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            controls_doc = json.loads(zf.read("controls.json").decode("utf-8"))

        assert "bundles" in manifest
        assert len(manifest["bundles"]) >= 1
        bundle_info = manifest["bundles"][0]
        assert bundle_info["bundle_hash"] == sample_bench_bundle.bundle_hash()
        assert bundle_info["suite_version"] == "audit-v1"

        # Verify controls.json has status and reasons
        ctrl_map = {c["control_id"]: c for c in controls_doc["controls"]}
        assert "art-12(1)" in ctrl_map
        art12 = ctrl_map["art-12(1)"]
        assert art12["status"] == "measured"
        assert "audit-v1" in art12["reason"]

    def test_verify_evidence_pack_passes_on_valid_pack(
        self, mock_sdd: Path, sample_bench_bundle: SubmissionBundle, tmp_path: Path
    ) -> None:
        """AC-1: verify_evidence_pack returns passed=True on intact pack with signed bundles."""
        out_zip = tmp_path / "evidence.zip"
        build_evidence_pack(
            sdd_dir=mock_sdd,
            standard="ai-act",
            output_path=out_zip,
            bench_bundles=[sample_bench_bundle],
        )

        result = verify_evidence_pack(out_zip)
        assert result.passed is True
        assert len(result.errors) == 0

    def test_verify_evidence_pack_fails_on_tampered_bundle(
        self, mock_sdd: Path, sample_bench_bundle: SubmissionBundle, tmp_path: Path
    ) -> None:
        """AC-1: Tampering with a bundle file inside the zip causes verify to fail."""
        out_zip = tmp_path / "evidence.zip"
        build_evidence_pack(
            sdd_dir=mock_sdd,
            standard="ai-act",
            output_path=out_zip,
            bench_bundles=[sample_bench_bundle],
        )

        # Read zip and tamper with bench bundle
        tampered_buf = io.BytesIO()
        with zipfile.ZipFile(out_zip, "r") as src_zip:
            with zipfile.ZipFile(tampered_buf, "w") as dst_zip:
                for item in src_zip.infolist():
                    content = src_zip.read(item.filename)
                    if item.filename.startswith("bench/") and item.filename.endswith(".json"):
                        # Flip a byte
                        content = content.replace(b"audit-v1", b"audit-v2")
                    dst_zip.writestr(item, content)

        tampered_zip = tmp_path / "tampered.zip"
        tampered_zip.write_bytes(tampered_buf.getvalue())

        result = verify_evidence_pack(tampered_zip)
        assert result.passed is False
        assert any("tamper" in err.lower() or "mismatch" in err.lower() for err in result.errors)

    def test_verify_evidence_pack_fails_on_unsigned_bundle(self, mock_sdd: Path, tmp_path: Path) -> None:
        """AC-1: An unsigned bundle embedded in the pack causes verify to fail."""
        suite = build_golden_suite_v1()
        adapter = MockReplayAdapter()
        unsigned_bundle = BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run()

        out_zip = tmp_path / "evidence_unsigned.zip"
        build_evidence_pack(
            sdd_dir=mock_sdd,
            standard="ai-act",
            output_path=out_zip,
            bench_bundles=[unsigned_bundle],
        )

        result = verify_evidence_pack(out_zip)
        assert result.passed is False
        assert any("unsigned" in err.lower() or "signature" in err.lower() for err in result.errors)


# ===========================================================================
# AC-2: OSCAL Assessment Results Export & Validation
# ===========================================================================


class TestOscalExport:
    def test_oscal_export_structure_and_schema_validation(
        self, mock_sdd: Path, sample_bench_bundle: SubmissionBundle
    ) -> None:
        """AC-2: OSCAL output conforms to schema structure with assessment-results root."""
        oscal_doc = export_oscal_assessment_results(
            standard="ai-act",
            sdd_dir=mock_sdd,
            bench_bundles=[sample_bench_bundle],
        )

        assert validate_oscal_assessment_results(oscal_doc) is True
        assert "assessment-results" in oscal_doc
        ar = oscal_doc["assessment-results"]
        assert ar["metadata"]["oscal-version"] == "1.0.0"
        assert len(ar["results"]) == 1
        res = ar["results"][0]
        assert "reviewed-controls" in res
        assert "findings" in res

    def test_oscal_findings_state_mapping(self, mock_sdd: Path, sample_bench_bundle: SubmissionBundle) -> None:
        """AC-2: Measured controls map to satisfied/not-satisfied; unmeasured map to not-applicable/not-tested."""
        oscal_doc = export_oscal_assessment_results(
            standard="ai-act",
            sdd_dir=mock_sdd,
            bench_bundles=[sample_bench_bundle],
        )

        findings = oscal_doc["assessment-results"]["results"][0]["findings"]
        findings_by_id = {f["target"]["target-id"]: f for f in findings}

        # art-12(1) was measured with 100% pass rate -> satisfied
        assert "art-12(1)" in findings_by_id
        assert findings_by_id["art-12(1)"]["target"]["status"]["state"] == "satisfied"

        # art-12(2)(a) was not measured by the bundle -> declared-not-measured or not-tested
        assert "art-12(2)(a)" in findings_by_id
        assert findings_by_id["art-12(2)(a)"]["target"]["status"]["state"] in ("not-tested", "not-applicable")


# ===========================================================================
# AC-3: Determinism & Byte-Identity
# ===========================================================================


class TestDeterminism:
    def test_pack_and_oscal_byte_identical_across_runs(
        self, mock_sdd: Path, sample_bench_bundle: SubmissionBundle, tmp_path: Path
    ) -> None:
        """AC-3: Same inputs produce byte-identical evidence pack and OSCAL export."""
        pack1 = build_evidence_pack(
            sdd_dir=mock_sdd,
            standard="ai-act",
            output_path=tmp_path / "p1.zip",
            bench_bundles=[sample_bench_bundle],
        )
        pack2 = build_evidence_pack(
            sdd_dir=mock_sdd,
            standard="ai-act",
            output_path=tmp_path / "p2.zip",
            bench_bundles=[sample_bench_bundle],
        )

        assert pack1.sha256 == pack2.sha256
        assert (tmp_path / "p1.zip").read_bytes() == (tmp_path / "p2.zip").read_bytes()

        oscal1 = export_oscal_assessment_results(
            standard="ai-act", sdd_dir=mock_sdd, bench_bundles=[sample_bench_bundle]
        )
        oscal2 = export_oscal_assessment_results(
            standard="ai-act", sdd_dir=mock_sdd, bench_bundles=[sample_bench_bundle]
        )
        assert json.dumps(oscal1, sort_keys=True) == json.dumps(oscal2, sort_keys=True)
