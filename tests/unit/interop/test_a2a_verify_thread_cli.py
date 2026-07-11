"""Tests for ``bernstein interop a2a verify-thread`` (#2304, AC2)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.interop_cmd import interop_group
from bernstein.core.interop.a2a_lineage import record_a2a_message
from bernstein.core.security.audit import load_or_create_audit_key

if TYPE_CHECKING:
    from pathlib import Path

_PEER_FP = "sha256:" + "cd" * 32


@pytest.fixture()
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with a fixed audit key so record + CLI share the key."""
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"a" * 64)
    key_path.chmod(0o600)
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    return tmp_path / "work"


def _seed_thread(workdir: Path, task_uuid: str = "task-cli") -> None:
    hmac_key = load_or_create_audit_key()
    lineage_root = workdir / ".sdd" / "lineage"
    identity_dir = workdir.parent / "identity"
    for seq, (direction, state) in enumerate(
        [("inbound", "submitted"), ("outbound", "working"), ("outbound", "completed")]
    ):
        record_a2a_message(
            workdir=workdir,
            lineage_root=lineage_root,
            hmac_key=hmac_key,
            identity_dir=identity_dir,
            task_uuid=task_uuid,
            direction=direction,
            state=state,
            peer_card_fingerprint=_PEER_FP,
            body=f"msg-{seq}".encode(),
            seq=seq,
            timestamp=1000 + seq,
        )


def test_verify_thread_ok(workdir: Path) -> None:
    _seed_thread(workdir)
    runner = CliRunner()
    result = runner.invoke(
        interop_group,
        ["a2a", "verify-thread", "--from-thread", "task-cli", "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output
    assert "verifies" in result.output


def test_verify_thread_json_ok(workdir: Path) -> None:
    _seed_thread(workdir)
    runner = CliRunner()
    result = runner.invoke(
        interop_group,
        ["a2a", "verify-thread", "--from-thread", "task-cli", "--workdir", str(workdir)],
        obj={"JSON": True},
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["message_count"] == 3


def test_verify_thread_tamper_exits_nonzero(workdir: Path) -> None:
    _seed_thread(workdir)
    receipt_path = workdir / ".sdd" / "a2a-messages" / "task-cli" / "0000.json"
    row = json.loads(receipt_path.read_text())
    row["peer_card_fingerprint"] = "sha256:" + "00" * 32
    receipt_path.write_text(json.dumps(row, separators=(",", ":"), sort_keys=True))
    runner = CliRunner()
    result = runner.invoke(
        interop_group,
        ["a2a", "verify-thread", "--from-thread", "task-cli", "--workdir", str(workdir)],
    )
    assert result.exit_code != 0
    assert "NOT" in result.output or "MISMATCH" in result.output or "FAIL" in result.output.upper()


def test_verify_thread_missing_thread_exits_nonzero(workdir: Path) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    result = runner.invoke(
        interop_group,
        ["a2a", "verify-thread", "--from-thread", "ghost", "--workdir", str(workdir)],
    )
    assert result.exit_code != 0
