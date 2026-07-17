"""CLI tests for ``bernstein audit receipt export|verify`` (#2604)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.security.audit import AuditLog


def _write_signing_key(path: Path) -> None:
    key = Ed25519PrivateKey.from_private_bytes(b"i" * 32)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def _seed(workdir: Path, key_path: Path) -> None:
    """Seed a chain under ``workdir/.sdd/audit`` keyed by the audit key file."""
    key_path.write_bytes(b"y" * 64)
    key_path.chmod(0o600)
    audit_dir = workdir / ".sdd" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    log = AuditLog(audit_dir, key_path=key_path)
    log.log("task.created", "alice", "task", "T-1", {"role": "backend"})
    log.log("task.completed", "alice", "task", "T-1", {"status": "ok"})


def test_receipt_export_then_verify(tmp_path: Path, monkeypatch) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    audit_key = tmp_path / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(audit_key))
    _seed(workdir, audit_key)

    sign_key = tmp_path / "sign.pem"
    _write_signing_key(sign_key)
    out_dir = tmp_path / "receipts"

    runner = CliRunner()
    export = runner.invoke(
        audit_group,
        [
            "receipt",
            "export",
            "--dir",
            str(workdir),
            "--since",
            "2020-01-01T00:00:00.000000Z",
            "--until",
            "2100-01-01T00:00:00.000000Z",
            "--signing-key-path",
            str(sign_key),
            "--output",
            str(out_dir),
        ],
    )
    assert export.exit_code == 0, export.output
    assert "Audit Receipt" in export.output

    receipts = list(out_dir.glob("audit-receipt-*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert set(receipt["formats"]) == {"cose", "intoto", "transparency"}

    # The export recorded an audit.receipt_export chain event.
    events = AuditLog(workdir / ".sdd" / "audit", key_path=audit_key).query()
    assert any(e.event_type == "audit.receipt_export" for e in events)

    # verify shells to the standalone tool and passes.
    verify = runner.invoke(audit_group, ["receipt", "verify", str(receipts[0])])
    assert verify.exit_code == 0, verify.output
    assert "OVERALL: PASS" in verify.output


def test_receipt_verify_detects_tamper(tmp_path: Path, monkeypatch) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    audit_key = tmp_path / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(audit_key))
    _seed(workdir, audit_key)
    sign_key = tmp_path / "sign.pem"
    _write_signing_key(sign_key)
    out_dir = tmp_path / "receipts"

    runner = CliRunner()
    runner.invoke(
        audit_group,
        [
            "receipt",
            "export",
            "--dir",
            str(workdir),
            "--since",
            "2020-01-01T00:00:00.000000Z",
            "--until",
            "2100-01-01T00:00:00.000000Z",
            "--signing-key-path",
            str(sign_key),
            "--output",
            str(out_dir),
        ],
    )
    receipt_path = next(out_dir.glob("audit-receipt-*.json"))
    data = json.loads(receipt_path.read_text())
    data["events"][0]["actor"] = "mallory"
    receipt_path.write_text(json.dumps(data))

    verify = runner.invoke(audit_group, ["receipt", "verify", str(receipt_path)])
    assert verify.exit_code == 1
    assert "OVERALL: FAIL" in verify.output


def test_receipt_export_requires_signing_key(tmp_path: Path, monkeypatch) -> None:
    workdir = tmp_path / "proj"
    workdir.mkdir()
    audit_key = tmp_path / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(audit_key))
    _seed(workdir, audit_key)

    runner = CliRunner()
    result = runner.invoke(
        audit_group,
        [
            "receipt",
            "export",
            "--dir",
            str(workdir),
            "--since",
            "2020-01-01T00:00:00.000000Z",
            "--until",
            "2100-01-01T00:00:00.000000Z",
        ],
    )
    assert result.exit_code == 2
    assert "signing-key-path" in result.output
