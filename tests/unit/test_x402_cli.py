"""CLI tests for x402 settlement listing + offline verification (#2528, phase 4).

``bernstein gateway settlements`` lists recorded spend receipts and
``bernstein mandate verify-settlement`` proves one offline against the WAL
invocation record and the authorising mandate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bernstein.core.wal import WALWriter
from click.testing import CliRunner

from bernstein.cli.commands.gateway_cmd import gateway_group
from bernstein.cli.commands.mandate_cmd import mandate_group
from bernstein.core.protocols.payments.mandates import IntentMandate
from bernstein.core.protocols.payments.x402 import (
    SettlementContext,
    X402Config,
    X402SettlementCoordinator,
    build_retry_request,
    parse_challenge,
)
from bernstein.core.security.audit import load_or_create_audit_key

_SERVER = "acme-data-api"
_TOOL = "fetch_dataset"


class _Hook:
    def settle(self, challenge: object, *, server_name: str, tool_name: str, amount_usd: float) -> str | None:
        return "pay-ref-cli"


def _challenge() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {
            "code": 402,
            "message": "Payment Required",
            "data": {"x402Version": 1, "accepts": [{"scheme": "exact", "amountUsd": 4.0}]},
        },
    }


@pytest.fixture
def settled_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, Path]:
    """A workspace with one recorded spend receipt; returns (root, hash, intent_file)."""
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    sdd = tmp_path / ".sdd"
    sdd.mkdir(parents=True, exist_ok=True)
    key = load_or_create_audit_key(tmp_path / "audit.key")

    intent = IntentMandate(task_id="task-cli", allowed_tool_calls=(_TOOL,), spend_cap_usd=50.0).sign(key)
    intent_file = tmp_path / "intent.json"
    intent_file.write_text(json.dumps(intent.to_dict()), encoding="utf-8")

    ctx = SettlementContext(
        config=X402Config(enabled=True, hook=_Hook()),
        hmac_key=key,
        workdir=tmp_path,
        lineage_root=sdd / "lineage",
        wal_sdd_dir=sdd,
        wal_run_id="gw-cli",
        intent=intent,
        meter=None,
        audit_chain=None,
        now=lambda: 1_700_000_000,
    )
    coord = X402SettlementCoordinator(ctx)
    challenge = parse_challenge(_challenge())
    assert challenge is not None
    pre = coord.pre_authorize(challenge, server_name=_SERVER, tool_name=_TOOL)

    writer = WALWriter(run_id="gw-cli", sdd_dir=sdd)
    wal_entry = writer.append(
        decision_type="mcp_tool_call",
        inputs={"method": "tools/call", "server_name": _SERVER, "tool_name": _TOOL, "arguments": {}, "request_id": 3},
        output={"result": {"content": []}, "error": None, "latency_ms": 1.0},
        actor="mcp_gateway",
    )
    retried = build_retry_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": _TOOL, "arguments": {}}},
        pre.payment_ref,
    )
    receipt = coord.record_settlement(
        challenge,
        server_name=_SERVER,
        tool_name=_TOOL,
        payment_ref=pre.payment_ref,
        amount_usd=pre.amount_usd,
        retried_request=retried,
        wal_entry=wal_entry,
    )
    return tmp_path, receipt.receipt_hash(), intent_file


def test_gateway_settlements_lists_receipt(settled_project: tuple[Path, str, Path]) -> None:
    root, _receipt_hash, _intent_file = settled_project
    result = CliRunner().invoke(gateway_group, ["settlements", "--workdir", str(root)])
    assert result.exit_code == 0, result.output
    assert _SERVER in result.output
    assert _TOOL in result.output


def test_verify_settlement_ok(settled_project: tuple[Path, str, Path]) -> None:
    root, receipt_hash, intent_file = settled_project
    result = CliRunner().invoke(
        mandate_group,
        ["verify-settlement", receipt_hash, "--intent", str(intent_file), "--workdir", str(root)],
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_verify_settlement_detects_tamper(settled_project: tuple[Path, str, Path]) -> None:
    root, receipt_hash, intent_file = settled_project
    path = root / ".sdd" / "x402" / "settlements" / f"{receipt_hash.replace(':', '_')}.json"
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace('"amount_usd":4.0', '"amount_usd":0.01'), encoding="utf-8")

    result = CliRunner().invoke(
        mandate_group,
        ["verify-settlement", receipt_hash, "--intent", str(intent_file), "--workdir", str(root)],
    )
    assert result.exit_code == 2, result.output


def test_gateway_settlements_empty(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir(parents=True, exist_ok=True)
    result = CliRunner().invoke(gateway_group, ["settlements", "--workdir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "No x402 settlements" in result.output
