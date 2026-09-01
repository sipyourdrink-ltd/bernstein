"""Install-isolation integration test for bernstein-verify-receipt.

The promise of `bernstein-verify-receipt` collapses if the wheel
transitively needs `bernstein` to run. This test creates a fresh venv that
has ONLY `bernstein-verify-receipt` (+ its declared deps) installed,
builds a real three-format receipt using bernstein in the OUTER environment,
and verifies it from inside the clean venv via subprocess.

This test is intentionally slow (creates a venv, installs a wheel) and
marked `slow`. Run with `pytest -m slow` or in CI.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_receipt import build_receipt
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]
_RECV_CLI_ROOT = REPO_ROOT / "verify_cli" / "bernstein_verify_receipt"

_HMAC_KEY = b"x" * 32
_SINCE = "2020-01-01T00:00:00.000000Z"
_UNTIL = "2100-01-01T00:00:00.000000Z"


def _make_clean_venv(tmp_path: Path) -> Path:
    """Create a venv with ONLY bernstein-verify-receipt installed.

    Uses ``uv venv --seed`` (fast, no ensurepip dance) and
    ``uv pip install`` for package installation.

    Returns the python path inside the venv.
    """
    import shutil as _shutil

    venv_dir = tmp_path / "rfresh"
    uv_bin = _shutil.which("uv")
    if uv_bin:
        subprocess.run(
            [uv_bin, "venv", "--seed", "--quiet", str(venv_dir)],
            check=True,
            capture_output=True,
        )
    else:
        import venv as _venv

        _venv.create(venv_dir, with_pip=True, clear=True)

    py = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert py.exists(), f"venv python missing: {py}"
    # Install bernstein-verify-receipt from the local source tree using uv.
    pip = venv_dir / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
    if not pip.exists():
        pip = venv_dir / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip3")
    # Use uv pip install when available (faster, respects hatchling build)
    if uv_bin:
        subprocess.run(
            [uv_bin, "pip", "install", "--quiet", "--python", str(py), str(_RECV_CLI_ROOT)],
            check=True,
            capture_output=True,
        )
    else:
        subprocess.run(
            [str(pip), "install", "--quiet", str(_RECV_CLI_ROOT)],
            check=True,
            capture_output=True,
        )
    return py


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
    return receipt.receipt_path


def _run_verifier(py: Path, receipt: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [str(py), "-m", "bernstein_verify_receipt", "verify", str(receipt), *extra],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture(scope="module")
def isolated_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    venv_dir = tmp_path_factory.mktemp("receipt-venv")
    py = _make_clean_venv(venv_dir)
    # Assert bernstein is NOT importable in this venv.
    probe = subprocess.run(
        [str(py), "-c", "import bernstein"], capture_output=True, text=True, check=False
    )
    assert probe.returncode != 0, "venv must NOT have bernstein installed"
    has_err = "ModuleNotFoundError" in probe.stderr or "No module named" in probe.stderr
    assert has_err, probe.stderr
    return py


class TestStandaloneReceiptVerifierInstall:
    """Install-isolation proof for bernstein-verify-receipt."""

    def test_verify_receipt_in_clean_venv(self, tmp_path: Path, isolated_python: Path) -> None:
        receipt = _build_receipt(tmp_path)
        proc = _run_verifier(isolated_python, receipt)
        assert proc.returncode == 0, f"stderr={proc.stderr!r} stdout={proc.stdout!r}"
        assert "OVERALL: PASS" in proc.stdout
        assert "[PASS] cose" in proc.stdout
        assert "[PASS] intoto" in proc.stdout
        assert "[PASS] transparency" in proc.stdout

    def test_tamper_receipt_fails_in_clean_venv(
        self, tmp_path: Path, isolated_python: Path
    ) -> None:
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

    def test_cli_entry_point_works_in_clean_venv(
        self, tmp_path: Path, isolated_python: Path
    ) -> None:
        """The `bernstein-verify-receipt` console-script must be on PATH inside the venv."""
        py_dir = isolated_python.parent
        name = "bernstein-verify-receipt.exe" if os.name == "nt" else "bernstein-verify-receipt"
        cli = py_dir / name
        assert cli.exists(), f"console script missing: {cli}"
        result = subprocess.run([str(cli), "--help"], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "verify" in result.stdout

    def test_installed_deps_are_minimal(self, tmp_path: Path) -> None:
        """The fresh venv must only have cryptography + cbor2 + click + bernstein-verify-receipt."""
        import json as _json
        import shutil as _shutil

        venv_dir = tmp_path / "rfresh"
        uv_bin = _shutil.which("uv")
        if uv_bin:
            subprocess.run(
                [uv_bin, "venv", "--seed", "--quiet", str(venv_dir)],
                check=True,
                capture_output=True,
            )
        else:
            import venv as _venv

            _venv.create(venv_dir, with_pip=True, clear=True)

        py = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        pip = venv_dir / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
        if not pip.exists():
            pip = venv_dir / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip3")
        # Use uv pip install for the local package
        if uv_bin:
            subprocess.run(
                [uv_bin, "pip", "install", "--quiet", "--python", str(py), str(_RECV_CLI_ROOT)],
                check=True,
                capture_output=True,
            )
        else:
            subprocess.run(
                [str(pip), "install", "--quiet", str(_RECV_CLI_ROOT)],
                check=True,
                capture_output=True,
            )
        listing = subprocess.run(
            [str(pip), "list", "--format=json"],
            capture_output=True,
            text=True,
            check=True,
        )
        installed = {p["name"].lower() for p in _json.loads(listing.stdout)}

        forbidden = {
            "bernstein",
            "fastapi",
            "uvicorn",
            "httpx",
            "rich",
            "textual",
            "pyyaml",
            "openai",
            "reportlab",
            "pillow",
            "websockets",
            "signxml",
            "keyring",
            "jsonschema",
            "mcp",
            "watchdog",
        }
        leaked = installed & forbidden
        assert not leaked, f"unexpected packages in clean venv: {leaked}"

        # Must contain our direct deps.
        assert "cryptography" in installed
        assert "cbor2" in installed
        assert "click" in installed
        assert "bernstein-verify-receipt" in installed
