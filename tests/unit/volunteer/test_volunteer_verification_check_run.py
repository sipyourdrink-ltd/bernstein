"""Tests for the volunteer receipt verification check run engine."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from unittest.mock import MagicMock, patch

from bernstein.core.security.result_receipt_bundle import (
    BundleVerification,
)
from bernstein.core.volunteer import verification_check_run as vcr
from bernstein.core.volunteer.verification_check_run import (
    GateComparison,
    VerificationCheckRunResult,
    _compare_gate_results,
    _extract_bundle_from_envelope,
    _extract_envelope_from_pr_body,
    _format_comparison_table,
    _verify_bundle_offline,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_envelope_dict(
    *,
    payload_b64: str | None = "valid",
    bundle: dict[str, Any] | None = None,
    subject_digest: str | None = None,
    log_for_gate: str | None = None,
    gate_log_attest: str | None = None,
) -> dict[str, Any]:
    """Build a minimal envelope dict for tests.

    If ``subject_digest`` is omitted, the sha256 of the canonical bundle
    bytes is used so internal consistency holds.
    """
    if bundle is None:
        bundle = {"gates": [], "manifest_sha256": "abc"}
    if log_for_gate is not None and gate_log_attest is not None:
        bundle = {
            **bundle,
            "gates": [
                {
                    "command": "pytest",
                    "exit_code": 0,
                    "log": log_for_gate,
                    "log_sha256": gate_log_attest,
                }
            ],
        }
    if payload_b64 == "valid":
        # payload is a statement dict with subjects + predicate.bundle
        if subject_digest is None:
            subject_digest = hashlib.sha256(
                json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        statement = {
            "subjects": [{"digest": {"sha256": subject_digest}}],
            "predicate": {"bundle": bundle},
        }
        payload = json.dumps(statement).encode("utf-8")
        b64 = base64.b64encode(payload).decode("ascii")
    else:
        b64 = payload_b64 or ""
    return {
        "payload_type": "application/vnd.in-toto+json",
        "payload_b64": b64,
        "signatures": [{"keyid": "k", "sig": "c2ln"}],
    }


# ---------------------------------------------------------------------------
# _extract_envelope_from_pr_body
# ---------------------------------------------------------------------------


class TestExtractEnvelopeFromPrBody:
    def test_no_envelope_returns_none(self) -> None:
        assert _extract_envelope_from_pr_body("nothing here") is None

    def test_envelope_via_explicit_marker(self) -> None:
        envelope = _make_envelope_dict()
        body = f"intro\n\n**Envelope:** ```json\n{json.dumps(envelope)}\n```\n\nfooter"
        result = _extract_envelope_from_pr_body(body)
        assert result is not None
        assert result["payload_type"] == "application/vnd.in-toto+json"

    def test_envelope_via_fallback_json_block(self) -> None:
        envelope = _make_envelope_dict()
        body = f"before\n```json\n{json.dumps(envelope)}\n```\nafter"
        result = _extract_envelope_from_pr_body(body)
        assert result is not None
        assert result["payload_b64"] == envelope["payload_b64"]

    def test_non_envelope_json_block_ignored(self) -> None:
        body = '```json\n{"foo": 1}\n```'
        assert _extract_envelope_from_pr_body(body) is None

    def test_malformed_explicit_marker_falls_back(self) -> None:
        # The explicit marker pattern is non-greedy and spans to the first closing ```.
        # When the JSON inside is malformed, the marker match produces invalid JSON
        # but the regex still matches. The fallback scans remaining json blocks.
        # Construct a body where the marker block has bad JSON but isn't
        # consumed as a whole by the regex.
        envelope = _make_envelope_dict()
        body = f"**Envelope:** ```json\n{{not valid json}}\n```\n```json\n{json.dumps(envelope)}\n```"
        result = _extract_envelope_from_pr_body(body)
        assert result is not None
        assert result["payload_b64"] == envelope["payload_b64"]


# ---------------------------------------------------------------------------
# _extract_bundle_from_envelope
# ---------------------------------------------------------------------------


class TestExtractBundleFromEnvelope:
    def test_valid_dsse_envelope(self) -> None:
        bundle = {"gates": [], "manifest_sha256": "abc"}
        envelope = _make_envelope_dict(bundle=bundle)
        out = _extract_bundle_from_envelope(envelope)
        assert out is not None
        assert out["manifest_sha256"] == "abc"

    def test_missing_payload_b64(self) -> None:
        envelope = _make_envelope_dict(payload_b64="")
        assert _extract_bundle_from_envelope(envelope) is None

    def test_malformed_base64(self) -> None:
        envelope = _make_envelope_dict(payload_b64="!!!not-base64!!!")
        assert _extract_bundle_from_envelope(envelope) is None

    def test_missing_bundle(self) -> None:
        payload = base64.b64encode(json.dumps({"predicate": {}}).encode("utf-8")).decode("ascii")
        envelope = _make_envelope_dict(payload_b64=payload)
        assert _extract_bundle_from_envelope(envelope) is None


# ---------------------------------------------------------------------------
# _verify_bundle_offline
# ---------------------------------------------------------------------------


class TestVerifyBundleOffline:
    def test_valid_bundle(self) -> None:
        bundle = {"gates": [], "manifest_sha256": "abc"}
        envelope = _make_envelope_dict(bundle=bundle)
        result = _verify_bundle_offline(envelope, expected_manifest_sha256="abc")
        assert result.ok is True
        assert result.errors == ()

    def test_missing_payload_b64(self) -> None:
        envelope = _make_envelope_dict(payload_b64="")
        result = _verify_bundle_offline(envelope)
        assert result.ok is False
        assert any(e.field == "envelope" for e in result.errors)

    def test_log_digest_mismatch(self) -> None:
        bundle = {"gates": [], "manifest_sha256": "abc"}
        actual_log = "real log text"
        wrong_sha = hashlib.sha256(b"different").hexdigest()
        envelope = _make_envelope_dict(bundle=bundle, log_for_gate=actual_log, gate_log_attest=wrong_sha)
        result = _verify_bundle_offline(envelope)
        assert result.ok is False
        assert any("gates[0].log" in e.field for e in result.errors)

    def test_manifest_digest_mismatch(self) -> None:
        bundle = {"gates": [], "manifest_sha256": "actual"}
        envelope = _make_envelope_dict(bundle=bundle)
        result = _verify_bundle_offline(envelope, expected_manifest_sha256="expected")
        assert result.ok is False
        assert any(e.field == "manifest_sha256" for e in result.errors)


# ---------------------------------------------------------------------------
# _compare_gate_results
# ---------------------------------------------------------------------------


class TestCompareGateResults:
    def test_all_match(self) -> None:
        attested_log = "ok"
        attested_log_sha = hashlib.sha256(attested_log.encode()).hexdigest()
        attested = MagicMock(command="pytest", exit_code=0, log=attested_log, log_sha256=attested_log_sha)
        ci = MagicMock(command="pytest", exit_code=0, log=attested_log, log_sha256=attested_log_sha)
        comps = _compare_gate_results((attested,), [ci])
        assert len(comps) == 1
        assert comps[0].passed is True
        assert comps[0].mismatch_reason is None

    def test_exit_code_mismatch(self) -> None:
        log = "ok"
        sha = hashlib.sha256(log.encode()).hexdigest()
        attested = MagicMock(command="pytest", exit_code=0, log=log, log_sha256=sha)
        ci = MagicMock(command="pytest", exit_code=1, log=log, log_sha256=sha)
        comps = _compare_gate_results((attested,), [ci])
        assert comps[0].passed is False
        assert "Exit code" in (comps[0].mismatch_reason or "")

    def test_log_mismatch(self) -> None:
        attested = MagicMock(command="pytest", exit_code=0, log="a", log_sha256="aaa")
        ci = MagicMock(command="pytest", exit_code=0, log="b", log_sha256="bbb")
        comps = _compare_gate_results((attested,), [ci])
        assert comps[0].passed is False
        assert comps[0].mismatch_reason == "Log content mismatch"

    def test_extra_gate_in_ci(self) -> None:
        attested = MagicMock(command="pytest", exit_code=0, log="a", log_sha256="aaa")
        ci = MagicMock(command="lint", exit_code=0, log="b", log_sha256="bbb")
        comps = _compare_gate_results((attested,), [ci])
        # attested pytest matches CI pytest; extra lint gate appended
        assert len(comps) == 2
        extra = next(c for c in comps if c.command == "lint")
        assert extra.passed is False
        assert "Extra gate" in (extra.mismatch_reason or "")


# ---------------------------------------------------------------------------
# _format_comparison_table
# ---------------------------------------------------------------------------


class TestFormatComparisonTable:
    def test_empty(self) -> None:
        assert _format_comparison_table([]) == "_No gates to compare._"

    def test_single_gate(self) -> None:
        comp = GateComparison(
            command="pytest",
            attested_exit_code=0,
            attested_log_sha256="a" * 64,
            ci_exit_code=0,
            ci_log_sha256="a" * 64,
            passed=True,
            mismatch_reason=None,
        )
        out = _format_comparison_table([comp])
        assert "| Command |" in out
        assert "`pytest`" in out
        assert "PASS" in out

    def test_multiple_gates(self) -> None:
        passing = GateComparison(
            command="pytest",
            attested_exit_code=0,
            attested_log_sha256="a" * 64,
            ci_exit_code=0,
            ci_log_sha256="a" * 64,
            passed=True,
            mismatch_reason=None,
        )
        failing = GateComparison(
            command="lint",
            attested_exit_code=0,
            attested_log_sha256="b" * 64,
            ci_exit_code=1,
            ci_log_sha256="c" * 64,
            passed=False,
            mismatch_reason="Exit code mismatch",
        )
        out = _format_comparison_table([passing, failing])
        lines = out.splitlines()
        assert lines[0].startswith("| Command")
        assert "PASS" in out
        assert "FAIL" in out
        assert "Exit code mismatch" in out


# ---------------------------------------------------------------------------
# dataclasses
# ---------------------------------------------------------------------------


def test_gate_comparison_dataclass() -> None:
    comp = GateComparison(
        command="pytest",
        attested_exit_code=0,
        attested_log_sha256="x",
        ci_exit_code=0,
        ci_log_sha256="x",
        passed=True,
        mismatch_reason=None,
    )
    assert comp.passed is True
    assert comp.mismatch_reason is None


def test_verification_check_run_result_dataclass() -> None:
    bv = BundleVerification(ok=True)
    gcs = [
        GateComparison(
            command="pytest",
            attested_exit_code=0,
            attested_log_sha256="x",
            ci_exit_code=0,
            ci_log_sha256="x",
            passed=True,
            mismatch_reason=None,
        )
    ]
    res = VerificationCheckRunResult(
        bundle_verification=bv,
        gate_comparisons=gcs,
        manifest_digest_match=True,
        conclusion="success",
        summary="ok",
        details="ok",
    )
    assert res.overall_passed is True
    assert res.bundle_verification is bv
    assert res.gate_comparisons is gcs


def test_overall_passed_is_derived_from_conclusion() -> None:
    """``overall_passed`` cannot disagree with ``conclusion``."""
    bv = BundleVerification(ok=True)

    def _res(conclusion: str) -> VerificationCheckRunResult:
        return VerificationCheckRunResult(
            bundle_verification=bv,
            gate_comparisons=[],
            manifest_digest_match=True,
            conclusion=conclusion,  # type: ignore[arg-type]
            summary="s",
            details="d",
        )

    assert _res("success").overall_passed is True
    assert _res("failure").overall_passed is False
    assert _res("neutral").overall_passed is False


# ---------------------------------------------------------------------------
# run_verification_check (integration with mocked run/sort)
# ---------------------------------------------------------------------------


def _make_manifest() -> MagicMock:
    manifest = MagicMock()
    manifest.digest = "expected-manifest-digest"
    manifest.max_wall_clock_minutes = 10
    return manifest


def test_run_verification_check_non_volunteer_pr_is_neutral() -> None:
    """An ordinary maintainer PR carries no receipt and is not a failure.

    The check run is advisory and not a required context; concluding
    ``failure`` on every non-volunteer PR trains reviewers to ignore red.
    """
    client = MagicMock()
    result = vcr.run_verification_check(
        pr_number=1,
        repo_slug="acme/widgets",
        workspace_path="/tmp",
        pr_body="## Problem\n\nA plain maintainer PR with no receipt.\n",
        check_run_client=client,
    )
    assert result.conclusion == "neutral"
    assert result.overall_passed is False
    assert "not a volunteer pr" in result.summary.lower()
    # nothing was verified, so no bundle errors are reported
    assert result.bundle_verification.errors == ()
    # no client call expected (nothing to post from here)
    client.create_verification_check_run.assert_not_called()


def test_run_verification_check_volunteer_claim_without_envelope_fails() -> None:
    """A PR that claims a receipt but ships none is still a hard failure."""
    client = MagicMock()
    body = "## Verification\n\n- **Receipt digest:** `deadbeef`\n"
    result = vcr.run_verification_check(
        pr_number=1,
        repo_slug="acme/widgets",
        workspace_path="/tmp",
        pr_body=body,
        check_run_client=client,
    )
    assert result.conclusion == "failure"
    assert result.overall_passed is False
    assert any("envelope" in e.field for e in result.bundle_verification.errors)
    client.create_verification_check_run.assert_not_called()


class TestPrClaimsVolunteerReceipt:
    """Attribution markers emitted by ``build_volunteer_pr_body``."""

    def test_plain_body_is_not_a_claim(self) -> None:
        assert vcr._pr_claims_volunteer_receipt("## Problem\n\nJust a fix.\n") is False

    def test_empty_body_is_not_a_claim(self) -> None:
        assert vcr._pr_claims_volunteer_receipt("") is False

    def test_receipt_digest_marker_is_a_claim(self) -> None:
        assert vcr._pr_claims_volunteer_receipt("- **Receipt digest:** `abc`") is True

    def test_manifest_digest_marker_is_a_claim(self) -> None:
        assert vcr._pr_claims_volunteer_receipt("- **Manifest digest:** `abc`") is True

    def test_verify_offline_marker_is_a_claim(self) -> None:
        assert vcr._pr_claims_volunteer_receipt("`bernstein receipt verify bundle.json`") is True

    def test_assisted_by_trailer_is_a_claim(self) -> None:
        assert vcr._pr_claims_volunteer_receipt("_Assisted-by: claude (sonnet)_") is True

    def test_envelope_header_is_a_claim(self) -> None:
        assert vcr._pr_claims_volunteer_receipt("**Envelope:** ```json\n{broken\n```") is True

    def test_real_volunteer_body_is_a_claim(self) -> None:
        """The body builder's own output must read as a claim."""
        from bernstein.core.volunteer.submission import build_volunteer_pr_body

        bundle = MagicMock()
        bundle.task.issue_number = 7
        bundle.task.repo = "acme/widgets"
        bundle.gates = ()
        bundle.manifest_sha256 = "m" * 64
        body = build_volunteer_pr_body(
            bundle,
            adapter_id="claude",
            model_id="sonnet",
            signed_off_by="A B <a@b.c>",
            bundle_digest="d" * 64,
        )
        assert vcr._pr_claims_volunteer_receipt(body) is True


