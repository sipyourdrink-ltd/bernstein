"""CLI tests for ``bernstein webhook verify <event_id>`` (#2310).

``verify`` recomputes the inbound event hash and confirms the outbound result
hash against the journal. The receipts are produced by the webhook node in the
project ``.sdd`` tree so the test stays offline (no HTTP).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.webhook_cmd import webhook_group

_SECRET = "whsec_test"
_SOURCE = "nocode-bus"
_BODY = b'{"goal":"ship it"}'
_EVENT_ID = "evt_cli_1"
_TIMESTAMP = 1_700_000_000


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _seed(project: Path) -> None:
    """Seed a verifiable event using the same HMAC key the CLI resolves.

    ``BERNSTEIN_AUDIT_KEY_PATH`` is already set by the ``project`` fixture, so
    the spine is tagged with the key ``verify`` reloads.
    """
    from bernstein.core.security.audit import load_or_create_audit_key
    from bernstein.core.trigger_sources.webhook_node import (
        STANDARD_WEBHOOK_ID_HEADER,
        STANDARD_WEBHOOK_SIGNATURE_HEADER,
        STANDARD_WEBHOOK_TIMESTAMP_HEADER,
        emit_outbound_receipt,
        receive_inbound_webhook,
        sign_standard_webhook,
    )

    key = load_or_create_audit_key()
    sig = sign_standard_webhook(secret=_SECRET, msg_id=_EVENT_ID, timestamp=_TIMESTAMP, body=_BODY)
    receive_inbound_webhook(
        workdir=project,
        lineage_root=project / ".sdd" / "lineage",
        hmac_key=key,
        identity_dir=project / ".sdd" / "identity",
        secret=_SECRET,
        source=_SOURCE,
        headers={
            STANDARD_WEBHOOK_ID_HEADER: _EVENT_ID,
            STANDARD_WEBHOOK_TIMESTAMP_HEADER: str(_TIMESTAMP),
            STANDARD_WEBHOOK_SIGNATURE_HEADER: sig,
        },
        body=_BODY,
        timestamp=_TIMESTAMP,
    )
    emit_outbound_receipt(
        workdir=project,
        lineage_root=project / ".sdd" / "lineage",
        hmac_key=key,
        identity_dir=project / ".sdd" / "identity",
        event_id=_EVENT_ID,
        result={"status": "succeeded"},
        journal_head="head-hash",
        timestamp=_TIMESTAMP,
    )


def test_verify_ok(project: Path) -> None:
    _seed(project)
    runner = CliRunner()
    result = runner.invoke(webhook_group, ["verify", _EVENT_ID, "-w", str(project)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_verify_tampered_inbound_exit_2(project: Path) -> None:
    _seed(project)
    path = project / ".sdd" / "webhook-node" / "inbound" / f"{_EVENT_ID}.json"
    path.write_text(path.read_text(encoding="utf-8").replace(_SOURCE, "attacker"), encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(webhook_group, ["verify", _EVENT_ID, "-w", str(project)])
    assert result.exit_code == 2, result.output
    assert "MISMATCH" in result.output


def test_verify_missing_event_exit_1(project: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(webhook_group, ["verify", "evt_missing", "-w", str(project)])
    assert result.exit_code == 1, result.output
