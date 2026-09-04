"""Tests for the secret leakage benchmark suite (Issue #5450)."""

from __future__ import annotations

from pathlib import Path

from bernstein.eval.bench.leakage_suite import (
    ALL_ENCODINGS,
    ALL_SEED_POINTS,
    ALL_SURFACES,
    ALL_TYPES,
    CanaryEncoding,
    CanaryType,
    LeakageHit,
    LeakageSuite,
    ScanSurface,
    generate_canaries,
    scan_surface,
    score_leakage,
)


def test_canary_generator_creates_all_types_and_encodings() -> None:
    canaries = generate_canaries(nonce="run_nonce_12345")
    assert len(canaries) > 0

    types_present = {c.canary_type for c in canaries}
    assert types_present == set(ALL_TYPES)

    encodings_present = {c.encoding for c in canaries}
    assert encodings_present == set(ALL_ENCODINGS)

    seed_points_present = {c.seed_point for c in canaries}
    assert seed_points_present == set(ALL_SEED_POINTS)

    # Verify no real secrets generated (run-time synthetic values)
    for c in canaries:
        assert "run_nonce_12345" in c.raw_value or "canary" in c.raw_value.lower()


def test_scan_surface_detects_all_5_encodings_on_unredacted_surface() -> None:
    canaries = generate_canaries(nonce="leak_probe_77")

    for encoding in ALL_ENCODINGS:
        matching_canaries = [c for c in canaries if c.encoding == encoding]
        assert matching_canaries
        canary = matching_canaries[0]

        # Put encoded canary directly into unredacted surface text
        raw_text = f"Log line with data: {canary.encoded_value} tail"
        hits = scan_surface(ScanSurface.LOGS, raw_text, [canary])
        assert len(hits) >= 1
        assert hits[0].canary_type == canary.canary_type
        assert hits[0].encoding == canary.encoding
        assert hits[0].surface == ScanSurface.LOGS
        assert hits[0].redaction_stage != ""


def test_scan_surface_clean_when_redacted() -> None:
    canaries = generate_canaries(nonce="clean_probe_99")
    clean_text = "All sensitive data has been redacted: *** [REDACTED] ***"
    hits = scan_surface(ScanSurface.PR_TITLE_AND_BODY, clean_text, canaries)
    assert len(hits) == 0


def test_all_8_surfaces_scanned() -> None:
    assert len(ALL_SURFACES) == 8
    expected_surfaces = {
        ScanSurface.JOURNAL,
        ScanSurface.RECEIPTS,
        ScanSurface.PR_TITLE_AND_BODY,
        ScanSurface.LOGS,
        ScanSurface.TELEMETRY_EXPORT,
        ScanSurface.EVIDENCE_PACK,
        ScanSurface.BENCH_BUNDLE,
        ScanSurface.RUN_ARCHIVE,
    }
    assert set(ALL_SURFACES) == expected_surfaces


def test_score_leakage_zero_hits_passes() -> None:
    score = score_leakage(hits=[], total_surfaces=8, total_canaries=40)
    assert score.passed is True
    assert len(score.hits) == 0


def test_score_leakage_with_hits_fails_and_reports_stage() -> None:
    hit = LeakageHit(
        canary_type=CanaryType.API_KEY,
        raw_value="TESTKEY_secret",
        encoding=CanaryEncoding.PLAIN,
        surface=ScanSurface.EVIDENCE_PACK,
        redaction_stage="evidence_pack_sanitizer",
        snippet="Found TESTKEY_secret in pack",
    )
    score = score_leakage(hits=[hit], total_surfaces=8, total_canaries=40)
    assert score.passed is False
    assert len(score.hits) == 1
    assert score.hits[0].surface == ScanSurface.EVIDENCE_PACK

    assert score.hits[0].redaction_stage == "evidence_pack_sanitizer"


def test_leakage_suite_execution_and_bundle_generation(tmp_path: Path) -> None:
    suite = LeakageSuite()
    bench_suite = suite.build_bench_suite()
    assert bench_suite.version != ""
    assert len(bench_suite.tasks) >= 8

    # Run clean simulation
    score, bundle = suite.run_simulation(clean=True)
    assert score.passed is True
    assert bundle is not None
    assert bundle.suite_version == bench_suite.version
    assert bundle.overall_score == 1.0

    # Run leaking simulation
    leaking_score, leaking_bundle = suite.run_simulation(clean=False)
    assert leaking_score.passed is False
    assert len(leaking_score.hits) > 0
    assert leaking_bundle.overall_score == 0.0
