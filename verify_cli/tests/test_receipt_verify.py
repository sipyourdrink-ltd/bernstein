"""Unit tests for bernstein_verify_receipt.verify.

These tests cross-import `bernstein` at TEST scope to prove byte-for-byte
compatibility of our re-implementation. The package under test
(`bernstein_verify_receipt`) never imports `bernstein` itself - see
test_no_bernstein_install_receipt.py for the install-isolation proof.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_receipt import build_receipt
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein_verify_receipt.verify import (
    CheckResult,
    VerifyResult,
    pae,
    recompute_head_sha256,
    run_verify,
    verify_cose,
    verify_intoto,
    verify_subject_binding,
    verify_transparency,
)

pytestmark = pytest.mark.slow

_HMAC_KEY = b"x" * 32
_SINCE = "2020-01-01T00:00:00.000000Z"
_UNTIL = "2100-01-01T00:00:00.000000Z"


def _build_receipt(tmp_path: Path) -> tuple[Path, dict]:
    """Build a real three-format receipt and return (receipt_path, receipt_data)."""
    audit_dir = tmp_path / ".sdd" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    log = AuditLog(audit_dir, key=_HMAC_KEY)
    log.log("task.created", "alice", "task", "T-1", {"role": "backend"})
    log.log("agent.spawned", "orchestrator", "agent", "A-1", {"task": "T-1"})
    log.log("task.completed", "alice", "task", "T-1", {"status": "ok"})

    key = Ed25519PrivateKey.from_private_bytes(b"i" * 32)
    key_path = tmp_path / "sign.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    receipt = build_receipt(
        audit_dir,
        since=_SINCE,
        until=_UNTIL,
        key=_HMAC_KEY,
        kms_adapter=FileBasedKMSAdapter(key_path, kid="e2e-receipt-key"),
        output_dir=tmp_path / "out",
        write=True,
    )
    assert receipt.receipt_path is not None
    data = json.loads(receipt.receipt_path.read_text())
    return receipt.receipt_path, data


# ---------- VerifyResult ----------


def test_verify_result_ok_all_pass():
    r = VerifyResult()
    r.checks.append(CheckResult("a", ok=True))
    r.checks.append(CheckResult("b", ok=True))
    assert r.ok is True
    assert r.errors == []
    assert r.stats == "2/2 checks passed"


def test_verify_result_ok_all_fail():
    r = VerifyResult()
    r.checks.append(CheckResult("a", ok=False, detail="fail a"))
    r.checks.append(CheckResult("b", ok=False, detail="fail b"))
    assert r.ok is False
    assert r.errors == ["fail a", "fail b"]
    assert r.stats == "0/2 checks passed"


def test_verify_result_ok_empty():
    r = VerifyResult()
    assert r.ok is True  # all() on empty is True
    assert r.errors == []


# ---------- recompute_head_sha256 ----------


def test_recompute_head_empty_events():
    # Empty events -> SHA-256 of b"" = the empty-tree hash, not empty string.
    assert recompute_head_sha256([]) != ""
    assert len(recompute_head_sha256([])) == 64  # hex SHA-256


def test_recompute_head_single_event():
    events = [{"seq": 0, "event": "start"}]
    head = recompute_head_sha256(events)
    assert isinstance(head, str)
    assert len(head) == 64  # hex SHA-256


def test_recompute_head_deterministic():
    events = [{"a": 1, "b": 2}]
    h1 = recompute_head_sha256(events)
    h2 = recompute_head_sha256(events)
    assert h1 == h2


# ---------- pae ----------


def test_pae_format():
    payload = b"hello"
    pae_bytes = pae("application/vnd.in-toto+json", payload)
    assert pae_bytes.startswith(b"DSSEv1 ")
    assert payload in pae_bytes


def test_pae_empty_payload():
    pae_bytes = pae("type", b"")
    assert b"DSSEv1 " in pae_bytes
    assert b" 0 " in pae_bytes  # length of empty payload


# ---------- verify_subject_binding ----------


def test_verify_subject_binding_missing_events():
    receipt = {"subject": {"digest": {"sha256": "abc"}}, "range": {"head_sha256": "def"}}
    check, head = verify_subject_binding(receipt)
    assert check.ok is False
    assert head == ""


def test_verify_subject_binding_tampered():
    """Tampering events should break subject binding."""
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        _path, data = _build_receipt(tmp_path)
        data["events"][0]["actor"] = "mallory"
        check, _head = verify_subject_binding(data)
        assert check.ok is False


# ---------- verify_cose ----------


def test_verify_cose_missing_format():
    receipt = {"events": [], "subject": {}, "range": {}}
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub = Ed25519PublicKey.from_public_bytes(b"\x00" * 32)
    check = verify_cose(receipt, pub, "abc")
    assert check.ok is False
    assert "no cose format block" in check.detail


# ---------- verify_intoto ----------


def test_verify_intoto_missing_format():
    receipt = {"events": [], "subject": {}, "range": {}}
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub = Ed25519PublicKey.from_public_bytes(b"\x00" * 32)
    check = verify_intoto(receipt, pub, "abc")
    assert check.ok is False
    assert "no intoto format block" in check.detail


# ---------- verify_transparency ----------


def test_verify_transparency_missing_format():
    receipt = {"events": [], "subject": {}, "range": {}}
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub = Ed25519PublicKey.from_public_bytes(b"\x00" * 32)
    check = verify_transparency(receipt, pub, "abc")
    assert check.ok is False
    assert "no transparency format block" in check.detail


# ---------- run_verify ----------


def test_run_verify_pass_path(tmp_path: Path) -> None:
    receipt_path, _ = _build_receipt(tmp_path)

    stream = io.StringIO()
    result = run_verify(
        receipt_path=receipt_path,
        which="all",
        pinned_jwk=None,
        pinned_pem=_signing_key_pem(tmp_path).read_bytes(),
        verbose=False,
        stream=stream,
    )
    assert result.ok is True
    output = stream.getvalue()
    assert "OVERALL: PASS" in output
    assert "[PASS] subject_binding" in output
    assert "[PASS] cose" in output
    assert "[PASS] intoto" in output
    assert "[PASS] transparency" in output


def test_run_verify_tampered_fails(tmp_path: Path) -> None:
    _receipt_path, data = _build_receipt(tmp_path)
    # Tamper one event
    data["events"][1]["actor"] = "mallory"
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(data))

    stream = io.StringIO()
    result = run_verify(
        receipt_path=tampered,
        which="all",
        pinned_jwk=None,
        pinned_pem=None,
        verbose=False,
        stream=stream,
    )
    assert result.ok is False
    output = stream.getvalue()
    assert "OVERALL: FAIL" in output
    assert "[FAIL] subject_binding" in output
    assert "[FAIL] cose" in output
    assert "[FAIL] intoto" in output
    assert "[FAIL] transparency" in output


def test_run_verify_missing_receipt_file(tmp_path: Path) -> None:
    stream = io.StringIO()
    result = run_verify(
        receipt_path=tmp_path / "nonexistent.json",
        which="all",
        pinned_jwk=None,
        pinned_pem=None,
        verbose=False,
        stream=stream,
    )
    assert result.ok is False
    assert any("receipt_load" in c.name for c in result.checks)


def test_run_verify_format_filter(tmp_path: Path) -> None:
    receipt_path, _ = _build_receipt(tmp_path)

    stream = io.StringIO()
    result = run_verify(
        receipt_path=receipt_path,
        which="cose",
        pinned_jwk=None,
        pinned_pem=None,
        verbose=False,
        stream=stream,
        allow_unpinned_key=True,
    )
    assert result.ok is True
    names = [c.name for c in result.checks]
    assert "cose" in names
    # intoto and transparency should NOT be checked when filtering
    assert "intoto" not in names
    assert "transparency" not in names


# ---------- key trust ----------


def _signing_key_pem(tmp_path: Path) -> Path:
    """The public half of the key ``_build_receipt`` signs with, held out of band."""
    public = Ed25519PrivateKey.from_private_bytes(b"i" * 32).public_key()
    pem_path = tmp_path / "trusted.pub.pem"
    pem_path.write_bytes(
        public.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return pem_path


def test_run_verify_unpinned_key_is_not_a_pass_by_default(tmp_path: Path) -> None:
    """A receipt verified only against the key it carries does not PASS."""
    receipt_path, _ = _build_receipt(tmp_path)

    stream = io.StringIO()
    result = run_verify(
        receipt_path=receipt_path,
        which="all",
        pinned_jwk=None,
        pinned_pem=None,
        verbose=False,
        stream=stream,
    )

    output = stream.getvalue()
    assert result.ok is False
    assert "[FAIL] public_key" in output
    assert "trust-on-first-use" in output
    assert "--allow-unpinned-key" in output
    # The integrity checks still run and report on their own.
    assert "[PASS] subject_binding" in output
    assert "[PASS] cose" in output
    assert "OVERALL: FAIL" in output


def test_run_verify_unpinned_key_passes_only_with_opt_in(tmp_path: Path) -> None:
    receipt_path, _ = _build_receipt(tmp_path)

    stream = io.StringIO()
    result = run_verify(
        receipt_path=receipt_path,
        which="all",
        pinned_jwk=None,
        pinned_pem=None,
        verbose=False,
        stream=stream,
        allow_unpinned_key=True,
    )

    output = stream.getvalue()
    assert result.ok is True
    # The key-trust status is printed even without --verbose.
    assert "[PASS] public_key - trust-on-first-use" in output
    assert "OVERALL: PASS (unpinned key: integrity only)" in output


def test_run_verify_pinned_key_prints_trust_status(tmp_path: Path) -> None:
    receipt_path, _ = _build_receipt(tmp_path)

    stream = io.StringIO()
    result = run_verify(
        receipt_path=receipt_path,
        which="all",
        pinned_jwk=None,
        pinned_pem=_signing_key_pem(tmp_path).read_bytes(),
        verbose=False,
        stream=stream,
    )

    output = stream.getvalue()
    assert result.ok is True
    assert "[PASS] public_key - pinned-pem" in output
    assert "OVERALL: PASS" in output


def test_main_exit_code_follows_key_trust(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from bernstein_verify_receipt.verify import main

    receipt_path, _ = _build_receipt(tmp_path)

    assert main(["--receipt", str(receipt_path)]) == 1
    assert main(["--receipt", str(receipt_path), "--allow-unpinned-key"]) == 0
    assert (
        main(["--receipt", str(receipt_path), "--public-key", str(_signing_key_pem(tmp_path))]) == 0
    )
    capsys.readouterr()


def test_click_cli_exit_code_follows_key_trust(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from bernstein_verify_receipt.__main__ import cli

    receipt_path, _ = _build_receipt(tmp_path)
    runner = CliRunner()

    default = runner.invoke(cli, ["verify", str(receipt_path)])
    assert default.exit_code == 1, default.output
    assert "trust-on-first-use" in default.output

    opted_in = runner.invoke(cli, ["verify", str(receipt_path), "--allow-unpinned-key"])
    assert opted_in.exit_code == 0, opted_in.output
