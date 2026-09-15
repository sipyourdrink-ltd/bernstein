"""Tests for the secret leakage benchmark suite (Issue #5450)."""

from __future__ import annotations

from pathlib import Path

from bernstein.eval.bench.leakage_suite import (
    ALL_ENCODINGS,
    ALL_SEED_POINTS,
    ALL_SURFACES,
    ALL_TYPES,
    EXERCISED_SURFACES,
    CanaryEncoding,
    CanaryType,
    LeakageHit,
    LeakageReplayAdapter,
    ScanSurface,
    build_leakage_suite_v1,
    generate_canaries,
    probe_surface,
    run_leakage_probes,
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

    # Synthetic, per-run values: every canary either carries the nonce or is
    # derived from it (the API key is the nonce's hash behind a real prefix).
    api = next(c for c in canaries if c.canary_type == CanaryType.API_KEY)
    assert api.raw_value.startswith("AKIA") and len(api.raw_value) == 20
    for c in canaries:
        if c.canary_type != CanaryType.API_KEY:
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


def test_every_surface_is_driven_for_real_or_reported_as_not_exercised(tmp_path: Path) -> None:
    score, bundle = run_leakage_probes("probe_nonce_01", tmp_path)
    by_surface = {r.receipt["surface"]: r for r in bundle.task_results}
    assert set(by_surface) == {s.value for s in ScanSurface}
    for s in EXERCISED_SURFACES:
        assert by_surface[s.value].receipt["status"] in ("clean", "leaked"), s
    for s in ScanSurface:
        if s not in EXERCISED_SURFACES:
            assert by_surface[s.value].receipt["status"] == "not_exercised", s
            assert by_surface[s.value].passed is False
    assert set(score.surfaces_not_exercised) == {"journal", "receipts", "telemetry_export"}
    assert score.total_scanned_surfaces == 5
    assert bundle.suite_version == "leakage-v1"


def test_what_the_surfaces_return_today(tmp_path: Path) -> None:
    """The measurement, pinned: three surfaces leak, two are clean.

    The bench bundle, the evidence pack and the run archive write the bytes
    they are given; nothing redacts on those paths. The PR projection
    references the bundle without embedding evidence, and the log sanitizer
    escapes control characters -- it never claimed to redact, and the
    canary it lets through says so in the stage name.
    """
    score, bundle = run_leakage_probes("probe_nonce_02", tmp_path)
    status = {r.receipt["surface"]: r.receipt["status"] for r in bundle.task_results}
    assert status["bench_bundle"] == "leaked"
    assert status["evidence_pack"] == "leaked"
    assert status["run_archive"] == "leaked"
    assert status["pr_title_and_body"] == "clean"
    assert status["logs"] == "leaked"
    assert score.passed is False
    stages = {h.surface.value: h.redaction_stage for h in score.hits}
    assert stages["bench_bundle"].startswith("none:")
    assert stages["evidence_pack"].startswith("none:")


def test_a_hit_names_surface_encoding_and_stage(tmp_path: Path) -> None:
    score, _ = run_leakage_probes("probe_nonce_03", tmp_path)
    bundle_hits = [h for h in score.hits if h.surface is ScanSurface.BENCH_BUNDLE]
    assert {h.encoding for h in bundle_hits} >= {
        CanaryEncoding.PLAIN,
        CanaryEncoding.BASE64,
        CanaryEncoding.URL_ENCODED,
    }
    for h in bundle_hits:
        assert h.redaction_stage
        assert h.snippet


def test_the_pr_projection_does_not_embed_producer_output(tmp_path: Path) -> None:
    canaries = generate_canaries("probe_nonce_04")
    emitted = probe_surface(ScanSurface.PR_TITLE_AND_BODY, canaries, tmp_path)
    assert emitted is not None
    assert scan_surface(ScanSurface.PR_TITLE_AND_BODY, emitted, canaries) == []


def test_unexercised_surface_returns_none_not_clean(tmp_path: Path) -> None:
    canaries = generate_canaries("probe_nonce_05")
    assert probe_surface(ScanSurface.JOURNAL, canaries, tmp_path) is None


def test_bench_run_and_verify_through_the_installed_entry_point(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from bernstein.cli.main import cli
    from bernstein.eval.bench.bundle import SubmissionBundle

    out = tmp_path / "leakage.json"
    run = CliRunner().invoke(cli, ["bench", "run", "leakage-v1", "--out", str(out), "--stub-signer"])
    assert run.exit_code == 0, run.output
    bundle = SubmissionBundle.load(out)
    assert bundle.signer_fingerprint.endswith("-stub")
    statuses = {r.receipt["surface"]: r.receipt["status"] for r in bundle.task_results}
    assert statuses["bench_bundle"] == "leaked" and statuses["journal"] == "not_exercised"
    assert "Pass rate   : 12.5%" in run.output  # one clean surface of eight
    verify = CliRunner().invoke(cli, ["bench", "verify", str(out), "--suite", "leakage-v1"])
    assert verify.exit_code == 0, verify.output
    assert "MATCH" in verify.output


def test_a_receipt_rewritten_to_clean_is_fabricated() -> None:
    from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
    from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus

    suite = build_leakage_suite_v1()
    adapter = LeakageReplayAdapter(nonce="probe_nonce_06")
    task = next(t for t in suite.tasks if t.id == "leakage_bench_bundle")
    receipt = adapter.run_task(task, {})
    assert receipt["status"] == "leaked"
    forged = dict(receipt, status="clean", hits=[])
    bundle = SubmissionBundle(
        suite_hash=suite.suite_hash,
        suite_version=suite.version,
        task_results=[
            TaskResult(task_id=task.id, task_hash=task.content_hash(), receipt=forged, passed=False, score=0.0)
        ],
        scheduler_config={},
    )
    result = BenchVerifier(suite=suite, adapter=adapter).verify(bundle)
    assert result.status is VerificationStatus.DIVERGED
