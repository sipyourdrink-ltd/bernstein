"""Offline-verification tests for regulator-mapped compliance packs.

Builds each pack kind with bernstein (test scope) then verifies it with the
standalone `bernstein_verify` re-implementation - proving the packs re-verify
offline against the chain and that a one-byte tamper in ANY member (including
a rendered PDF) fails verification naming the member.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

# Test-scope imports of the production builder are allowed; the package under
# test (bernstein_verify) never imports bernstein.
from bernstein.core.compliance.pack import (
    build_incident_pack,
    build_oversight_pack,
    build_retention_pack,
)
from bernstein.core.lineage.entry import LineageEntry, canonicalise, entry_hash
from bernstein.core.lineage.identity import AgentCard, generate_keypair, sign_detached
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein_verify.verify import verify_pack


def _date_to_ns(d: str) -> int:
    dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000_000_000)


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


def _lineage_layout(tmp_path: Path) -> dict[str, Path]:
    import hashlib

    lineage_dir = tmp_path / "lineage"
    signatures_dir = lineage_dir / "signatures"
    agent_cards_dir = tmp_path / "agents"
    lineage_dir.mkdir()
    signatures_dir.mkdir()
    agent_cards_dir.mkdir()

    priv_pem, pub_pem = generate_keypair()
    agent_id = "agent:worker-1"
    kid = f"{agent_id}-kid"
    card = AgentCard(agent_id=agent_id, kid=kid, public_key_pem=pub_pem)
    (agent_cards_dir / f"{agent_id.replace(':', '_')}.json").write_text(
        json.dumps(asdict(card), sort_keys=True), encoding="utf-8"
    )

    entries = []
    for i, day in enumerate(("2026-03-01", "2026-03-20")):
        entries.append(
            LineageEntry(
                v=1,
                artefact_path=f"src/f{i}.py",
                artefact_kind="file",
                content_hash="sha256:" + hashlib.sha256(str(i).encode()).hexdigest(),
                parent_hashes=[],
                agent_id=agent_id,
                agent_card_kid=kid,
                tool_call_id=f"tc-{i}",
                span_id=f"{i:016x}"[:16],
                ts_ns=_date_to_ns(day),
                operator_hmac="deadbeef",
            )
        )
    log_path = lineage_dir / "log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(asdict(entry), sort_keys=True) + "\n")
    for entry in entries:
        h = entry_hash(entry)
        jws = sign_detached(canonicalise(entry), priv_pem, kid=kid)
        (signatures_dir / f"{h.split(':', 1)[1]}.jws").write_text(jws, encoding="utf-8")
    return {"lineage_dir": lineage_dir, "agent_cards_dir": agent_cards_dir}


def _approvals() -> list[dict]:
    return [
        {
            "receipt_id": "ap-1111",
            "principal": "alice@acme.example",
            "decision": "allow",
            "ts_ns": _date_to_ns("2026-03-05"),
            "displayed": {"tool": "shell", "args": {"command": "ls -la"}},
            "executed": {"tool": "shell", "args": {"command": "ls -la"}},
        },
        {
            "receipt_id": "ap-2222",
            "principal": "bob@acme.example",
            "decision": "reject",
            "ts_ns": _date_to_ns("2026-03-06"),
            "displayed": {"tool": "http", "args": {"url": "https://x.example/y"}},
            "executed": {"tool": "http", "args": {"url": "https://x.example/y"}},
        },
    ]


def _build_retention(tmp_path: Path) -> Path:
    layout = _lineage_layout(tmp_path)
    out = tmp_path / "retention.zip"
    build_retention_pack(
        since=date(2026, 1, 1),
        until=date(2026, 6, 30),
        org="Acme",
        lineage_dir=layout["lineage_dir"],
        agent_cards_dir=layout["agent_cards_dir"],
        output_path=out,
        operator_key_path=_operator_key(tmp_path),
    )
    return out


def _build_oversight(tmp_path: Path) -> Path:
    out = tmp_path / "oversight.zip"
    build_oversight_pack(
        since=date(2026, 1, 1),
        until=date(2026, 6, 30),
        org="Acme",
        approvals=_approvals(),
        output_path=out,
        operator_key_path=_operator_key(tmp_path),
    )
    return out


def _build_incident(tmp_path: Path, *, gaps: list[dict] | None = None) -> Path:
    out = tmp_path / "incident.zip"
    build_incident_pack(
        run_id="run-42",
        org="Acme",
        timeline={
            "run_id": "run-42",
            "opened_ts_ns": _date_to_ns("2026-03-10"),
            "events": [{"ts_ns": _date_to_ns("2026-03-10"), "kind": "detected", "detail": "spike"}],
            "involved_agents": ["agent:worker-1"],
            "artifacts": ["src/f0.py"],
        },
        audit_events=[
            {"seq": 0, "prev_hmac": "", "hmac": "aaaa", "event": "start"},
            {"seq": 1, "prev_hmac": "aaaa", "hmac": "bbbb", "event": "end"},
        ],
        evidence_bundles={} if gaps else {"task-1.json": b'{"b":1}'},
        receipts={} if gaps else {"ap-9.json": b'{"r":9}'},
        gaps=gaps or [],
        output_path=out,
        operator_key_path=_operator_key(tmp_path),
    )
    return out


def _rewrite_member(zip_path: Path, member: str, new_bytes: bytes) -> Path:
    """Return a new zip with one member's bytes replaced (a one-byte tamper)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == member:
                data = new_bytes
            dst.writestr(info.filename, data)
    tampered = zip_path.with_suffix(".tampered.zip")
    tampered.write_bytes(buf.getvalue())
    return tampered


