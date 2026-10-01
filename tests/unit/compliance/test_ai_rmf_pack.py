"""NIST AI RMF pack: Covered rows carry chain entries; empty windows do not claim (#4915)."""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.adapters.admission import anchor_admission_receipt_in_lineage
from bernstein.core.agents.computer_use_attestation import _artefact_path as computer_use_artefact_path
from bernstein.core.compliance.ai_rmf import (
    COVERED_EVIDENCE_SELECTORS,
    PACK_KIND_AI_RMF,
    build_ai_rmf_pack,
    parse_mapping_rows,
)
from bernstein.core.datasources.receipt import _artefact_path as datasource_artefact_path
from bernstein.core.lineage.artifact_record import artifact_entry_path
from bernstein.core.lineage.coverage import anchor_coverage_record
from bernstein.core.lineage.entry import LineageEntry, canonicalise, entry_hash
from bernstein.core.lineage.identity import AgentCard, generate_keypair, sign_detached
from bernstein.core.lineage.provenance import TrustClass, record_tool_result
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore
from bernstein.core.payments.receipt import _RECEIPT_ARTEFACT_KIND as PAYMENT_RECEIPT_KIND
from bernstein.core.payments.receipt import receipt_artefact_path
from bernstein.core.tasks.artifacts import ArtifactKind

REPO_ROOT = Path(__file__).resolve().parents[3]
MAPPING = REPO_ROOT / "docs" / "compliance" / "nist-ai-rmf-mapping.md"


def _date_to_ns(day: str) -> int:
    parsed = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1_000_000_000)


def _operator_key(tmp_path: Path) -> Path:
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "operator.key"
    key_path.write_bytes(pem)
    return key_path


def _make_entry(*, path: str, content: str, agent_id: str, kid: str, ts_ns: int) -> LineageEntry:
    return LineageEntry(
        v=1,
        artefact_path=path,
        artefact_kind="file",
        content_hash="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
        parent_hashes=[],
        agent_id=agent_id,
        agent_card_kid=kid,
        tool_call_id=f"tc-{ts_ns}",
        span_id=f"{ts_ns:016x}"[:16],
        ts_ns=ts_ns,
        operator_hmac="deadbeef",
    )


def _write_lineage(tmp_path: Path, entries: list[LineageEntry]) -> dict[str, Path]:
    lineage_dir = tmp_path / "lineage"
    signatures_dir = lineage_dir / "signatures"
    agent_cards_dir = tmp_path / "agents"
    lineage_dir.mkdir()
    signatures_dir.mkdir()
    agent_cards_dir.mkdir()
    priv_pem, pub_pem = generate_keypair()
    agent_id = "agent:worker-1"
    kid = f"{agent_id}-kid"
    (agent_cards_dir / f"{agent_id.replace(':', '_')}.json").write_text(
        json.dumps({"agent_id": agent_id, "kid": kid, "public_key_pem": pub_pem}, sort_keys=True),
        encoding="utf-8",
    )
    log_path = lineage_dir / "log.jsonl"
    with log_path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(canonicalise(entry).decode("utf-8") + "\n")
            digest = entry_hash(entry)
            jws = sign_detached(canonicalise(entry), priv_pem, kid=kid)
            (signatures_dir / f"{digest.split(':', 1)[1]}.jws").write_text(jws, encoding="utf-8")
    return {"lineage_dir": lineage_dir, "agent_cards_dir": agent_cards_dir}


def _build(tmp_path: Path, layout: dict[str, Path], name: str) -> Path:
    out = tmp_path / name
    build_ai_rmf_pack(
        since=date(2026, 1, 1),
        until=date(2026, 6, 30),
        org="Acme",
        lineage_dir=layout["lineage_dir"],
        agent_cards_dir=layout["agent_cards_dir"],
        mapping_path=MAPPING,
        output_path=out,
        operator_key_path=_operator_key(tmp_path),
    )
    return out


def _evidence(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("subcategory-evidence.json"))


