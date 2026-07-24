"""Tests for the ``bernstein artifact verify`` CLI (issue #2608)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.lineage.artifact_record import record_artifact
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.recorder import LineageRecorder
from bernstein.core.lineage.store import LineageStore
from bernstein.core.tasks.artifacts import ArtifactKind

# The CLI encodes the operator secret as UTF-8, so the key must be printable.
_SECRET = "s" * 64


def _seed_artifact(workdir: Path, task_id: str, artifact: object) -> None:
    """Record an artifact into ``workdir``'s .sdd layout the way the CLI reads it."""
    sdd = workdir / ".sdd"
    priv_pem, pub_pem = generate_keypair()
    card = AgentCard(agent_id="agent:worker", kid="key-001", public_key_pem=pub_pem)
    record_artifact(
        recorder=LineageRecorder(store=LineageStore(sdd / "lineage"), operator_hmac_key=_SECRET.encode("utf-8")),
        sink_root=sdd / "artifacts",
        task_id=task_id,
        kind=ArtifactKind.OPS_RESULT,
        artifact=artifact,
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv_pem,
    )
    card_dir = sdd / "agents" / card.agent_id
    card_dir.mkdir(parents=True, exist_ok=True)
    (card_dir / "card.json").write_text(
        json.dumps({"agent_id": card.agent_id, "kid": card.kid, "public_key_pem": card.public_key_pem}),
        encoding="utf-8",
    )


def test_verify_ok_exit_zero(tmp_path: Path) -> None:
    _seed_artifact(tmp_path, "T-1", {"status": "ok", "changed": 2})
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["artifact", "verify", "T-1", "--workdir", str(tmp_path)],
        env={"BERNSTEIN_OPERATOR_SECRET": _SECRET},
    )
    assert result.exit_code == 0, result.output
    assert "VERIFIED" in result.output


def test_verify_json_verdict(tmp_path: Path) -> None:
    _seed_artifact(tmp_path, "T-2", {"status": "ok"})
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["artifact", "verify", "T-2", "--workdir", str(tmp_path), "--output-json"],
        env={"BERNSTEIN_OPERATOR_SECRET": _SECRET},
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["entry_hash"].startswith("sha256:")


def test_verify_fails_on_tampered_blob_exit_two(tmp_path: Path) -> None:
    _seed_artifact(tmp_path, "T-3", {"status": "ok"})
    blob = tmp_path / ".sdd" / "artifacts" / "T-3" / "artifact.bin"
    data = bytearray(blob.read_bytes())
    data[-1] ^= 0x10
    blob.write_bytes(bytes(data))

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["artifact", "verify", "T-3", "--workdir", str(tmp_path)],
        env={"BERNSTEIN_OPERATOR_SECRET": _SECRET},
    )
    assert result.exit_code == 2, result.output
    assert "TAMPERED" in result.output


def test_verify_missing_artifact_exit_two(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir()
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["artifact", "verify", "ghost", "--workdir", str(tmp_path)],
        env={"BERNSTEIN_OPERATOR_SECRET": _SECRET},
    )
    assert result.exit_code == 2, result.output
    assert "TAMPERED" in result.output
