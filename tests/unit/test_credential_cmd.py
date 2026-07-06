"""CLI tests for ``bernstein credential emit|verify`` (#2303)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.credential_cmd import credential_group
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