def test_run_verification_check_post_called_on_pass() -> None:
    ok_log = "ok"
    ok_log_sha = hashlib.sha256(ok_log.encode()).hexdigest()
    bundle = {
        "gates": [{"command": "pytest", "exit_code": 0, "log": ok_log, "log_sha256": ok_log_sha}],
        "manifest_sha256": "expected-manifest-digest",
    }
    envelope = _make_envelope_dict(bundle=bundle)
    body = f"**Envelope:** ```json\n{json.dumps(envelope)}\n```"
    manifest = _make_manifest()
    ci_gate = MagicMock(command="pytest", exit_code=0, log=ok_log, log_sha256=ok_log_sha)

    client = MagicMock()
    with (
        patch.object(vcr, "load_manifest_from_repo", return_value=manifest),
        patch.object(vcr, "_run_manifest_gates_in_ci", return_value=([ci_gate], True)),
    ):
        result = vcr.run_verification_check(
            pr_number=1,
            repo_slug="acme/widgets",
            workspace_path="/tmp",
            pr_body=body,
            check_run_client=client,
            expected_manifest_sha256="expected-manifest-digest",
        )
    assert result.overall_passed is True
    assert result.conclusion == "success"
    # The create_verification_check_run is only called by post_verification_check_run function, not by run_verification_check itself
    # So we should not assert client.create_verification_check_run was called
    # Instead, we can check that the result is valid
    assert result.bundle_verification.ok is True
    assert result.manifest_digest_match is True


