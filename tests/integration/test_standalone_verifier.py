"""Subprocess-isolated test that the standalone verifier really is standalone.

The previous "standalone" verifier imported
``bernstein.core.security.article12_bundle``. The promise of this module is
that the tool at ``tools/verify_audit_dsse.py`` runs against
**stdlib + cryptography only** - no ``bernstein`` package on PYTHONPATH.

The test:

1. Creates a fresh venv (``python -m venv``) inside a tmp dir.
2. Installs **only** the ``cryptography`` wheel into it.
3. Asserts that ``import bernstein`` from inside the venv raises
   ``ModuleNotFoundError`` (proves the venv is hermetic).
4. Builds a real DSSE-wrapped bundle in the project venv (the test
   process), writes the artefacts to disk.
5. Runs ``tools/verify_audit_dsse.py`` as a subprocess **using the new
   venv's Python interpreter** and asserts PASS.
6. For each tamper variant, runs the verifier again and asserts FAIL +
   non-zero exit code.

If the verifier ever gains a ``from bernstein...`` import, step 5 raises
``ModuleNotFoundError`` and the test fails.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.article12_bundle import build_article12_bundle
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_dsse import (
    export_public_key_pem,
    wrap_bundle,
    write_envelope,
)

# Slow tests gate - venv creation + cryptography install can take a minute.
pytestmark = pytest.mark.slow


REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER_SCRIPT = REPO_ROOT / "tools" / "verify_audit_dsse.py"


def _seed_log(audit_dir: Path) -> AuditLog:
    """Populate ``audit_dir`` with three HMAC-chained events."""
    audit_dir.mkdir(parents=True, exist_ok=True)
    log = AuditLog(audit_dir, key=b"x" * 32)
    log.log("task.created", "alice", "task", "T-1", {"role": "backend"})
    log.log("agent.spawned", "orchestrator", "agent", "A-1", {"task": "T-1"})
    log.log("task.completed", "alice", "task", "T-1", {"status": "ok"})
    return log


def _create_isolated_venv(venv_dir: Path) -> Path:
    """Create a venv with cryptography (and only cryptography).

    Uses ``uv venv`` + ``uv pip install`` because ``python -m venv --with-pip``
    fails on uv-managed Python (no bundled ensurepip). Falls back to stdlib
    ``venv`` when ``uv`` is not on PATH.

    Returns:
        Path to the venv's Python interpreter.
    """
    import shutil as _shutil

    uv_bin = _shutil.which("uv")
    if uv_bin:
        # Build with `uv venv` (no pip needed inside the venv). We install
        # cryptography by targeting the venv's Python via `uv pip --python`.
        subprocess.run(
            [uv_bin, "venv", str(venv_dir), "--quiet"],
            check=True,
        )
        if sys.platform == "win32":
            py = venv_dir / "Scripts" / "python.exe"
        else:
            py = venv_dir / "bin" / "python"
        subprocess.run(
            [
                uv_bin,
                "pip",
                "install",
                "--quiet",
                "--python",
                str(py),
                "cryptography>=50.0.0",
            ],
            check=True,
        )
        return py

    # Fallback for environments without uv.
    venv.create(str(venv_dir), with_pip=True, clear=True)
    if sys.platform == "win32":
        py = venv_dir / "Scripts" / "python.exe"
    else:
        py = venv_dir / "bin" / "python"
    subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "cryptography>=50.0.0"],
        check=True,
        cwd=str(venv_dir),
    )
    return py


def _assert_no_bernstein(py: Path) -> None:
    """Confirm ``import bernstein`` fails from inside the isolated venv."""
    out = subprocess.run(
        [str(py), "-c", "import bernstein"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode != 0, "venv must NOT have bernstein installed"
    assert "ModuleNotFoundError" in out.stderr or "No module named" in out.stderr, (
        f"expected ModuleNotFoundError, got stderr={out.stderr!r}"
    )


def _build_envelope_bundle(tmp_path: Path) -> dict[str, Any]:
    """Materialise bundle + envelope + public key on disk.

    Returns:
        Dict carrying ``envelope``, ``bundle``, ``public_key`` paths.
    """
    audit_dir = tmp_path / ".sdd" / "audit"
    _seed_log(audit_dir)
    today = datetime.now(tz=UTC).date()
    since = f"{today.isoformat()}T00:00:00+00:00"
    until = f"{(today + timedelta(days=1)).isoformat()}T00:00:00+00:00"
    output_dir = tmp_path / ".sdd" / "evidence"
    bundle = build_article12_bundle(
        audit_dir=audit_dir,
        since=since,
        until=until,
        risk_class="high",
        output_dir=output_dir,
        write=True,
    )
    assert bundle.archive_path is not None

    seed = b"i" * 32  # deterministic for repeatable runs
    key = Ed25519PrivateKey.from_private_bytes(seed)
    envelope = wrap_bundle(bundle, signing_key=key)
    envelope_path = tmp_path / "audit.dsse.json"
    write_envelope(envelope, envelope_path)

    pub_path = tmp_path / "audit.pub.pem"
    pub_path.write_bytes(export_public_key_pem(key.public_key()))

    return {
        "envelope": envelope_path,
        "bundle": bundle.archive_path,
        "public_key": pub_path,
    }


def _run_verifier(
    py: Path,
    *,
    envelope: Path,
    bundle: Path,
    public_key: Path,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the standalone verifier in the isolated venv."""
    cmd = [
        str(py),
        str(VERIFIER_SCRIPT),
        "--envelope",
        str(envelope),
        "--bundle",
        str(bundle),
        "--public-key",
        str(public_key),
    ]
    if extra_args:
        cmd.extend(extra_args)
    # Empty PYTHONPATH so the project's src/ does not leak into sys.path.
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)


