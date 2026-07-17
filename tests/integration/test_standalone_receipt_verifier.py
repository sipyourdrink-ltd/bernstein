"""Hermetic-venv proof that the audit-receipt verifier is truly standalone.

The promise of ``tools/verify_audit_receipt.py`` is that an external auditor
validates a bernstein audit receipt with the off-the-shelf libraries they
already run - ``cryptography`` + ``cbor2`` - and **no bernstein package** on
PYTHONPATH, no operator HMAC key.

This is the empirical SOTA-axis (verifiability) proof for #2604:

1. Create a fresh venv with only ``cryptography`` and ``cbor2`` installed.
2. Assert ``import bernstein`` fails inside it (the venv is hermetic).
3. Build a real three-format receipt in the project venv.
4. Run the verifier under the isolated interpreter and assert PASS - a stock
   COSE_Sign1 / in-toto / RFC 6962 verification path validates the receipt.
5. Mutate exactly one underlying chain entry in the receipt and re-run the
   verifier: every format fails because the head recomputed from the embedded
   range no longer matches the signed subject.

If the verifier ever gains a ``from bernstein...`` import, step 4 raises
``ModuleNotFoundError`` and the test fails - that is the whole point.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_receipt import build_receipt
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER_SCRIPT = REPO_ROOT / "tools" / "verify_audit_receipt.py"

_HMAC_KEY = b"x" * 32
_SINCE = "2020-01-01T00:00:00.000000Z"
_UNTIL = "2100-01-01T00:00:00.000000Z"


def _create_isolated_venv(venv_dir: Path) -> Path:
    """Create a venv with only cryptography + cbor2 installed."""
    import shutil as _shutil

    uv_bin = _shutil.which("uv")
    if uv_bin:
        subprocess.run([uv_bin, "venv", str(venv_dir), "--quiet"], check=True)
        py = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run(
            [uv_bin, "pip", "install", "--quiet", "--python", str(py), "cryptography>=45.0.0", "cbor2>=5.6"],
            check=True,
        )
        return py

    venv.create(str(venv_dir), with_pip=True, clear=True)
    py = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    subprocess.run(
        [
            str(py),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "cryptography>=45.0.0",
            "cbor2>=5.6",
        ],
        check=True,
        cwd=str(venv_dir),
    )
    return py


def _assert_no_bernstein(py: Path) -> None:
    out = subprocess.run([str(py), "-c", "import bernstein"], capture_output=True, text=True, check=False)
    assert out.returncode != 0, "venv must NOT have bernstein installed"
    assert "ModuleNotFoundError" in out.stderr or "No module named" in out.stderr, out.stderr


def _build_receipt(tmp_path: Path) -> Path:
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
        ),
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
    return receipt.receipt_path


def _run_verifier(py: Path, receipt: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(py), str(VERIFIER_SCRIPT), "--receipt", str(receipt), *extra],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture(scope="module")
def isolated_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    venv_dir = tmp_path_factory.mktemp("receipt-venv")
    py = _create_isolated_venv(venv_dir)
    _assert_no_bernstein(py)
    return py


class TestStandaloneReceiptVerifier:
    """End-to-end verifiability proof under a hermetic interpreter."""

    def test_pass_path(self, tmp_path: Path, isolated_python: Path) -> None:
        receipt = _build_receipt(tmp_path)
        proc = _run_verifier(isolated_python, receipt)
        assert proc.returncode == 0, f"stderr={proc.stderr!r} stdout={proc.stdout!r}"
        assert "OVERALL: PASS" in proc.stdout
        assert "[PASS] cose" in proc.stdout
        assert "[PASS] intoto" in proc.stdout
        assert "[PASS] transparency" in proc.stdout

    def test_tamper_one_entry_fails_all_formats(self, tmp_path: Path, isolated_python: Path) -> None:
        receipt = _build_receipt(tmp_path)
        # Prove PASS first, then mutate exactly one underlying chain entry.
        assert _run_verifier(isolated_python, receipt).returncode == 0

        data = json.loads(receipt.read_text())
        data["events"][1]["actor"] = "mallory"
        tampered = tmp_path / "tampered.json"
        tampered.write_text(json.dumps(data))

        proc = _run_verifier(isolated_python, tampered)
        assert proc.returncode == 1
        assert "OVERALL: FAIL" in proc.stdout
        assert "[FAIL] subject_binding" in proc.stdout
        assert "[FAIL] cose" in proc.stdout
        assert "[FAIL] intoto" in proc.stdout
        assert "[FAIL] transparency" in proc.stdout

    def test_verifier_runs_without_bernstein(self, tmp_path: Path, isolated_python: Path) -> None:
        receipt = _build_receipt(tmp_path)
        proc = _run_verifier(isolated_python, receipt)
        assert "ModuleNotFoundError" not in proc.stderr