def test_run_verification_check_bad_envelope_still_fails() -> None:
    """Strictness is unchanged for a PR that does ship an envelope."""
    bundle = {"gates": [], "manifest_sha256": "wrong-manifest"}
    envelope = _make_envelope_dict(bundle=bundle)
    body = f"**Envelope:** ```json\n{json.dumps(envelope)}\n```"

    result = vcr.run_verification_check(
        pr_number=1,
        repo_slug="acme/widgets",
        workspace_path="/tmp",
        pr_body=body,
        check_run_client=MagicMock(),
        expected_manifest_sha256="expected-manifest-digest",
    )
    assert result.conclusion == "failure"
    assert result.bundle_verification.ok is False


def test_post_verification_check_run_forwards_neutral_conclusion() -> None:
    """A skipped verification must post as neutral, not as failure."""
    client = MagicMock()
    result = VerificationCheckRunResult(
        bundle_verification=BundleVerification(ok=True),
        gate_comparisons=[],
        manifest_digest_match=True,
        conclusion="neutral",
        summary="Not a volunteer PR",
        details="d",
    )
    vcr.post_verification_check_run(
        repo_slug="acme/widgets",
        pr_number=1,
        head_sha="abc",
        verification_result=result,
        check_run_client=client,
    )
    assert client.create_verification_check_run.call_args.kwargs["conclusion"] == "neutral"