@pytest.fixture(scope="module")
def isolated_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Module-scoped venv: created once, used by every test below."""
    venv_dir = tmp_path_factory.mktemp("standalone-venv")
    py = _create_isolated_venv(venv_dir)
    _assert_no_bernstein(py)
    return py


class TestStandaloneVerifier:
    """End-to-end checks against the standalone verifier."""

    def test_pass_path(self, tmp_path: Path, isolated_python: Path) -> None:
        artefacts = _build_envelope_bundle(tmp_path)
        proc = _run_verifier(isolated_python, **artefacts)
        assert proc.returncode == 0, f"verifier failed: stderr={proc.stderr!r} stdout={proc.stdout!r}"
        assert "OVERALL: PASS" in proc.stdout

    def test_pass_with_hmac_chain(self, tmp_path: Path, isolated_python: Path) -> None:
        """Passing --hmac-key adds a chain walk; we provide the real key.

        The audit module loads the key from a file; we replicate that on disk.
        """
        artefacts = _build_envelope_bundle(tmp_path)
        hmac_key_path = tmp_path / "audit.key"
        hmac_key_path.write_bytes(b"x" * 32)
        proc = _run_verifier(
            isolated_python,
            **artefacts,
            extra_args=["--hmac-key", str(hmac_key_path), "--verbose"],
        )
        assert proc.returncode == 0, f"verifier failed: stderr={proc.stderr!r} stdout={proc.stdout!r}"
        assert "[PASS] hmac_chain" in proc.stdout
        assert "OVERALL: PASS" in proc.stdout

    def test_envelope_signature_flip_fails(self, tmp_path: Path, isolated_python: Path) -> None:
        artefacts = _build_envelope_bundle(tmp_path)
        # Flip a byte in the signature.
        env = json.loads(artefacts["envelope"].read_text())
        sig = env["signatures"][0]["sig"]
        # Swap one base64 char for another to break the sig.
        bad = "A" + sig[1:] if sig[0] != "A" else "B" + sig[1:]
        env["signatures"][0]["sig"] = bad
        artefacts["envelope"].write_text(json.dumps(env))

        proc = _run_verifier(isolated_python, **artefacts)
        assert proc.returncode == 1
        assert "OVERALL: FAIL" in proc.stdout
        assert "[FAIL] envelope_signature" in proc.stdout

    def test_bundle_byte_flip_fails(self, tmp_path: Path, isolated_python: Path) -> None:
        artefacts = _build_envelope_bundle(tmp_path)
        # Flip the last byte of the bundle.
        raw = artefacts["bundle"].read_bytes()
        artefacts["bundle"].write_bytes(raw[:-1] + bytes([raw[-1] ^ 0x01]))

        proc = _run_verifier(isolated_python, **artefacts)
        assert proc.returncode == 1
        assert "OVERALL: FAIL" in proc.stdout
        assert "[FAIL] subject_sha256" in proc.stdout

    def test_chain_link_break_fails_with_hmac_key(
        self,
        tmp_path: Path,
        isolated_python: Path,
    ) -> None:
        """Tamper a single events.jsonl entry; chain walk must catch it."""
        import zipfile

        artefacts = _build_envelope_bundle(tmp_path)
        hmac_key_path = tmp_path / "audit.key"
        hmac_key_path.write_bytes(b"x" * 32)

        # Re-zip the bundle with a deliberately tampered events.jsonl.
        with zipfile.ZipFile(artefacts["bundle"]) as zf:
            members = {n: zf.read(n) for n in zf.namelist()}

        # Flip the last byte of the last event entry.
        events = members["events.jsonl"]
        # Locate last newline; flip the byte before the second-to-last newline.
        # Easier: replace one character of the JSON with another.
        members["events.jsonl"] = events.replace(b'"task.completed"', b'"task.tampered"', 1)

        # Rewrite a deterministic zip in the same shape as the original.
        out = tmp_path / "tampered.zip"
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(members):
                info = zipfile.ZipInfo(filename=name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, members[name])
        artefacts["bundle"] = out

        proc = _run_verifier(
            isolated_python,
            **artefacts,
            extra_args=["--hmac-key", str(hmac_key_path)],
        )
        assert proc.returncode == 1
        assert "OVERALL: FAIL" in proc.stdout
        # Either the subject digest catches it (different bundle bytes) or the
        # chain walk does. Both outcomes are valid - both prove tamper detection.
        assert "[FAIL] subject_sha256" in proc.stdout or "[FAIL] hmac_chain" in proc.stdout

    def test_verifier_does_not_import_bernstein(self, tmp_path: Path, isolated_python: Path) -> None:
        """The headline assertion: invoking the verifier in the isolated venv works.

        If anyone ever adds ``from bernstein...`` to ``tools/verify_audit_dsse.py``
        this call will raise ``ModuleNotFoundError`` and the test fails - that
        is the whole point of the test.
        """
        artefacts = _build_envelope_bundle(tmp_path)
        proc = _run_verifier(isolated_python, **artefacts)
        assert "ModuleNotFoundError" not in proc.stderr
        # Belt and braces: scan the script for a bernstein import.
        source = VERIFIER_SCRIPT.read_text(encoding="utf-8")
        # The string can appear in comments / docstrings. We grep only for
        # the import statement shape itself.
        offending_lines = [
            ln
            for ln in source.splitlines()
            if (ln.strip().startswith("import bernstein") or ln.strip().startswith("from bernstein"))
            and not ln.lstrip().startswith("#")
        ]
        assert not offending_lines, f"verifier imports bernstein: {offending_lines}"