def test_covered_selectors_match_mapping_verdicts() -> None:
    rows = parse_mapping_rows(MAPPING.read_text(encoding="utf-8"))
    covered = {row["id"] for row in rows if row["verdict"] == "Covered"}
    assert covered == set(COVERED_EVIDENCE_SELECTORS)


def test_covered_rows_resolve_to_chain_entries(tmp_path: Path) -> None:
    """Plumbing only: each entry is built from its row's own token, so it must match.

    Whether the tokens match what the chain really emits is
    ``test_covered_row_evidences_from_real_producer_events``.
    """
    agent_id = "agent:worker-1"
    kid = f"{agent_id}-kid"
    base = _date_to_ns("2026-04-02")
    entries = [
        _make_entry(
            path=f".sdd/controls/{tokens[0]}/record.json",
            content=sub_id,
            agent_id=agent_id,
            kid=kid,
            ts_ns=base + index,
        )
        for index, (sub_id, tokens) in enumerate(sorted(COVERED_EVIDENCE_SELECTORS.items()))
    ]
    layout = _write_lineage(tmp_path, entries)
    out = _build(tmp_path, layout, "ai-rmf.zip")
    doc = _evidence(out)
    assert doc["kind"] == PACK_KIND_AI_RMF
    assert doc["window_claim"] == "evidenced"
    by_id = {row["id"]: row for row in doc["rows"]}
    for sub_id in COVERED_EVIDENCE_SELECTORS:
        assert by_id[sub_id]["verdict"] == "Covered"
        assert by_id[sub_id]["evidenced"] is True
        assert by_id[sub_id]["chain_entry_hashes"]
        assert by_id[sub_id]["genai_profile_ref"] == sub_id
    for verdict in ("Partial", "Not-covered"):
        rows = [row for row in doc["rows"] if row["verdict"] == verdict]
        assert rows, verdict
        assert all(row["chain_entry_hashes"] == [] and row["evidenced"] is False for row in rows)
    assert doc["mapping_sha256"] == hashlib.sha256(MAPPING.read_bytes()).hexdigest()


def test_one_entry_evidences_every_row_sharing_its_token(tmp_path: Path) -> None:
    agent_id = "agent:worker-1"
    entries = [
        _make_entry(
            path=".sdd/controls/quarantine/q-1.json",
            content="quarantine",
            agent_id=agent_id,
            kid=f"{agent_id}-kid",
            ts_ns=_date_to_ns("2026-04-02"),
        )
    ]
    doc = _evidence(_build(tmp_path, _write_lineage(tmp_path, entries), "overlap.zip"))
    evidenced = {row["id"] for row in doc["rows"] if row["evidenced"]}
    assert evidenced == {"GOVERN-4.3", "MANAGE-2.3", "MANAGE-2.4"}


#: Covered rows that no in-tree lineage producer can evidence yet. Each one is
#: a strict xfail: once a producer emits an entry the row's tokens match, the
#: test passes, strict turns that into a failure, and the row leaves this map.
NO_LINEAGE_PRODUCER: dict[str, str] = {
    "GOVERN-3.2": "approval.py, approval/gate.py and plan_approval.py do not write to the lineage log",
    "GOVERN-4.3": "security_incident_response.py, denial_tracker.py and quarantine.py do not write to the lineage log",
    "GOVERN-6.1": "sigstore_attestation.py, sbom.py and endpoints/certify.py do not write to the lineage log",
    "MAP-2.1": "model_routing.py and persistence/lineage.py do not write to the lineage log",
    "MAP-3.5": "approval.py, dual_approval.py, plan_approval.py and auto_approve.py do not write to the lineage log",
    "MAP-4.2": (
        "capability_matrix.py, data_residency.py, dlp_scanner_v2.py and input_refusal.py "
        "do not write to the lineage log"
    ),
    "MEASURE-2.4": "audit_chain.py, cost_tracker.py and denial_tracker.py do not write to the lineage log",
    "MEASURE-2.7": (
        "socket_guard.py, state_encryption.py, owasp_asi_detectors.py and sbom.py do not write to the lineage log"
    ),
    "MEASURE-2.8": "audit.py, audit_dsse.py and article12_bundle.py do not write to the lineage log",
    "MEASURE-2.10": "dlp_scanner_v2.py, pii_output_gate.py and data_residency.py do not write to the lineage log",
    "MEASURE-3.1": (
        "denial_tracker.py, security_incident_response.py and audit_chain.py do not write to the lineage log"
    ),
    "MANAGE-1.3": "security_incident_response.py does not write to the lineage log",
    "MANAGE-2.3": "security_incident_response.py and quarantine.py do not write to the lineage log",
    "MANAGE-2.4": "quarantine.py and role_adapter_policy.py do not write to the lineage log",
    "MANAGE-3.1": "endpoints/certify.py, sigstore_attestation.py and data_residency.py do not write to the lineage log",
    "MANAGE-4.3": (
        "security_incident_response.py, denial_tracker.py and audit_chain.py do not write to the lineage log"
    ),
}


