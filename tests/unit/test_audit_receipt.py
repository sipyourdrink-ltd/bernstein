"""Unit tests for the standard verifiable audit receipt (#2604).

Covers the projection contract: the receipt subject digest IS the chain range
``head_sha256`` (no independently recomputed head), byte-identical determinism,
round-trip verification of all three formats, schema conformance, and the
substrate-coupling tamper property (mutate one embedded chain entry -> every
format fails).
"""

from __future__ import annotations

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_multitenant import export_tenant_slice
from bernstein.core.security.audit_receipt import (
    ALL_FORMATS,
    RECEIPT_TYPE,
    build_receipt,
)
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

if TYPE_CHECKING:
    from bernstein.core.security.lineage_kms import KMSAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER_SCRIPT = REPO_ROOT / "tools" / "verify_audit_receipt.py"

_HMAC_KEY = b"x" * 32
_SIGN_SEED = b"i" * 32
_SINCE = "2020-01-01T00:00:00.000000Z"
_UNTIL = "2100-01-01T00:00:00.000000Z"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_log(audit_dir: Path) -> None:
    """Populate ``audit_dir`` with three HMAC-chained events."""
    audit_dir.mkdir(parents=True, exist_ok=True)
    log = AuditLog(audit_dir, key=_HMAC_KEY)
    log.log("task.created", "alice", "task", "T-1", {"role": "backend"})
    log.log("agent.spawned", "orchestrator", "agent", "A-1", {"task": "T-1"})
    log.log("task.completed", "alice", "task", "T-1", {"status": "ok"})


def _kms(tmp_path: Path) -> KMSAdapter:
    """Return a deterministic file-backed Ed25519 KMS adapter."""
    key = Ed25519PrivateKey.from_private_bytes(_SIGN_SEED)
    tmp_path.mkdir(parents=True, exist_ok=True)
    key_path = tmp_path / "sign.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return FileBasedKMSAdapter(key_path, kid="test-receipt-key")


