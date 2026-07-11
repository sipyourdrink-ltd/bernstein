"""CLI tests for ``bernstein credential emit|verify`` (#2303)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.credential_cmd import (
    _load_or_create_install_key,
    credential_group,
)
from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.security.audit import load_or_create_audit_key

_RUN_ID = "run-1"
_ARTIFACT_REL = "out/report.md"
_CONTENT = b"# hello world\n"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with an artifact and a matching spine entry."""
    # Isolate the audit key so the HMAC key is stable and repo-local.
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    # Isolate the install signing key inside the project.
    monkeypatch.setenv("BERNSTEIN_CREDENTIAL_SIGNING_KEY", str(tmp_path / "install.key"))

    artifact = tmp_path / _ARTIFACT_REL
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(_CONTENT)

    spine = LineageSpine(
        tmp_path / ".sdd" / "lineage",
        run_id=_RUN_ID,
        hmac_key=load_or_create_audit_key(tmp_path / "audit.key"),
    )
    spine.record(
        artifact_path=_ARTIFACT_REL,
        content=_CONTENT,
        actor="agent-a",
        step_id="step-1",
        model="anthropic:claude",
        timestamp=1000,
    )
    return tmp_path


def test_emit_writes_manifest_next_to_artifact(project: Path) -> None:
    """AC1: emit projects the spine into a manifest written beside the artifact."""
    runner = CliRunner()
    result = runner.invoke(
        credential_group,
        ["emit", _ARTIFACT_REL, "--run-id", _RUN_ID, "--workdir", str(project)],
    )
    assert result.exit_code == 0, result.output
    manifest_path = project / "out" / "report.md.c2pa.json"
    assert manifest_path.exists()
    doc = json.loads(manifest_path.read_text())
    assert doc["signature_b64"]
    labels = [a["label"] for a in doc["assertions"]]
    assert "c2pa.hash.data" in labels
    assert "c2pa.actions" in labels


def test_emit_is_deterministic(project: Path) -> None:
    """AC2: two emits produce byte-identical manifest documents."""
    runner = CliRunner()
    r1 = runner.invoke(
        credential_group,
        ["emit", _ARTIFACT_REL, "--run-id", _RUN_ID, "--workdir", str(project), "--json"],
    )
    r2 = runner.invoke(
        credential_group,
        ["emit", _ARTIFACT_REL, "--run-id", _RUN_ID, "--workdir", str(project), "--json"],
    )
    assert r1.exit_code == 0, r1.output
    assert r2.exit_code == 0, r2.output
    assert r1.output == r2.output


def test_verify_ok_round_trip(project: Path) -> None:
    """AC3: verify confirms the emitted manifest against the artifact."""
    runner = CliRunner()
    emit = runner.invoke(
        credential_group,
        ["emit", _ARTIFACT_REL, "--run-id", _RUN_ID, "--workdir", str(project)],
    )
    assert emit.exit_code == 0, emit.output
    verify = runner.invoke(
        credential_group,
        ["verify", _ARTIFACT_REL, "--workdir", str(project)],
    )
    assert verify.exit_code == 0, verify.output
    assert "OK" in verify.output


def test_verify_fails_on_tampered_artifact(project: Path) -> None:
    """AC3: mutating the artifact after emit fails the hard-binding check."""
    runner = CliRunner()
    runner.invoke(
        credential_group,
        ["emit", _ARTIFACT_REL, "--run-id", _RUN_ID, "--workdir", str(project)],
    )
    (project / _ARTIFACT_REL).write_bytes(b"tampered")
    verify = runner.invoke(
        credential_group,
        ["verify", _ARTIFACT_REL, "--workdir", str(project)],
    )
    assert verify.exit_code == 2, verify.output
    assert "FAILED" in verify.output


def test_emit_without_lineage_fails(project: Path) -> None:
    """AC4: emit for an artifact with no spine entry is unproducible."""
    other = project / "out" / "other.md"
    other.write_bytes(b"no lineage")
    runner = CliRunner()
    result = runner.invoke(
        credential_group,
        ["emit", "out/other.md", "--run-id", _RUN_ID, "--workdir", str(project)],
    )
    assert result.exit_code != 0
    assert "unproducible" in result.output
    assert not (project / "out" / "other.md.c2pa.json").exists()


# ---------------------------------------------------------------------------
# Regression guards (#2303 flaky-emit hunt)
# ---------------------------------------------------------------------------

# The six ASCII bytes that ``bytes.strip()`` removes. A random 32-byte
# Ed25519 seed begins or ends with one of these ~4.7% of the time, so a
# strip() on the persisted key silently corrupted a valid key into a
# "not 32 raw bytes" error, failing emit only on those runs. This showed up
# as an order-/shard-dependent flake because each test mints its own key.
_ASCII_WHITESPACE_BYTES = (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20)


def _seed_with_boundary_byte(byte_value: int, *, at_end: bool) -> bytes:
    """Return a valid 32-byte Ed25519 seed whose first/last byte is fixed.

    Generates real keypairs until one lands the requested whitespace byte at
    the requested boundary, so the persisted-key round trip is exercised on
    exactly the inputs that ``strip()`` used to eat.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    idx = -1 if at_end else 0
    for _ in range(100_000):  # ~4.7% hit rate -> effectively certain
        seed = Ed25519PrivateKey.generate().private_bytes_raw()
        if seed[idx] == byte_value:
            return seed
    raise AssertionError(f"could not mint a seed with byte {byte_value:#04x} at index {idx}")


@pytest.mark.parametrize("byte_value", _ASCII_WHITESPACE_BYTES)
@pytest.mark.parametrize("at_end", [False, True])
def test_install_key_with_whitespace_boundary_round_trips(tmp_path: Path, byte_value: int, at_end: bool) -> None:
    """A persisted key whose boundary byte is ASCII whitespace loads verbatim.

    Regression: the loader must read the raw seed exactly, never ``strip()``
    it. Planting a key with a whitespace boundary byte and loading it back
    must return the identical 32-byte key rather than raising.
    """
    seed = _seed_with_boundary_byte(byte_value, at_end=at_end)
    key_path = tmp_path / "install.key"
    key_path.write_bytes(seed)

    loaded = _load_or_create_install_key(key_path)

    assert loaded.private_bytes_raw() == seed


def test_emit_verify_ignores_process_cwd(project: Path, tmp_path: Path) -> None:
    """The credential flow resolves everything from --workdir, not ambient CWD.

    Emit then verify from a working directory that is unrelated to the
    project root. Both must succeed against the explicit ``--workdir`` and
    the env-pinned keys, proving the CLI carries no hidden CWD dependency.
    """
    import os

    unrelated = tmp_path / "somewhere-else"
    unrelated.mkdir()
    prev = os.getcwd()
    os.chdir(unrelated)
    try:
        runner = CliRunner()
        emit = runner.invoke(
            credential_group,
            ["emit", _ARTIFACT_REL, "--run-id", _RUN_ID, "--workdir", str(project)],
        )
        assert emit.exit_code == 0, emit.output
        assert (project / "out" / "report.md.c2pa.json").exists()
        verify = runner.invoke(
            credential_group,
            ["verify", _ARTIFACT_REL, "--workdir", str(project)],
        )
        assert verify.exit_code == 0, verify.output
        assert "OK" in verify.output
    finally:
        os.chdir(prev)
