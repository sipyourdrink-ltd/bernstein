"""
Tests for benchmark bundles in evidence packs (Issue #5456, piece 1).

The OSCAL export over the same assessment is the second piece and carries
its own tests.
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
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
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

    def test_bundle_from_unknown_suite_is_named_not_silently_uncovered(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        (sdd / "audit").mkdir(parents=True)
        (sdd / "lineage").mkdir()
        (sdd / "metrics").mkdir()
        bundles_dir = sdd / "bench" / "bundles"
        bundles_dir.mkdir(parents=True)
        bundle = _signed_bundle(build_golden_suite_v1())
        raw = bundle.to_dict()
        unknown = SubmissionBundle(
            suite_hash=raw["suite_hash"],
            suite_version="vendor-suite-v9",
            task_results=bundle.task_results,
            scheduler_config=raw["scheduler_config"],
            submitted_at=raw["submitted_at"],
        )
        unknown.save(bundles_dir / "vendor.json")
        zip_path = sdd / "evidence.zip"
        build_evidence_pack(sdd_dir=sdd, standard="ai-act", output_path=zip_path)
        with zipfile.ZipFile(zip_path, "r") as zf:
            assessment = json.loads(zf.read("controls.json").decode("utf-8"))["bench_assessment"]
        assert assessment["_unresolvable_suites"] == ["vendor-suite-v9"]
        assert all(v["status"] == "declared_not_measured" for k, v in assessment.items() if k.startswith("CTL-"))


class TestPackTrustBoundaryIsPinned:
    """The pack re-checks a bundle's own hashes, not its authorship. These are the
    two gaps that follows from that, held as tests so no one rediscovers them by
    experiment and files them as bugs (#5856; the on-disk tamper is the #5496 class)."""

    @staticmethod
    def _sdd_with(tmp_path: Path, bundle: SubmissionBundle) -> Path:
        sdd = tmp_path / ".sdd"
        (sdd / "audit").mkdir(parents=True, exist_ok=True)
        (sdd / "audit" / "events.jsonl").write_text(
            json.dumps({"timestamp": "2026-01-01T00:00:00Z", "event_type": "tool_call", "hmac": "abc"}) + "\n",
            encoding="utf-8",
        )
        bundles_dir = sdd / "bench" / "bundles"
        bundles_dir.mkdir(parents=True, exist_ok=True)
        bundle.save(bundles_dir / f"{bundle.bundle_hash()}.json")
        return sdd

    def test_a_tampered_then_rehashed_bundle_still_verifies(self, tmp_path: Path) -> None:
        """A bundle whose contents were altered and then re-hashed and re-signed is
        self-consistent, so the pack embeds it and ``verify_evidence_pack`` passes it --
        exactly as ``bench verify`` would. Only the signature could catch this, and
        nothing checks it yet (#5856). Pinned so the property is on the record."""
        honest = _signed_bundle(build_golden_suite_v1())
        first = honest.task_results[0]
        forged_tr = TaskResult(
            task_id=first.task_id,
            task_hash=first.task_hash,
            receipt={**first.receipt, "run_id": "forged-after-the-fact"},
            passed=first.passed,
            score=first.score,
        )
        forged = StubSigner().sign(
            SubmissionBundle(
                suite_hash=honest.suite_hash,
                suite_version=honest.suite_version,
                task_results=[forged_tr, *honest.task_results[1:]],
                scheduler_config=honest.scheduler_config,
            )
        )
        sdd = self._sdd_with(tmp_path, forged)
        zip_path = sdd / "evidence.zip"
        build_evidence_pack(sdd_dir=sdd, standard="ai-act", output_path=zip_path)

        assert verify_evidence_pack(zip_path) is True
        with zipfile.ZipFile(zip_path, "r") as zf:
            embedded = json.loads(zf.read(f"bench-bundles/{forged.bundle_hash()}.json").decode("utf-8"))
        assert embedded["task_results"][0]["receipt"]["run_id"] == "forged-after-the-fact"

    def test_a_pack_carrying_an_unsigned_bundle_still_verifies(self, tmp_path: Path) -> None:
        """The pack does not require a bundle to be signed; an unsigned one verifies
        the same as a signed one, because verification is hash-consistency, not
        attestation (#5856)."""
        unsigned = BenchRunner(suite=build_golden_suite_v1(), adapter=MockReplayAdapter(), scheduler_config={}).run()
        assert unsigned.signature == ""
        sdd = self._sdd_with(tmp_path, unsigned)
        zip_path = sdd / "evidence.zip"
        build_evidence_pack(sdd_dir=sdd, standard="ai-act", output_path=zip_path)
        assert verify_evidence_pack(zip_path) is True