def _load_verifier() -> Any:
    """Load the standalone verifier as a module (no bernstein imports in it)."""
    import sys

    spec = importlib.util.spec_from_file_location("verify_audit_receipt", VERIFIER_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses can resolve the module namespace.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _verify(receipt_path: Path, *args: str) -> tuple[int, str]:
    """Run the standalone verifier's main() in-process; return (rc, stdout)."""
    verifier = _load_verifier()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = verifier.main(["--receipt", str(receipt_path), *args])
    return rc, buf.getvalue()


@pytest.fixture
def receipt_env(tmp_path: Path) -> dict[str, Any]:
    """Build a full three-format receipt on disk with a seeded chain."""
    audit_dir = tmp_path / ".sdd" / "audit"
    _seed_log(audit_dir)
    kms = _kms(tmp_path)
    receipt = build_receipt(
        audit_dir,
        since=_SINCE,
        until=_UNTIL,
        key=_HMAC_KEY,
        kms_adapter=kms,
        output_dir=tmp_path / "out",
        write=True,
    )
    assert receipt.receipt_path is not None
    return {"receipt": receipt, "audit_dir": audit_dir, "kms": kms, "tmp": tmp_path}


# ---------------------------------------------------------------------------
# Subject binding (AC1): subject digest IS the chain head, not recomputed
# ---------------------------------------------------------------------------


def test_subject_digest_equals_chain_head(receipt_env: dict[str, Any]) -> None:
    receipt = receipt_env["receipt"]
    assert receipt.event_count == 3
    subject_digest = receipt.receipt["subject"]["digest"]["sha256"]
    assert subject_digest == receipt.head_sha256
    assert receipt.receipt["range"]["head_sha256"] == receipt.head_sha256


def test_head_matches_multitenant_export_for_range(receipt_env: dict[str, Any]) -> None:
    """AC1: the head is the existing chain head, identical to the tenant export.

    The seeded events carry no tenant id, so they all map to the default tenant.
    The multitenant exporter over the same window must produce the identical
    ``head_sha256`` - proving the receipt binds the existing head, not a
    format-specific recomputation.
    """
    export = export_tenant_slice(
        receipt_env["audit_dir"],
        "default",
        since=_SINCE,
        until=_UNTIL,
        key=_HMAC_KEY,
        write=False,
    )
    assert export.head_sha256 == receipt_env["receipt"].head_sha256


def test_receipt_type_and_formats(receipt_env: dict[str, Any]) -> None:
    receipt = receipt_env["receipt"]
    assert receipt.receipt["receipt_type"] == RECEIPT_TYPE
    assert receipt.formats == tuple(sorted(ALL_FORMATS))


# ---------------------------------------------------------------------------
# Determinism (AC6): byte-identical replay
# ---------------------------------------------------------------------------


def test_byte_identical_replay(tmp_path: Path) -> None:
    audit_dir = tmp_path / ".sdd" / "audit"
    _seed_log(audit_dir)
    kms1 = _kms(tmp_path / "k1")
    kms2 = _kms(tmp_path / "k2")
    r1 = build_receipt(audit_dir, since=_SINCE, until=_UNTIL, key=_HMAC_KEY, kms_adapter=kms1, write=False)
    r2 = build_receipt(audit_dir, since=_SINCE, until=_UNTIL, key=_HMAC_KEY, kms_adapter=kms2, write=False)
    assert r1.receipt_bytes == r2.receipt_bytes
    assert r1.sha256 == r2.sha256


# ---------------------------------------------------------------------------
# Round-trip verification (AC2/AC3/AC4)
# ---------------------------------------------------------------------------


def test_verifier_passes_all_formats(receipt_env: dict[str, Any]) -> None:
    rc, out = _verify(receipt_env["receipt"].receipt_path)
    assert rc == 0, out
    assert "OVERALL: PASS" in out
    assert "[PASS] cose" in out
    assert "[PASS] intoto" in out
    assert "[PASS] transparency" in out
    assert "[PASS] subject_binding" in out


@pytest.mark.parametrize("fmt", ["cose", "intoto", "transparency"])
def test_each_format_verifies_alone(tmp_path: Path, fmt: str) -> None:
    audit_dir = tmp_path / ".sdd" / "audit"
    _seed_log(audit_dir)
    receipt = build_receipt(
        audit_dir,
        since=_SINCE,
        until=_UNTIL,
        key=_HMAC_KEY,
        kms_adapter=_kms(tmp_path),
        formats=(fmt,),
        output_dir=tmp_path / "out",
        write=True,
    )
    assert receipt.formats == (fmt,)
    rc, out = _verify(receipt.receipt_path, "--format", fmt)
    assert rc == 0, out
    assert f"[PASS] {fmt}" in out


def test_verifier_imports_no_bernstein() -> None:
    """The headline promise: the verifier has no bernstein import."""
    source = VERIFIER_SCRIPT.read_text(encoding="utf-8")
    offending = [
        ln
        for ln in source.splitlines()
        if (ln.strip().startswith(("import bernstein", "from bernstein"))) and not ln.lstrip().startswith("#")
    ]
    assert not offending, f"verifier imports bernstein: {offending}"


def test_pinned_jwk_matches(receipt_env: dict[str, Any]) -> None:
    receipt = receipt_env["receipt"]
    jwk_path = receipt_env["tmp"] / "trusted.jwk.json"
    jwk_path.write_text(json.dumps(receipt.receipt["signing"]["public_key_jwk"]))
    rc, out = _verify(receipt.receipt_path, "--jwk", str(jwk_path), "--verbose")
    assert rc == 0, out
    assert "[PASS] public_key - pinned-jwk" in out


def test_pinned_jwk_mismatch_fails(receipt_env: dict[str, Any]) -> None:
    receipt = receipt_env["receipt"]
    wrong = {"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "x": "A" * 43}
    jwk_path = receipt_env["tmp"] / "wrong.jwk.json"
    jwk_path.write_text(json.dumps(wrong))
    rc, out = _verify(receipt.receipt_path, "--jwk", str(jwk_path))
    assert rc == 1
    assert "[FAIL] public_key" in out


# ---------------------------------------------------------------------------
# Tamper property (AC5): mutate one embedded entry -> every format fails
# ---------------------------------------------------------------------------


def test_tamper_one_entry_fails_all_formats(receipt_env: dict[str, Any]) -> None:
    receipt = receipt_env["receipt"]
    data = json.loads(receipt.receipt_path.read_text())
    # Mutate exactly one underlying chain entry inside the embedded range.
    data["events"][1]["actor"] = "mallory"
    tampered = receipt_env["tmp"] / "tampered.json"
    tampered.write_text(json.dumps(data))

    rc, out = _verify(tampered)
    assert rc == 1
    assert "OVERALL: FAIL" in out
    assert "[FAIL] subject_binding" in out
    assert "[FAIL] cose" in out
    assert "[FAIL] intoto" in out
    assert "[FAIL] transparency" in out


def test_tamper_details_field_fails(receipt_env: dict[str, Any]) -> None:
    receipt = receipt_env["receipt"]
    data = json.loads(receipt.receipt_path.read_text())
    data["events"][0]["details"]["role"] = "frontend"
    tampered = receipt_env["tmp"] / "tampered2.json"
    tampered.write_text(json.dumps(data))
    rc, out = _verify(tampered)
    assert rc == 1
    assert "[FAIL] cose" in out
    assert "[FAIL] intoto" in out
    assert "[FAIL] transparency" in out


def test_dropped_event_fails(receipt_env: dict[str, Any]) -> None:
    receipt = receipt_env["receipt"]
    data = json.loads(receipt.receipt_path.read_text())
    data["events"] = data["events"][:-1]  # drop the chain-head event
    tampered = receipt_env["tmp"] / "dropped.json"
    tampered.write_text(json.dumps(data))
    rc, out = _verify(tampered)
    assert rc == 1
    assert "[FAIL] subject_binding" in out


# ---------------------------------------------------------------------------
# Schema conformance (AC7)
# ---------------------------------------------------------------------------


def test_receipt_validates_against_schema(receipt_env: dict[str, Any]) -> None:
    import jsonschema

    schema = json.loads((REPO_ROOT / "schemas" / "audit-receipt-v1.json").read_text())
    jsonschema.validate(receipt_env["receipt"].receipt, schema)


def test_empty_range_receipt(tmp_path: Path) -> None:
    """An empty window still yields a deterministic, verifiable receipt."""
    audit_dir = tmp_path / ".sdd" / "audit"
    _seed_log(audit_dir)
    # A window that excludes every seeded event.
    receipt = build_receipt(
        audit_dir,
        since="2100-01-01T00:00:00.000000Z",
        until="2100-01-02T00:00:00.000000Z",
        key=_HMAC_KEY,
        kms_adapter=_kms(tmp_path),
        output_dir=tmp_path / "out",
        write=True,
    )
    assert receipt.event_count == 0
    rc, out = _verify(receipt.receipt_path)
    assert rc == 0, out
    assert "OVERALL: PASS" in out


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_invalid_window_rejected(tmp_path: Path) -> None:
    audit_dir = tmp_path / ".sdd" / "audit"
    _seed_log(audit_dir)
    with pytest.raises(ValueError, match="must be <"):
        build_receipt(audit_dir, since=_UNTIL, until=_SINCE, key=_HMAC_KEY, kms_adapter=_kms(tmp_path), write=False)


def test_unknown_format_rejected(tmp_path: Path) -> None:
    audit_dir = tmp_path / ".sdd" / "audit"
    _seed_log(audit_dir)
    with pytest.raises(ValueError, match="unknown receipt format"):
        build_receipt(
            audit_dir,
            since=_SINCE,
            until=_UNTIL,
            key=_HMAC_KEY,
            kms_adapter=_kms(tmp_path),
            formats=("cose", "bogus"),
            write=False,
        )
