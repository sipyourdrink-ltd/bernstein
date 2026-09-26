"""Tests for the secret leakage benchmark suite (Issue #5450)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.eval.bench.leakage_suite import (
    ALL_ENCODINGS,
    ALL_SEED_POINTS,
    ALL_SURFACES,
    ALL_TYPES,
    EXERCISED_SEED_POINTS,
    EXERCISED_SURFACES,
    CanaryEncoding,
    CanarySeedPoint,
    CanaryType,
    LeakageHit,
    LeakageReplayAdapter,
    ScanSurface,
    build_leakage_suite_v1,
    collapsed_encodings,
    generate_canaries,
    probe_surface,
    run_leakage_probes,
    scan_surface,
    score_from_bundle,
    score_leakage,
)


def test_canary_generator_covers_every_type_and_seed_point() -> None:
    canaries = generate_canaries(nonce="run_nonce_12345")
    assert len(canaries) > 0

    types_present = {c.canary_type for c in canaries}
    assert types_present == set(ALL_TYPES)

    seed_points_present = {c.seed_point for c in canaries}
    assert seed_points_present == set(ALL_SEED_POINTS)

    # Synthetic, per-run values: every canary either carries the nonce or is
    # derived from it (the API key is the nonce's hash behind a real prefix).
    api = next(c for c in canaries if c.canary_type == CanaryType.API_KEY)
    assert api.raw_value.startswith("AKIA") and len(api.raw_value) == 20
    for c in canaries:
        if c.canary_type != CanaryType.API_KEY:
            assert "run_nonce_12345" in c.raw_value or "canary" in c.raw_value.lower()


def test_every_generated_canary_is_a_distinct_byte_probe() -> None:
    """Five encoding labels are not five probes, and the suite says which collapse.

    ``json.dumps`` escapes nothing in any of these four values, and ``quote``
    leaves an alphanumeric key id and an underscore-separated nonce alone.
    Emitting those as separate canaries counted the plaintext probe up to
    three times per type, inflating the canary count and every hit list.
    """
    canaries = generate_canaries(nonce="distinct_1")
    by_scope: dict[tuple[str, str], list[str]] = {}
    for c in canaries:
        by_scope.setdefault((c.canary_type.value, c.seed_point.value), []).append(c.encoded_value)
    for scope, values in by_scope.items():
        assert len(values) == len(set(values)), scope

    collapsed = collapsed_encodings("distinct_1")
    assert "api_key: json_escaped == plain" in collapsed
    assert "api_key: url_encoded == plain" in collapsed
    assert "nonce: url_encoded == plain" in collapsed
    # An email and a path do survive url-encoding: "@" and "/" are escaped.
    assert not any(c.startswith("email: url_encoded") for c in collapsed)
    assert not any(c.startswith("internal_path: url_encoded") for c in collapsed)


def test_each_seed_point_injects_different_bytes() -> None:
    """A hit is only attributable if the value names the path it came in on."""
    canaries = generate_canaries(nonce="attribution_1")
    for c_type in ALL_TYPES:
        raws = {c.seed_point: c.raw_value for c in canaries if c.canary_type is c_type}
        assert len(set(raws.values())) == len(ALL_SEED_POINTS), c_type


def test_the_environment_seed_point_is_reported_not_silently_skipped(tmp_path: Path) -> None:
    """Nothing injects at ``environment``, so the score has to say so.

    None of the five exercised surfaces reads the process environment, so the
    canaries for that seed point were generated, never injected, and then
    counted in ``total_canaries_tested`` as though they had been.
    """
    assert CanarySeedPoint.ENVIRONMENT not in EXERCISED_SEED_POINTS
    score, _ = run_leakage_probes("env_probe_1", tmp_path)
    assert score.seed_points_not_exercised == ("environment",)
    assert score.to_dict()["seed_points_not_exercised"] == ["environment"]

    canaries = generate_canaries("env_probe_1")
    injected = [c for c in canaries if c.seed_point in EXERCISED_SEED_POINTS]
    assert score.total_canaries_tested == len(injected) < len(canaries)
    assert all(h.seed_point is not CanarySeedPoint.ENVIRONMENT for h in score.hits)


def test_scan_surface_detects_every_distinct_encoding_on_an_unredacted_surface() -> None:
    canaries = generate_canaries(nonce="leak_probe_77")
    present = {c.encoding for c in canaries}
    # json_escaped is plaintext for all four shapes, so it is never a probe.
    assert present == set(ALL_ENCODINGS) - {CanaryEncoding.JSON_ESCAPED}

    for encoding in sorted(present, key=lambda e: e.value):
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
        seed_point=CanarySeedPoint.TOOL_OUTPUT,
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


def test_plain_unsplit_leak_not_mislabelled_split_lines() -> None:
    canaries = generate_canaries(nonce="split_guard_1")
    canary = next(c for c in canaries if c.encoding == CanaryEncoding.SPLIT_LINES)
    text = f"log: {canary.raw_value} tail"
    hits = scan_surface(ScanSurface.LOGS, text, [canary])
    assert hits == []


def test_true_split_lines_still_detected() -> None:
    canaries = generate_canaries(nonce="split_guard_2")
    canary = next(c for c in canaries if c.encoding == CanaryEncoding.SPLIT_LINES)
    mid = len(canary.raw_value) // 2
    text = f"log: {canary.raw_value[:mid]}\n{canary.raw_value[mid:]} tail"
    hits = scan_surface(ScanSurface.LOGS, text, [canary])
    assert [h.encoding for h in hits] == [CanaryEncoding.SPLIT_LINES]


def test_a_hit_names_the_seed_point_it_was_injected_at(tmp_path: Path) -> None:
    """Attribution, end to end: the archive leaks, and the score says from where."""
    score, _ = run_leakage_probes("attribution_2", tmp_path)
    archive_hits = [h for h in score.hits if h.surface is ScanSurface.RUN_ARCHIVE]
    assert archive_hits
    for hit in archive_hits:
        assert hit.seed_point in EXERCISED_SEED_POINTS
        # The value carries its own provenance, so the attribution is checkable.
        assert hit.seed_point.value in hit.raw_value or hit.canary_type is CanaryType.API_KEY
        assert hit.to_dict()["seed_point"] == hit.seed_point.value
    # The archive zips the whole seeded .sdd, so more than one path reaches it.
    assert len({h.seed_point for h in archive_hits}) > 1


def test_the_score_serialises_what_it_did_not_cover(tmp_path: Path) -> None:
    """A frozen score that omits its gaps reads as a clean sweep of all eight."""
    score, _ = run_leakage_probes("coverage_1", tmp_path)
    payload = score.to_dict()
    assert payload["surfaces_not_exercised"] == ["journal", "receipts", "telemetry_export"]
    assert payload["seed_points_not_exercised"] == ["environment"]
    assert payload["encodings_collapsed"]
    assert payload["total_scanned_surfaces"] == len(EXERCISED_SURFACES)


def test_score_from_bundle_reproduces_the_score_the_probes_computed(tmp_path: Path) -> None:
    """The bundle is what `bench run` produces, so the coverage has to come back out of it."""
    score, bundle = run_leakage_probes("bundle_score_1", tmp_path)
    from_bundle = score_from_bundle(bundle)
    assert from_bundle.to_dict() == score.to_dict()


class TestReplayDerivesTheVerdictFromBytes:
    """`bench verify` calls ``score_task`` and never ``run_task``.

    So whatever ``score_task`` does not check, nobody checks. It used to read
    ``status`` straight back out of the receipt, which made the receipt its
    own authority: a bundle asserting ``clean`` verified as MATCH with
    nothing ever looking at a byte.
    """

    @staticmethod
    def _receipt(nonce: str = "replay_1", task_id: str = "leakage_bench_bundle") -> tuple[object, dict]:
        suite = build_leakage_suite_v1()
        adapter = LeakageReplayAdapter(nonce=nonce)
        task = next(t for t in suite.tasks if t.id == task_id)
        return adapter, adapter.run_task(task, {})

    def test_the_receipt_carries_the_bytes_and_the_canary_identity(self) -> None:
        import base64
        import hashlib

        _adapter, receipt = self._receipt()
        assert receipt["nonce"] == "replay_1"
        emitted = base64.b64decode(receipt["emitted_b64"])
        assert hashlib.sha256(emitted).hexdigest() == receipt["emitted_sha256"]
        assert receipt["status"] == "leaked"

    def test_a_clean_claim_over_leaking_bytes_is_refused(self) -> None:
        adapter, receipt = self._receipt()
        suite = build_leakage_suite_v1()
        task = next(t for t in suite.tasks if t.id == "leakage_bench_bundle")
        forged = dict(receipt, status="clean", hits=[])
        with pytest.raises(ValueError, match="contradicts its own evidence"):
            adapter.score_task(task, forged)

    def test_bytes_swapped_for_clean_ones_do_not_match_their_digest(self) -> None:
        import base64

        adapter, receipt = self._receipt()
        suite = build_leakage_suite_v1()
        task = next(t for t in suite.tasks if t.id == "leakage_bench_bundle")
        forged = dict(
            receipt,
            status="clean",
            hits=[],
            emitted_b64=base64.b64encode(b"nothing to see here").decode("ascii"),
        )
        with pytest.raises(ValueError, match="do not match the digest"):
            adapter.score_task(task, forged)

    def test_a_receipt_without_its_bytes_cannot_be_scored(self) -> None:
        adapter, receipt = self._receipt()
        suite = build_leakage_suite_v1()
        task = next(t for t in suite.tasks if t.id == "leakage_bench_bundle")
        with pytest.raises(ValueError, match="no emitted bytes"):
            adapter.score_task(task, dict(receipt, emitted_b64="", emitted_sha256=""))
        with pytest.raises(ValueError, match="no nonce"):
            adapter.score_task(task, dict(receipt, nonce=""))

    def test_an_exercised_surface_cannot_claim_it_was_not_exercised(self) -> None:
        adapter, receipt = self._receipt()
        suite = build_leakage_suite_v1()
        task = next(t for t in suite.tasks if t.id == "leakage_bench_bundle")
        with pytest.raises(ValueError, match="this suite drives that surface"):
            adapter.score_task(task, dict(receipt, status="not_exercised", hits=[]))

    def test_an_honest_receipt_still_scores(self) -> None:
        adapter, receipt = self._receipt()
        suite = build_leakage_suite_v1()
        task = next(t for t in suite.tasks if t.id == "leakage_bench_bundle")
        passed, score, detail = adapter.score_task(task, receipt)
        assert passed is False and score == 0.0
        assert detail["hits_count"] == len(receipt["hits"])

        clean_task = next(t for t in suite.tasks if t.id == "leakage_pr_title_and_body")
        clean_receipt = adapter.run_task(clean_task, {})
        assert adapter.score_task(clean_task, clean_receipt)[:2] == (True, 1.0)

    def test_a_verifier_with_a_different_nonce_still_reproduces_the_verdict(self) -> None:
        """The submitter's nonce comes out of the receipt, not out of the verifier."""
        from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
        from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus

        suite = build_leakage_suite_v1()
        submitter = LeakageReplayAdapter(nonce="submitter_nonce")
        task = next(t for t in suite.tasks if t.id == "leakage_bench_bundle")
        receipt = submitter.run_task(task, {})
        bundle = SubmissionBundle(
            suite_hash=suite.suite_hash,
            suite_version=suite.version,
            task_results=[
                TaskResult(task_id=task.id, task_hash=task.content_hash(), receipt=receipt, passed=False, score=0.0)
            ],
            scheduler_config={},
        )
        result = BenchVerifier(suite=suite, adapter=LeakageReplayAdapter(nonce="verifier_nonce")).verify(bundle)
        assert result.status is VerificationStatus.MATCH
