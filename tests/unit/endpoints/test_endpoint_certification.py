"""Signed endpoint certification receipt tests (issue #2356).

The certification result is not a boolean in config: it is a signed receipt
anchored in the lineage spine and mirrored into the HMAC audit chain. These
tests prove the receipt verifies offline, that tampering is detected, and
that two identical conformance runs seal byte-identical receipts.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.endpoints.certification import (
    CERTIFICATION_RUN_ID,
    EndpointCertification,
    build_endpoint_certification,
    certification_path,
    certified_roles_for_endpoint,
    endpoint_fingerprint,
    load_or_create_endpoint_identity,
    read_endpoint_certification,
    validate_endpoint_assignments,
    verify_endpoint_certification,
)
from bernstein.core.endpoints.conformance import (
    ConformanceTranscript,
    evaluate_roles,
    run_conformance,
)
from tests.unit.endpoints.stub_endpoint import EndpointBehavior, FakeTransport

_KEY = b"0" * 32
_BASE_URL = "http://127.0.0.1:11434/v1"
_MODEL = "tiny-coder"
_ROLES = ("linter", "test_writer", "triage", "doc_sweeper", "manager")


def _transcript(behavior: EndpointBehavior | None = None) -> ConformanceTranscript:
    return run_conformance(base_url=_BASE_URL, model=_MODEL, transport=FakeTransport(behavior))


def _build(
    tmp_path: Path,
    *,
    behavior: EndpointBehavior | None = None,
    timestamp: int = 1000,
    chain: object | None = None,
) -> EndpointCertification:
    transcript = _transcript(behavior)
    priv, pub = load_or_create_endpoint_identity(tmp_path / ".sdd" / "identity")
    return build_endpoint_certification(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        transcript=transcript,
        verdicts=evaluate_roles(transcript, _ROLES),
        engine="stub",
        timestamp=timestamp,
        chain=chain,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Build + verify offline
# ---------------------------------------------------------------------------


def test_build_writes_receipt_that_verifies_offline(tmp_path: Path) -> None:
    cert = _build(tmp_path)
    fingerprint = endpoint_fingerprint(_BASE_URL, _MODEL)
    path = certification_path(tmp_path, fingerprint)
    assert path.is_file()
    assert cert.signature
    assert cert.journal_entry_hash

    result = verify_endpoint_certification(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        base_url=_BASE_URL,
        model=_MODEL,
    )
    assert result.ok is True
    assert result.reason == ""


def test_read_round_trips_the_sealed_receipt(tmp_path: Path) -> None:
    sealed = _build(tmp_path)
    loaded = read_endpoint_certification(tmp_path, _BASE_URL, _MODEL)
    assert loaded == sealed
    assert loaded is not None
    assert loaded.certified_roles() == frozenset({"linter", "test_writer", "triage", "doc_sweeper", "manager"})


def test_rejection_receipt_records_reasons(tmp_path: Path) -> None:
    cert = _build(tmp_path, behavior=EndpointBehavior(tools_ok=False))
    assert "test_writer" not in cert.certified_roles()
    assert "manager" not in cert.certified_roles()
    assert "linter" in cert.certified_roles()
    rejected = {v["role"]: v for v in cert.verdicts if not v["certified"]}
    assert rejected["test_writer"]["reasons"]


def test_missing_receipt_verify_reports_no_receipt(tmp_path: Path) -> None:
    result = verify_endpoint_certification(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        base_url=_BASE_URL,
        model=_MODEL,
    )
    assert result.ok is False
    assert "no certification" in result.reason


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


def test_tampered_receipt_fails_signature_verification(tmp_path: Path) -> None:
    # Build against a tool-incapable endpoint so the manager verdict is a
    # rejection, then forge it into a certification by editing the receipt.
    _build(tmp_path, behavior=EndpointBehavior(tools_ok=False))
    path = certification_path(tmp_path, endpoint_fingerprint(_BASE_URL, _MODEL))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert any(v["role"] == "manager" and not v["certified"] for v in data["verdicts"])
    data["verdicts"] = [
        {**v, "certified": True, "reasons": []} if v["role"] == "manager" else v for v in data["verdicts"]
    ]
    path.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    result = verify_endpoint_certification(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        base_url=_BASE_URL,
        model=_MODEL,
    )
    assert result.ok is False
    assert "signature" in result.reason


def test_tampered_spine_fails_anchor_verification(tmp_path: Path) -> None:
    _build(tmp_path)
    spine_log = tmp_path / ".sdd" / "lineage" / CERTIFICATION_RUN_ID / "spine.jsonl"
    assert spine_log.is_file()
    tampered = spine_log.read_bytes().replace(_MODEL.encode("ascii"), b"tamp-model")
    assert tampered != spine_log.read_bytes(), "tamper must actually change the spine row"
    spine_log.write_bytes(tampered)

    result = verify_endpoint_certification(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        base_url=_BASE_URL,
        model=_MODEL,
    )
    assert result.ok is False


def test_certified_roles_for_endpoint_is_empty_after_tampering(tmp_path: Path) -> None:
    _build(tmp_path)
    assert "linter" in certified_roles_for_endpoint(tmp_path, _BASE_URL, _MODEL)

    path = certification_path(tmp_path, endpoint_fingerprint(_BASE_URL, _MODEL))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["model"] = "other-model"
    path.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    assert certified_roles_for_endpoint(tmp_path, _BASE_URL, "other-model") == frozenset()


# ---------------------------------------------------------------------------
# Determinism: identical runs seal identical receipts
# ---------------------------------------------------------------------------


def test_identical_runs_seal_byte_identical_bindings(tmp_path: Path) -> None:
    cert_a = _build(tmp_path / "a", timestamp=1234)
    cert_b = _build(tmp_path / "b", timestamp=1234)
    assert cert_a.to_canonical_bytes() == cert_b.to_canonical_bytes()
    assert cert_a.certification_hash() == cert_b.certification_hash()


def test_fingerprint_is_stable_and_normalized(tmp_path: Path) -> None:
    assert endpoint_fingerprint(_BASE_URL, _MODEL) == endpoint_fingerprint(_BASE_URL + "/", _MODEL)
    assert endpoint_fingerprint(_BASE_URL, _MODEL) != endpoint_fingerprint(_BASE_URL, "other")


# ---------------------------------------------------------------------------
# Audit chain mirror
# ---------------------------------------------------------------------------


def test_certification_is_mirrored_into_audit_chain(tmp_path: Path) -> None:
    from bernstein.core.security.audit_chain import (
        EVENT_ENDPOINT_CERTIFICATION,
        AuditChainStore,
    )

    chain = AuditChainStore(tmp_path / ".sdd" / "audit", key=_KEY)
    cert = _build(tmp_path, chain=chain)

    events = [e for e in chain.query() if e.event_type == EVENT_ENDPOINT_CERTIFICATION]
    assert len(events) == 1
    details = events[0].details
    assert details["fingerprint"] == endpoint_fingerprint(_BASE_URL, _MODEL)
    assert details["transcript_hash"] == cert.transcript_hash()
    assert details["journal_entry_hash"] == cert.journal_entry_hash
    assert "linter" in details["certified_roles"]
    assert "prev_chain_digest" in details


# ---------------------------------------------------------------------------
# Config gate helper (AC3)
# ---------------------------------------------------------------------------


def test_gated_role_without_receipt_yields_clear_error(tmp_path: Path) -> None:
    errors = validate_endpoint_assignments(
        [("manager", "workhorse", _BASE_URL, _MODEL)],
        workdir=tmp_path,
    )
    assert len(errors) == 1
    assert "manager" in errors[0]
    assert "workhorse" in errors[0]
    assert "doctor --endpoint" in errors[0]


def test_gated_role_with_certifying_receipt_passes(tmp_path: Path) -> None:
    _build(tmp_path)
    errors = validate_endpoint_assignments(
        [("manager", "workhorse", _BASE_URL, _MODEL)],
        workdir=tmp_path,
    )
    assert errors == []


def test_gated_role_with_rejecting_receipt_fails_with_reasons(tmp_path: Path) -> None:
    _build(tmp_path, behavior=EndpointBehavior(tools_ok=False))
    errors = validate_endpoint_assignments(
        [("manager", "workhorse", _BASE_URL, _MODEL)],
        workdir=tmp_path,
    )
    assert len(errors) == 1
    assert "manager" in errors[0]


def test_low_stakes_role_passes_without_receipt(tmp_path: Path) -> None:
    errors = validate_endpoint_assignments(
        [("linter", "workhorse", _BASE_URL, _MODEL), ("triage", "workhorse", _BASE_URL, _MODEL)],
        workdir=tmp_path,
    )
    assert errors == []