# ---------------------------------------------------------------------------
# Happy path: all three kinds verify offline.
# ---------------------------------------------------------------------------


def test_retention_pack_verifies(tmp_path):
    result = verify_pack(_build_retention(tmp_path))
    assert result.ok, result.errors
    assert result.stats["kind"] == "retention"


def test_oversight_pack_verifies(tmp_path):
    result = verify_pack(_build_oversight(tmp_path))
    assert result.ok, result.errors
    assert result.stats["kind"] == "oversight"


def test_incident_pack_verifies(tmp_path):
    result = verify_pack(_build_incident(tmp_path))
    assert result.ok, result.errors
    assert result.stats["kind"] == "incident"


# ---------------------------------------------------------------------------
# Tamper: flip one byte in any member (including a PDF) -> FAIL naming member.
# ---------------------------------------------------------------------------


def _flip_one_byte(data: bytes) -> bytes:
    mid = len(data) // 2
    return data[:mid] + bytes([data[mid] ^ 0x01]) + data[mid + 1 :]


def test_retention_pdf_tamper_fails_naming_member(tmp_path):
    pack = _build_retention(tmp_path)
    with zipfile.ZipFile(pack) as zf:
        pdf = zf.read("retention-evidence.pdf")
    tampered = _rewrite_member(pack, "retention-evidence.pdf", _flip_one_byte(pdf))
    result = verify_pack(tampered)
    assert not result.ok
    assert any("retention-evidence.pdf" in e for e in result.errors), result.errors


def test_oversight_receipt_tamper_fails(tmp_path):
    pack = _build_oversight(tmp_path)
    with zipfile.ZipFile(pack) as zf:
        member = next(n for n in zf.namelist() if n.startswith("receipts/"))
        raw = zf.read(member)
    tampered = _rewrite_member(pack, member, _flip_one_byte(raw))
    result = verify_pack(tampered)
    assert not result.ok
    assert any(member in e for e in result.errors), result.errors


def test_oversight_binding_break_fails(tmp_path):
    """Rewriting a receipt so displayed != executed but keeping a matching
    stored hash is caught by binding recomputation."""
    pack = _build_oversight(tmp_path)
    with zipfile.ZipFile(pack) as zf:
        member = next(n for n in zf.namelist() if n.startswith("receipts/"))
        receipt = json.loads(zf.read(member))
    # Change executed action but leave executed_hash stale -> recompute catches it.
    receipt["executed"] = {"tool": "shell", "args": {"command": "rm -rf /"}}
    forged = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    # Update input_hashes so the member-integrity layer passes and we exercise
    # the binding-recomputation layer specifically.
    tampered = _repack_with_manifest_hash(pack, member, forged)
    result = verify_pack(tampered)
    assert not result.ok
    assert any(member in e for e in result.errors), result.errors


def _repack_with_manifest_hash(zip_path: Path, member: str, new_bytes: bytes) -> Path:
    import hashlib

    buf = io.BytesIO()
    with zipfile.ZipFile(zip_path) as src:
        manifest = json.loads(src.read("pack-manifest.json"))
        members = {i.filename: src.read(i.filename) for i in src.infolist()}
    members[member] = new_bytes
    manifest["input_hashes"][member] = "sha256:" + hashlib.sha256(new_bytes).hexdigest()
    body = {k: v for k, v in manifest.items() if k != "output_hash"}
    manifest["output_hash"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
    )
    manifest_bytes = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    members["pack-manifest.json"] = manifest_bytes
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for name, data in members.items():
            dst.writestr(name, data)
    out = zip_path.with_suffix(".binding.zip")
    out.write_bytes(buf.getvalue())
    return out


def test_incident_gap_list_is_reported(tmp_path):
    """A pack with declared gaps verifies but surfaces the gap list (never a
    silent PASS that hides an incomplete store)."""
    gaps = [
        {"kind": "evidence_bundle", "ref": "task-77", "reason": "missing_from_store"},
        {"kind": "receipt", "ref": "ap-55", "reason": "missing_from_store"},
    ]
    result = verify_pack(_build_incident(tmp_path, gaps=gaps))
    assert result.ok, result.errors
    assert result.stats["gap_count"] == 2
    assert len(result.stats["gaps"]) == 2


def test_incident_audit_slice_tamper_fails(tmp_path):
    pack = _build_incident(tmp_path)
    with zipfile.ZipFile(pack) as zf:
        slice_bytes = zf.read("audit-slice.jsonl")
    tampered = _rewrite_member(pack, "audit-slice.jsonl", _flip_one_byte(slice_bytes))
    result = verify_pack(tampered)
    assert not result.ok
    assert any("audit-slice.jsonl" in e for e in result.errors), result.errors


def test_incident_broken_hmac_chain_fails(tmp_path):
    """A recomputed member hash cannot hide a broken prev_hmac linkage."""
    pack = _build_incident(tmp_path)
    with zipfile.ZipFile(pack) as zf:
        lines = [json.loads(ln) for ln in zf.read("audit-slice.jsonl").decode().split("\n") if ln]
    lines[1]["prev_hmac"] = "wrong"
    forged = b"".join(
        json.dumps(o, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"
        for o in lines
    )
    tampered = _repack_with_manifest_hash(pack, "audit-slice.jsonl", forged)
    result = verify_pack(tampered)
    assert not result.ok
    assert any("prev_hmac" in e or "chain" in e for e in result.errors), result.errors
