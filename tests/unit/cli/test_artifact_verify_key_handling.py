"""Tests for artifact verify secret handling (key loading vs creation)."""

import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.main import cli
from bernstein.core.lineage.artifact_record import record_artifact
from bernstein.core.lineage.identity import AgentCard, generate_keypair
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore
from bernstein.core.security.audit import AuditKeyMissingError, load_audit_key, load_or_create_audit_key
from bernstein.core.tasks.artifacts import ArtifactKind

_SECRET = "s" * 64


def _seed_artifact(
    workdir: Path, task_id: str, artifact: object, operator_secret: bytes = _SECRET.encode("utf-8")
) -> None:
    sdd = workdir / ".sdd"
    priv_pem, pub_pem = generate_keypair()
    card = AgentCard(agent_id="agent:worker", kid="key-001", public_key_pem=pub_pem)
    record_artifact(
        recorder=SignedLineageLog(store=LineageStore(sdd / "lineage"), operator_hmac_key=operator_secret),
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


def test_verify_without_key_file_skips_hmac_and_does_not_create_key(tmp_path: Path) -> None:
    # Ensure no audit key exists
    key_path = None
    try:
        # Determine default key path via load_audit_key (will raise)
        load_audit_key()
    except AuditKeyMissingError:
        # The exception contains the missing path in its message; extract it
        # Simple approach: use default function from module
        from bernstein.core.security.audit import _default_audit_key_path

        key_path = _default_audit_key_path()
    if key_path and key_path.exists():
        key_path.unlink()

    _seed_artifact(tmp_path, "TK-1", {"status": "ok"})
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["artifact", "verify", "TK-1", "--workdir", str(tmp_path)],
        env={},
    )
    assert result.exit_code == 0, result.output
    assert "VERIFIED" in result.output
    # Verify key file still does not exist
    if key_path:
        assert not key_path.exists()


def test_verify_with_existing_key_uses_it(tmp_path: Path) -> None:
    # Create a real audit key file
    key_path = None
    try:
        load_audit_key()
    except AuditKeyMissingError:
        from bernstein.core.security.audit import _default_audit_key_path

        key_path = _default_audit_key_path()
    # Ensure key exists now
    if key_path:
        # load_or_create_audit_key will create it
        load_or_create_audit_key(key_path)

    # Use the same audit key for HMAC to match verification
    audit_key = load_audit_key()
    _seed_artifact(tmp_path, "TK-2", {"status": "ok"}, operator_secret=audit_key)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["artifact", "verify", "TK-2", "--workdir", str(tmp_path)],
        env={},
    )
    assert result.exit_code == 0, result.output
    assert "VERIFIED" in result.output
    # Key file should still exist
    if key_path:
        assert key_path.exists()
