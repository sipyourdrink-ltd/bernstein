"""CLI wiring tests for `bernstein compliance pack <kind>`.

Covers the default-subcommand fallback (legacy ``pack --since ...`` still
reaches ``article-12``) and the three regulator-mapped subcommands, each
driven from a seeded workspace with no manual file selection.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.cli.commands.compliance_cmd import compliance_group
from bernstein.core.lineage.entry import LineageEntry, canonicalise, entry_hash
from bernstein.core.lineage.identity import AgentCard, generate_keypair, sign_detached


def _date_to_ns(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1_000_000_000)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    sdd = tmp_path / ".sdd"
    lineage = sdd / "lineage"
    signatures = lineage / "signatures"
    agents = sdd / "agents"
    keys = sdd / "keys"
    for d in (lineage, signatures, agents, keys):
        d.mkdir(parents=True)

    priv = Ed25519PrivateKey.generate()
    (keys / "operator.key").write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    a_priv, a_pub = generate_keypair()
    agent_id = "agent:worker-1"
    kid = f"{agent_id}-kid"
    (agents / f"{agent_id.replace(':', '_')}.json").write_text(
        json.dumps(asdict(AgentCard(agent_id=agent_id, kid=kid, public_key_pem=a_pub)), sort_keys=True)
    )
    entry = LineageEntry(
        v=1,
        artefact_path="src/a.py",
        artefact_kind="file",
        content_hash="sha256:" + hashlib.sha256(b"a").hexdigest(),
        parent_hashes=[],
        agent_id=agent_id,
        agent_card_kid=kid,
        tool_call_id="tc-1",
        span_id="0" * 16,
        ts_ns=_date_to_ns("2026-03-01"),
        operator_hmac="deadbeef",
    )
    (lineage / "log.jsonl").write_text(json.dumps(asdict(entry), sort_keys=True) + "\n")
    h = entry_hash(entry)
    (signatures / f"{h.split(':', 1)[1]}.jws").write_text(sign_detached(canonicalise(entry), a_priv, kid=kid))

    # Resolved approvals for the oversight pack.
    (sdd / "approvals").mkdir()
    (sdd / "approvals" / "resolved.jsonl").write_text(
        json.dumps(
            {
                "receipt_id": "ap-1",
                "principal": "alice@acme.example",
                "decision": "allow",
                "ts_ns": _date_to_ns("2026-03-05"),
                "displayed": {"tool": "shell", "args": {"command": "ls"}},
                "executed": {"tool": "shell", "args": {"command": "ls"}},
            }
        )
        + "\n"
    )

    # Incident directory with a timeline referencing one present + one missing ref.
    idir = sdd / "incidents" / "run-42"
    idir.mkdir(parents=True)
    (idir / "timeline.json").write_text(
        json.dumps(
            {
                "run_id": "run-42",
                "opened_ts_ns": _date_to_ns("2026-03-10"),
                "events": [{"ts_ns": _date_to_ns("2026-03-10"), "kind": "detected", "detail": "spike"}],
                "involved_agents": [agent_id],
                "artifacts": ["src/a.py"],
                "evidence_bundle_refs": ["task-present", "task-missing"],
                "receipt_refs": ["ap-1"],
            }
        )
    )
    (idir / "audit-slice.jsonl").write_text(
        json.dumps({"seq": 0, "prev_hmac": "", "hmac": "aaaa", "event": "start"}, sort_keys=True) + "\n"
    )
    (sdd / "evidence").mkdir()
    (sdd / "evidence" / "task-present.json").write_text('{"bundle":"present"}')
    (sdd / "approvals" / "ap-1.json").write_text('{"receipt":"ap-1"}')
    return tmp_path


def _run(args: list[str]) -> object:
    return CliRunner().invoke(compliance_group, args)


def test_legacy_pack_defaults_to_article12(workspace: Path) -> None:
    out = workspace / "a12.zip"
    result = _run(
        [
            "pack",
            "--since",
            "2026-01-01",
            "--until",
            "2026-06-30",
            "--org",
            "Acme",
            "--output",
            str(out),
            "--workdir",
            str(workspace),
        ]
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        assert "article12-evidence.pdf" in zf.namelist()


def test_pack_retention_subcommand(workspace: Path) -> None:
    out = workspace / "retention.zip"
    result = _run(
        [
            "pack",
            "retention",
            "--since",
            "2026-01-01",
            "--until",
            "2026-06-30",
            "--org",
            "Acme",
            "--output",
            str(out),
            "--workdir",
            str(workspace),
        ]
    )
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as zf:
        assert json.loads(zf.read("pack-manifest.json"))["kind"] == "retention"


def test_pack_oversight_subcommand(workspace: Path) -> None:
    out = workspace / "oversight.zip"
    result = _run(
        [
            "pack",
            "oversight",
            "--since",
            "2026-01-01",
            "--until",
            "2026-06-30",
            "--org",
            "Acme",
            "--output",
            str(out),
            "--workdir",
            str(workspace),
        ]
    )
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("pack-manifest.json"))
    assert manifest["kind"] == "oversight"
    assert manifest["receipt_count"] == 1


def test_pack_incident_subcommand_records_gap(workspace: Path) -> None:
    out = workspace / "incident.zip"
    result = _run(
        ["pack", "incident", "--run", "run-42", "--org", "Acme", "--output", str(out), "--workdir", str(workspace)]
    )
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as zf:
        manifest = json.loads(zf.read("pack-manifest.json"))
        gaps = json.loads(zf.read("gaps.json"))
    assert manifest["kind"] == "incident"
    # task-present resolved, task-missing recorded as a gap.
    assert manifest["gap_count"] == 1
    assert gaps["gaps"][0]["ref"] == "task-missing"
