"""Tests for the regulator-mapped compliance pack family.

Covers the three pack kinds added on top of the Article 12 bundle:

* retention  - chain-continuity evidence for a window.
* incident   - incident timeline joined with audit slice, evidence
               bundles, and receipts, with explicit gap entries.
* oversight  - approval receipts with attested displayed-vs-executed
               bindings.

Each pack is a deterministic projection of the chain sealed with the same
operator-key signing and signed provenance manifest as the Article 12 pack
(PACK_FORMAT_VERSION 2 canonical-bytes rule).
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import asdict
from datetime import date
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.compliance.pack import (
    PACK_KIND_INCIDENT,
    PACK_KIND_OVERSIGHT,
    PACK_KIND_RETENTION,
    build_incident_pack,
    build_oversight_pack,
    build_retention_pack,
)
from bernstein.core.lineage.entry import LineageEntry, canonicalise, entry_hash
from bernstein.core.lineage.identity import (
    AgentCard,
    generate_keypair,
    sign_detached,
    verify_detached,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _date_to_ns(d: str) -> int:
    from datetime import UTC, datetime

    dt = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000_000_000)


def _canon(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _operator_key(tmp_path: Path) -> tuple[Path, str]:
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "operator.key"
    key_path.write_bytes(pem)
    pub_pem = (
        priv.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return key_path, pub_pem


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


@pytest.fixture
def lineage_layout(tmp_path: Path) -> dict[str, Path]:
    """A .sdd/lineage/ dir with two in-window and one out-of-window signed entry."""
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
        json.dumps(asdict(card), sort_keys=True),
        encoding="utf-8",
    )

    entries = [
        _make_entry(path="src/a.py", content="a", agent_id=agent_id, kid=kid, ts_ns=_date_to_ns("2026-03-01")),
        _make_entry(path="src/b.py", content="b", agent_id=agent_id, kid=kid, ts_ns=_date_to_ns("2026-03-20")),
        _make_entry(path="src/old.py", content="old", agent_id=agent_id, kid=kid, ts_ns=_date_to_ns("2025-12-31")),
    ]
    log_path = lineage_dir / "log.jsonl"
    with log_path.open("w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(asdict(entry), sort_keys=True) + "\n")

    for entry in entries:
        canonical = canonicalise(entry)
        h = entry_hash(entry)
        jws = sign_detached(canonical, priv_pem, kid=kid)
        (signatures_dir / f"{h.split(':', 1)[1]}.jws").write_text(jws, encoding="utf-8")

    return {"lineage_dir": lineage_dir, "agent_cards_dir": agent_cards_dir, "log_path": log_path}


def _read_manifest(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("pack-manifest.json"))


def _assert_manifest_sealed(zip_path: Path, pub_pem: str, *, kind: str) -> dict:
    """Every pack manifest: kind field, v2, self-anchored output_hash, signed."""
    with zipfile.ZipFile(zip_path) as zf:
        manifest_bytes = zf.read("pack-manifest.json")
        sig = zf.read("pack-manifest.json.sig").decode("ascii")
    manifest = json.loads(manifest_bytes)
    assert manifest["kind"] == kind
    assert manifest["pack_format_version"] == 2
    assert manifest["output_hash"].startswith("sha256:")
    # output_hash self-anchors the manifest body.
    body = {k: v for k, v in manifest.items() if k != "output_hash"}
    assert manifest["output_hash"] == _sha256(_canon(body))
    # operator signature over the full manifest verifies.
    card = AgentCard(agent_id="operator", kid=manifest["operator_kid"], public_key_pem=pub_pem)
    assert verify_detached(manifest_bytes, sig, card)
    return manifest


# ---------------------------------------------------------------------------
# Retention pack
# ---------------------------------------------------------------------------


class TestRetentionPack:
    def test_builds_and_seals(self, tmp_path: Path, lineage_layout: dict[str, Path]) -> None:
        key_path, pub_pem = _operator_key(tmp_path)
        out = tmp_path / "retention.zip"
        result = build_retention_pack(
            since=date(2026, 1, 1),
            until=date(2026, 6, 30),
            org="Acme",
            lineage_dir=lineage_layout["lineage_dir"],
            agent_cards_dir=lineage_layout["agent_cards_dir"],
            output_path=out,
            operator_key_path=key_path,
        )
        assert result == out
        manifest = _assert_manifest_sealed(out, pub_pem, kind=PACK_KIND_RETENTION)
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        assert {
            "retention-evidence.json",
            "retention-evidence.pdf",
            "retention-evidence.csv",
            "lineage-log.jsonl",
        } <= names
        assert manifest["entry_count"] == 2  # two in-window entries

    def test_boundary_head_hashes_recomputable(self, tmp_path: Path, lineage_layout: dict[str, Path]) -> None:
        """Boundary head hashes in the evidence == entry_hash of the actual signed entries."""
        key_path, _ = _operator_key(tmp_path)
        out = tmp_path / "retention.zip"
        build_retention_pack(
            since=date(2026, 1, 1),
            until=date(2026, 6, 30),
            org="Acme",
            lineage_dir=lineage_layout["lineage_dir"],
            agent_cards_dir=lineage_layout["agent_cards_dir"],
            output_path=out,
            operator_key_path=key_path,
        )
        with zipfile.ZipFile(out) as zf:
            evidence = json.loads(zf.read("retention-evidence.json"))
            log_lines = [json.loads(ln) for ln in zf.read("lineage-log.jsonl").decode().split("\n") if ln]
        entries = [LineageEntry(**rec) for rec in log_lines]
        ordered = sorted(entries, key=lambda e: (e.ts_ns, entry_hash(e)))
        assert evidence["boundary"]["first_entry_hash"] == entry_hash(ordered[0])
        assert evidence["boundary"]["last_entry_hash"] == entry_hash(ordered[-1])
        assert evidence["entry_count"] == len(entries)

    def test_deterministic_member_hashes(self, tmp_path: Path, lineage_layout: dict[str, Path]) -> None:
        """Two builds over the same window -> byte-identical member hashes."""
        key_path, _ = _operator_key(tmp_path)
        h1 = _build_and_input_hashes(build_retention_pack, tmp_path / "r1.zip", key_path, lineage_layout)
        h2 = _build_and_input_hashes(build_retention_pack, tmp_path / "r2.zip", key_path, lineage_layout)
        assert h1 == h2, "member content hashes must be byte-identical across builds"


def _build_and_input_hashes(fn, out: Path, key_path: Path, layout: dict[str, Path]) -> dict[str, str]:
    fn(
        since=date(2026, 1, 1),
        until=date(2026, 6, 30),
        org="Acme",
        lineage_dir=layout["lineage_dir"],
        agent_cards_dir=layout["agent_cards_dir"],
        output_path=out,
        operator_key_path=key_path,
    )
    return _read_manifest(out)["input_hashes"]


# ---------------------------------------------------------------------------
# Oversight pack
# ---------------------------------------------------------------------------


def _approval_records() -> list[dict]:
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


class TestOversightPack:
    def test_builds_and_seals(self, tmp_path: Path) -> None:
        key_path, pub_pem = _operator_key(tmp_path)
        out = tmp_path / "oversight.zip"
        result = build_oversight_pack(
            since=date(2026, 1, 1),
            until=date(2026, 6, 30),
            org="Acme",
            approvals=_approval_records(),
            output_path=out,
            operator_key_path=key_path,
        )
        assert result == out
        manifest = _assert_manifest_sealed(out, pub_pem, kind=PACK_KIND_OVERSIGHT)
        assert manifest["receipt_count"] == 2
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
            evidence = json.loads(zf.read("oversight-evidence.json"))
        assert any(n.startswith("receipts/") and n.endswith(".json") for n in names)
        assert {"oversight-evidence.json", "oversight-report.pdf", "oversight-report.csv"} <= names
        # every row carries the displayed-vs-executed binding.
        for row in evidence["receipts"]:
            assert "displayed_hash" in row and "executed_hash" in row
            assert row["binding_ok"] == (row["displayed_hash"] == row["executed_hash"])

    def test_displayed_executed_binding_recomputable(self, tmp_path: Path) -> None:
        key_path, _ = _operator_key(tmp_path)
        out = tmp_path / "oversight.zip"
        build_oversight_pack(
            since=date(2026, 1, 1),
            until=date(2026, 6, 30),
            org="Acme",
            approvals=_approval_records(),
            output_path=out,
            operator_key_path=key_path,
        )
        with zipfile.ZipFile(out) as zf:
            names = [n for n in zf.namelist() if n.startswith("receipts/") and n.endswith(".json")]
            for name in names:
                receipt = json.loads(zf.read(name))
                assert receipt["displayed_hash"] == _sha256(_canon(receipt["displayed"]))
                assert receipt["executed_hash"] == _sha256(_canon(receipt["executed"]))

    def test_window_filters_receipts(self, tmp_path: Path) -> None:
        key_path, _ = _operator_key(tmp_path)
        out = tmp_path / "oversight.zip"
        records = _approval_records()
        records.append(
            {
                "receipt_id": "ap-old",
                "principal": "carol@acme.example",
                "decision": "allow",
                "ts_ns": _date_to_ns("2020-01-01"),
                "displayed": {"tool": "noop", "args": {}},
                "executed": {"tool": "noop", "args": {}},
            }
        )
        build_oversight_pack(
            since=date(2026, 1, 1),
            until=date(2026, 6, 30),
            org="Acme",
            approvals=records,
            output_path=out,
            operator_key_path=key_path,
        )
        assert _read_manifest(out)["receipt_count"] == 2


# ---------------------------------------------------------------------------
# Incident pack
# ---------------------------------------------------------------------------


def _incident_inputs() -> dict:
    timeline = {
        "run_id": "run-42",
        "opened_ts_ns": _date_to_ns("2026-03-10"),
        "events": [
            {"ts_ns": _date_to_ns("2026-03-10"), "kind": "detected", "detail": "error rate spike"},
            {"ts_ns": _date_to_ns("2026-03-11"), "kind": "mitigated", "detail": "rollback"},
        ],
        "involved_agents": ["agent:worker-1"],
        "artifacts": ["src/a.py"],
    }
    audit_events = [
        {"seq": 0, "prev_hmac": "", "hmac": "aaaa", "event": "task_start"},
        {"seq": 1, "prev_hmac": "aaaa", "hmac": "bbbb", "event": "tool_call"},
        {"seq": 2, "prev_hmac": "bbbb", "hmac": "cccc", "event": "task_end"},
    ]
    return {"timeline": timeline, "audit_events": audit_events}


class TestIncidentPack:
    def test_builds_and_seals(self, tmp_path: Path) -> None:
        key_path, pub_pem = _operator_key(tmp_path)
        out = tmp_path / "incident.zip"
        inp = _incident_inputs()
        result = build_incident_pack(
            run_id="run-42",
            org="Acme",
            timeline=inp["timeline"],
            audit_events=inp["audit_events"],
            evidence_bundles={"task-1.json": b'{"bundle":"task-1"}'},
            receipts={"ap-9.json": b'{"receipt":"ap-9"}'},
            gaps=[],
            output_path=out,
            operator_key_path=key_path,
        )
        assert result == out
        manifest = _assert_manifest_sealed(out, pub_pem, kind=PACK_KIND_INCIDENT)
        assert manifest["run_id"] == "run-42"
        with zipfile.ZipFile(out) as zf:
            names = set(zf.namelist())
        assert {"incident-timeline.json", "audit-slice.jsonl", "gaps.json", "incident-report.pdf"} <= names
        assert any(n.startswith("evidence-bundles/") for n in names)
        assert any(n.startswith("receipts/") for n in names)

    def test_missing_reference_recorded_as_gap(self, tmp_path: Path) -> None:
        """A referenced bundle/receipt missing from the store becomes an explicit gap."""
        key_path, _ = _operator_key(tmp_path)
        out = tmp_path / "incident.zip"
        inp = _incident_inputs()
        gaps = [
            {"kind": "evidence_bundle", "ref": "task-77", "reason": "missing_from_store"},
            {"kind": "receipt", "ref": "ap-55", "reason": "missing_from_store"},
        ]
        build_incident_pack(
            run_id="run-42",
            org="Acme",
            timeline=inp["timeline"],
            audit_events=inp["audit_events"],
            evidence_bundles={},
            receipts={},
            gaps=gaps,
            output_path=out,
            operator_key_path=key_path,
        )
        with zipfile.ZipFile(out) as zf:
            gaps_doc = json.loads(zf.read("gaps.json"))
        manifest = _read_manifest(out)
        assert manifest["gap_count"] == 2
        assert len(gaps_doc["gaps"]) == 2
        refs = {g["ref"] for g in gaps_doc["gaps"]}
        assert refs == {"task-77", "ap-55"}

    def test_audit_slice_is_canonical_jsonl(self, tmp_path: Path) -> None:
        key_path, _ = _operator_key(tmp_path)
        out = tmp_path / "incident.zip"
        inp = _incident_inputs()
        build_incident_pack(
            run_id="run-42",
            org="Acme",
            timeline=inp["timeline"],
            audit_events=inp["audit_events"],
            evidence_bundles={},
            receipts={},
            gaps=[],
            output_path=out,
            operator_key_path=key_path,
        )
        with zipfile.ZipFile(out) as zf:
            slice_bytes = zf.read("audit-slice.jsonl")
        assert slice_bytes.endswith(b"\n")
        for raw in [ln for ln in slice_bytes.split(b"\n") if ln]:
            obj = json.loads(raw)
            assert _canon(obj) == raw, "audit-slice lines must be byte-canonical"
