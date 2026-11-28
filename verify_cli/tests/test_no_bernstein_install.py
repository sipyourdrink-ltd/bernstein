"""Install-isolation integration test.

The promise of `bernstein-verify` collapses if the wheel transitively
needs `bernstein` to run. This test creates a fresh venv that has ONLY
`bernstein-verify` (+ its declared deps) installed, generates a
compliance-pack-shaped ZIP using bernstein in the OUTER environment,
and verifies it from inside the clean venv via subprocess.

This test is intentionally slow (creates a venv, installs a wheel) and
marked `slow`. Run with `pytest -m slow` or in CI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import venv
import zipfile
from pathlib import Path

import pytest

# Test-scope import - allowed, but only at test scope.
from bernstein.core.lineage.entry import LineageEntry, canonicalise
from bernstein.core.lineage.identity import generate_keypair, sign_detached

_VERIFY_CLI_ROOT = Path(__file__).resolve().parent.parent


pytestmark = pytest.mark.slow


def _make_pack(tmp_path: Path) -> Path:
    """Build a tiny but well-formed compliance pack using bernstein."""
    priv, pub = generate_keypair()
    kid = "k-isolation-1"
    agent_id = "agent:isolated-test"

    entry = LineageEntry(
        v=1,
        artefact_path="src/foo.py",
        artefact_kind="file",
        content_hash="sha256:" + "a" * 64,
        parent_hashes=[],
        agent_id=agent_id,
        agent_card_kid=kid,
        tool_call_id="tc-1",
        span_id="00f067aa0ba902b7",
        ts_ns=1_715_600_000_000_000_000,
        operator_hmac="deadbeef" * 8,
    )
    payload = canonicalise(entry)
    jws = sign_detached(payload, priv, kid=kid)
    entry_hash = "sha256:" + hashlib.sha256(payload).hexdigest()

    card = {
        "agent_id": agent_id,
        "kid": kid,
        "public_key_pem": pub,
        "protocol_version": "a2a/1.0",
    }

    bundle = tmp_path / "pack.zip"
    with zipfile.ZipFile(bundle, "w") as z:
        z.writestr(
            "lineage-log.jsonl",
            payload.decode("utf-8") + "\n",
        )
        z.writestr(f"signatures/{entry_hash}.jws", jws)
        z.writestr(f"agent-cards/{agent_id}.json", json.dumps(card))
    return bundle


def _make_clean_venv(tmp_path: Path) -> tuple[Path, Path]:
    """Create a venv with ONLY bernstein-verify installed.

    Prefers `uv venv --seed` (fast, no ensurepip dance) and falls back to
    stdlib `venv.create(with_pip=True)` when `uv` is unavailable.

    Returns (python_path, pip_path).
    """
    venv_dir = tmp_path / "vfresh"
    uv_bin = shutil.which("uv")
    if uv_bin:
        subprocess.run(
            [uv_bin, "venv", "--seed", "--quiet", str(venv_dir)],
            check=True,
            capture_output=True,
        )
    else:
        venv.create(venv_dir, with_pip=True, clear=True)

    py = venv_dir / "bin" / "python"
    pip = venv_dir / "bin" / "pip"
    assert py.exists(), f"venv python missing: {py}"
    # Install bernstein-verify from the local source tree.
    subprocess.run(
        [str(pip), "install", "--quiet", str(_VERIFY_CLI_ROOT)],
        check=True,
        capture_output=True,
    )
    return py, pip


def test_verify_pack_in_clean_venv_no_bernstein(tmp_path):
    bundle = _make_pack(tmp_path)
    py, _pip = _make_clean_venv(tmp_path)

    # Sanity: confirm bernstein is NOT installed in this venv.
    probe = subprocess.run(
        [str(py), "-c", "import bernstein"],
        capture_output=True,
        text=True,
    )
    assert probe.returncode != 0, "bernstein must NOT be importable in the clean venv"
    assert "ModuleNotFoundError" in probe.stderr or "No module" in probe.stderr

    # And bernstein_verify IS importable.
    probe2 = subprocess.run(
        [str(py), "-c", "import bernstein_verify; print(bernstein_verify.__version__)"],
        capture_output=True,
        text=True,
    )
    assert probe2.returncode == 0, probe2.stderr
    assert probe2.stdout.strip() == "1.0.0"

    # Run the CLI.
    result = subprocess.run(
        [str(py), "-m", "bernstein_verify", "pack", str(bundle)],
        capture_output=True,
        text=True,
        # Ensure subprocess doesn't accidentally inherit a PYTHONPATH
        # that re-introduces bernstein.
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert result.returncode == 0, (
        f"verify pack failed in clean venv\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "PASS" in result.stdout


def test_verify_cli_entry_point_works_in_clean_venv(tmp_path):
    """The `bernstein-verify` console-script must be on PATH inside the venv."""
    py, _pip = _make_clean_venv(tmp_path)
    cli = py.parent / "bernstein-verify"
    assert cli.exists(), f"console script missing: {cli}"
    result = subprocess.run([str(cli), "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "pack" in result.stdout
    assert "chain" in result.stdout
    assert "forks" in result.stdout


def test_installed_deps_are_minimal(tmp_path):
    """The fresh venv must only have cryptography + click + bernstein-verify
    (plus transitive deps of cryptography: cffi, pycparser).
    No bernstein, no rich, no textual, no httpx, etc.
    """
    _py, pip = _make_clean_venv(tmp_path)
    listing = subprocess.run(
        [str(pip), "list", "--format=json"],
        capture_output=True,
        text=True,
        check=True,
    )
    installed = {p["name"].lower() for p in json.loads(listing.stdout)}

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
        "asn1crypto",
        "defusedxml",
    }
    leaked = installed & forbidden
    assert not leaked, f"unexpected packages in clean venv: {leaked}"

    # Must contain our two direct deps.
    assert "cryptography" in installed
    assert "click" in installed
    assert "bernstein-verify" in installed
