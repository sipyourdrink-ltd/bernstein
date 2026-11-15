"""Pinned MCP taxonomy and real signing-gate evidence in the existing CoSAI pack."""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.compliance.evidence_pack import build_evidence_pack, get_standard_map
from bernstein.core.protocols.mcp.mcp_signing_policy import MCPSigningPolicy, enforce_mcp_server_load
from bernstein.core.protocols.mcp.mcp_verifier import MCPVerificationError, canonicalize_manifest, parse_manifest
from bernstein.core.security.audit_chain import AuditChainStore

if TYPE_CHECKING:
    from pathlib import Path


# Section 6.2 of the owner-selected source revision, not its differently
# worded summary table or the latest upstream document.
_PINNED_THREATS = {
    "MCP-T1": "Improper Authentication and Identity Management",
    "MCP-T2": "Missing or Improper Access Control",
    "MCP-T3": "Input Validation/Sanitization Failures",
    "MCP-T4": "Input/Instruction Boundary Distinction Failure",
    "MCP-T5": "Inadequate Data Protection and Confidentiality Controls",
    "MCP-T6": "Missing Integrity/Verification Controls",
    "MCP-T7": "Session and Transport Security Failures",
    "MCP-T8": "Network Binding/Isolation Failures",
    "MCP-T9": "Trust Boundary and Privilege Design Failures",
    "MCP-T10": "Resource Management/Rate Limiting Absence",
    "MCP-T11": "Supply Chain and Lifecycle Security Failures",
    "MCP-T12": "Insufficient Logging, Monitoring, and Auditability",
}
_PROVENANCE = (
    "Model Context Protocol (MCP) Security",
    "7ec1306f2f55563f6eeef9d36a6bb2b531491ceb",
    "2026-01-08",
    "section 6.2",
)
_GATE_EVENTS = {"mcp.signature_verification", "mcp.bundle_scan", "mcp.load_decision"}


def _assert_catalogue(controls: list[dict]) -> list[dict]:
    rows = [row for row in controls if row["control_id"].startswith("MCP-")]
    assert [row["control_id"] for row in rows] == list(_PINNED_THREATS)
    for row in rows:
        assert _PINNED_THREATS[row["control_id"]] in row["requirement"]
        assert all(value in row["requirement"] for value in _PROVENANCE)
        # These broad threat classes exceed the pre-load gate's coverage.
        assert row["status"] in {"partial", "todo"}
        if row["status"] == "partial":
            assert row["artefact"] == "audit-chain/events.jsonl"
            assert set(row["selector"].split(",")) <= _GATE_EVENTS
            assert row["selector"]
        else:
            assert row["selector"] == row["artefact"] == "n/a"
    return rows


def test_mcp_catalogue_uses_exact_pinned_names_ids_and_provenance() -> None:
    _assert_catalogue(get_standard_map("cosai")["controls"])


def test_existing_thirteen_principle_rows_remain_byte_identical() -> None:
    original = [row for row in get_standard_map("cosai")["controls"] if not row["control_id"].startswith("MCP-")]
    assert len(original) == 13
    encoded = json.dumps(original, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == "ec7e2ff606d34a468a2c7f77d78e1f7f65237cb4a0e8c2ee919cbd612268374a"


@pytest.mark.parametrize("defect", ["duplicate", "missing", "extra", "alias", "revision", "date", "title", "class"])
def test_catalogue_guard_rejects_identifier_and_source_drift(defect: str) -> None:
    rows = deepcopy(get_standard_map("cosai")["controls"])
    _assert_catalogue(rows)
    target = next(row for row in rows if row["control_id"] == "MCP-T1")
    if defect == "duplicate":
        rows.append(target.copy())
    elif defect == "missing":
        rows.remove(target)
    elif defect == "extra":
        rows.append({**target, "control_id": "MCP-T13"})
    elif defect == "alias":
        target["control_id"] = "MCP-T01"
    else:
        old = {
            "revision": _PROVENANCE[1],
            "date": _PROVENANCE[2],
            "title": _PROVENANCE[0],
            "class": _PINNED_THREATS["MCP-T1"],
        }[defect]
        target["requirement"] = target["requirement"].replace(old, "unbound")
    with pytest.raises(AssertionError):
        _assert_catalogue(rows)


def _record_signed_server(chain: AuditChainStore, *, critical: bool) -> None:
    name = "scan-refused" if critical else "signed-clean"
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fingerprint = f"ed25519/{name}"
    source = "subprocess.run(command, shell=True)\n" if critical else "result = 1\n"
    bundle = source.encode()
    manifest = json.dumps(
        {
            "name": name,
            "version": "1.0.0",
            "publisher": {"name": "fixture", "fingerprint": fingerprint},
            "content_hash": "sha256/" + hashlib.sha256(bundle).hexdigest(),
        }
    )
    signature = base64.b64encode(private.sign(canonicalize_manifest(parse_manifest(manifest)))).decode()
    kwargs = dict(
        server_name=name,
        manifest_yaml=manifest,
        signature_b64=signature,
        bundle_files={"tool.py": source},
        bundle_bytes=bundle,
        policy=MCPSigningPolicy(trusted_publishers=frozenset({fingerprint}), publisher_keys={fingerprint: public}),
        audit_chain=chain,
    )
    if critical:
        with pytest.raises(MCPVerificationError):
            enforce_mcp_server_load(**kwargs)
    else:
        assert enforce_mcp_server_load(**kwargs).allowed


def test_exported_mcp_rows_cite_real_signed_and_scan_refused_gate_events(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    chain = AuditChainStore(sdd / "audit", key=b"synthetic-fixture-key" * 2)
    _record_signed_server(chain, critical=False)
    _record_signed_server(chain, critical=True)
    assert chain.verify() == (True, [])
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    build_evidence_pack(sdd_dir=sdd, standard="cosai", output_path=first)
    build_evidence_pack(sdd_dir=sdd, standard="cosai", output_path=second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        controls = json.loads(archive.read("controls.json"))
        assert controls["standard"] == "cosai"
        rows = _assert_catalogue(controls["controls"])
        events = [json.loads(line) for line in archive.read("audit-chain/events.jsonl").splitlines()]
        assert len(events) == 6
        for name, allowed in [("signed-clean", True), ("scan-refused", False)]:
            recorded = [event for event in events if event["resource_id"] == name]
            assert {event["event_type"] for event in recorded} == _GATE_EVENTS
            decision = next(event for event in recorded if event["event_type"] == "mcp.load_decision")
            assert decision["details"]["allowed"] is allowed
            scan = next(event for event in recorded if event["event_type"] == "mcp.bundle_scan")
            assert bool(scan["details"]["findings"]) is (not allowed)
        for row in rows:
            if row["status"] != "todo":
                assert set(row["selector"].split(",")) <= {event["event_type"] for event in events}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == "1.0.0"