@pytest.fixture(scope="module")
def real_producer_rows(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    """Rows of a pack built from one entry per in-tree lineage producer.

    Producers whose entry point needs no wider setup are called directly; the
    rest are recorded the way they record themselves, through the recorder
    with their own path helper and artefact kind.
    """
    root = tmp_path_factory.mktemp("ai-rmf-producers")
    lineage_dir = root / "lineage"
    cards_dir = root / "agents"
    cards_dir.mkdir()
    priv_pem, pub_pem = generate_keypair()
    card = AgentCard(agent_id="agent:worker-1", kid="agent:worker-1-kid", public_key_pem=pub_pem)
    (cards_dir / "agent_worker-1.json").write_text(
        json.dumps({"agent_id": card.agent_id, "kid": card.kid, "public_key_pem": pub_pem}, sort_keys=True),
        encoding="utf-8",
    )
    hmac_key = b"0" * 64
    recorder = SignedLineageLog(store=LineageStore(lineage_dir), operator_hmac_key=hmac_key)
    signer = {"agent_id": card.agent_id, "agent_card": card, "private_key_pem": priv_pem}

    record_tool_result(
        recorder,
        tool_name="web_fetch",
        result_bytes=b"<html></html>",
        trust_class=TrustClass.PUBLIC,
        tool_call_id="tc-provenance",
        span_id="0" * 16,
        **signer,
    )
    anchor_coverage_record(
        recorder, tool_name="web_fetch", tool_call_id="tc-coverage", coverage={"covered": True}, **signer
    )
    anchor_admission_receipt_in_lineage(
        {"adapter": "claude", "verdict": "admit"}, store=recorder.store, operator_hmac_key=hmac_key, **signer
    )
    recorded = [
        (receipt_artefact_path("sha256:" + "1" * 64), PAYMENT_RECEIPT_KIND),
        (datasource_artefact_path("warehouse", "sha256:" + "2" * 64, "sha256:" + "3" * 64), "query-result"),
        (computer_use_artefact_path("run-1", 0), "tool-result"),
        *(
            (artifact_entry_path(f"task-{kind.value}"), kind.value)
            for kind in ArtifactKind
            if kind is not ArtifactKind.CODE_DIFF
        ),
        ("src/app/main.py", "file"),
    ]
    for path, kind in recorded:
        recorder.record_write(
            artefact_path=path,
            new_content=path.encode(),
            tool_call_id=f"tc-{path}",
            span_id="0" * 16,
            artefact_kind=kind,
            **signer,
        )

    today = datetime.now(UTC).date()
    out = root / "ai-rmf.zip"
    build_ai_rmf_pack(
        since=today - timedelta(days=1),
        until=today + timedelta(days=1),
        org="Acme",
        lineage_dir=lineage_dir,
        agent_cards_dir=cards_dir,
        mapping_path=MAPPING,
        output_path=out,
        operator_key_path=_operator_key(root),
    )
    doc = _evidence(out)
    assert doc["entry_count"] == 3 + len(recorded)
    return {row["id"]: row for row in doc["rows"]}


def test_no_lineage_producer_names_only_covered_rows() -> None:
    assert set(NO_LINEAGE_PRODUCER) <= set(COVERED_EVIDENCE_SELECTORS)


def test_real_producer_pack_carries_every_covered_row(real_producer_rows: dict[str, dict]) -> None:
    """Not an xfail, so a producer or fixture break cannot hide behind the ones below."""
    assert set(COVERED_EVIDENCE_SELECTORS) <= set(real_producer_rows)


@pytest.mark.parametrize(
    "sub_id",
    [
        pytest.param(
            sub_id,
            marks=pytest.mark.xfail(strict=True, raises=AssertionError, reason=NO_LINEAGE_PRODUCER[sub_id]),
        )
        if sub_id in NO_LINEAGE_PRODUCER
        else sub_id
        for sub_id in sorted(COVERED_EVIDENCE_SELECTORS)
    ],
)
def test_covered_row_evidences_from_real_producer_events(sub_id: str, real_producer_rows: dict[str, dict]) -> None:
    assert real_producer_rows[sub_id]["evidenced"] is True


def test_two_builds_share_member_hashes(tmp_path: Path) -> None:
    agent_id = "agent:worker-1"
    kid = f"{agent_id}-kid"
    entries = [
        _make_entry(
            path=".sdd/controls/approval/record.json",
            content="approval",
            agent_id=agent_id,
            kid=kid,
            ts_ns=_date_to_ns("2026-04-02"),
        )
    ]
    layout = _write_lineage(tmp_path, entries)
    key = _operator_key(tmp_path)

    def _hashes(name: str) -> dict[str, str]:
        out = tmp_path / name
        build_ai_rmf_pack(
            since=date(2026, 1, 1),
            until=date(2026, 6, 30),
            org="Acme",
            lineage_dir=layout["lineage_dir"],
            agent_cards_dir=layout["agent_cards_dir"],
            mapping_path=MAPPING,
            output_path=out,
            operator_key_path=key,
        )
        with zipfile.ZipFile(out) as zf:
            manifest = json.loads(zf.read("pack-manifest.json"))
        return manifest["input_hashes"]

    assert _hashes("a.zip") == _hashes("b.zip")


def test_empty_window_is_valid_and_makes_no_claim(tmp_path: Path) -> None:
    lineage = tmp_path / "lineage"
    cards = tmp_path / "agents"
    lineage.mkdir()
    cards.mkdir()
    out = tmp_path / "empty.zip"
    build_ai_rmf_pack(
        since=date(2026, 1, 1),
        until=date(2026, 1, 2),
        org="Acme",
        lineage_dir=lineage,
        agent_cards_dir=cards,
        mapping_path=MAPPING,
        output_path=out,
        operator_key_path=_operator_key(tmp_path),
    )
    doc = _evidence(out)
    assert doc["window_claim"] == "empty"
    assert doc["entry_count"] == 0
    assert all(row["evidenced"] is False and row["chain_entry_hashes"] == [] for row in doc["rows"])


def test_unmatched_entries_do_not_claim_coverage(tmp_path: Path) -> None:
    agent_id = "agent:worker-1"
    kid = f"{agent_id}-kid"
    entries = [
        _make_entry(
            path="src/readme.md",
            content="notes",
            agent_id=agent_id,
            kid=kid,
            ts_ns=_date_to_ns("2026-04-02"),
        )
    ]
    layout = _write_lineage(tmp_path, entries)
    doc = _evidence(_build(tmp_path, layout, "unmatched.zip"))
    assert doc["window_claim"] == "unmatched"
    assert doc["entry_count"] == 1
    assert all(row["evidenced"] is False for row in doc["rows"])
