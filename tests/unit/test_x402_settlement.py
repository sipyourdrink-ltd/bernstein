"""x402 settlement-hook tests (issue #2528).

The settlement hook is the concrete x402 adapter over the AP2 mandate /
consent-receipt surface (``bernstein.core.protocols.payments.mandates``). Each
test maps to an issue #2528 acceptance criterion:

* AC1 -- default off: a 402 challenge with no active x402 config invokes no
  hook and triggers no retry.
* AC2 -- a hook configured but no matching mandate refuses fail closed and the
  refusal is a chain-anchored receipt.
* AC3 -- the happy path emits a spend receipt whose bindings recompute offline
  against the WAL invocation record and the mandate; mutating the amount, the
  challenge digest, or the invocation digest fails verification.
* AC5 -- spend-ledger rollups include settled amounts tagged per server.

Bernstein never executes payments here; the hook is a hermetic fake that stands
in for the operator's own payment tooling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bernstein.core.wal import WALEntry, WALWriter

from bernstein.core.cost.mcp_server_cost import MCPServerCostMeter
from bernstein.core.cost.spend_ledger import SpendLedger
from bernstein.core.protocols.payments.mandates import IntentMandate
from bernstein.core.protocols.payments.x402 import (
    SettlementContext,
    SettlementStatus,
    X402Config,
    X402SettlementCoordinator,
    build_retry_request,
    parse_challenge,
    read_spend_receipt,
    verify_spend_receipt,
)
from bernstein.core.security.audit_chain import AuditChainStore

_KEY = b"x" * 32
_SERVER = "acme-data-api"
_TOOL = "fetch_dataset"


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _challenge_response(amount_usd: float = 4.0, *, req_id: int = 7) -> dict[str, Any]:
    """A JSON-RPC error carrying an x402 402 challenge with an explicit USD price."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": 402,
            "message": "Payment Required",
            "data": {
                "x402Version": 1,
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": "base",
                        "maxAmountRequired": "4000000",
                        "asset": "usdc",
                        "resource": "https://acme.example/datasets/1",
                        "amountUsd": amount_usd,
                    }
                ],
            },
        },
    }


def _settled_response(req_id: int = 7) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": "ok"}]}}


def _call_message(req_id: int = 7) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": _TOOL, "arguments": {"q": "widgets"}},
    }


def _signed_intent(*, allowed: tuple[str, ...] = (_TOOL,), cap: float = 100.0) -> IntentMandate:
    return IntentMandate(
        task_id="task-x402",
        allowed_tool_calls=allowed,
        spend_cap_usd=cap,
        expires_at=0,
    ).sign(_KEY)


class _RecordingHook:
    """Hermetic settlement hook that records its calls and returns a fixed ref."""

    def __init__(self, payment_ref: str | None = "pay-ref-0001") -> None:
        self._ref = payment_ref
        self.calls: list[dict[str, Any]] = []

    def settle(self, challenge: Any, *, server_name: str, tool_name: str, amount_usd: float) -> str | None:
        self.calls.append(
            {"server_name": server_name, "tool_name": tool_name, "amount_usd": amount_usd, "challenge": challenge}
        )
        return self._ref


def _context(
    tmp_path: Path,
    *,
    config: X402Config,
    intent: IntentMandate | None,
    ledger: SpendLedger | None = None,
) -> tuple[SettlementContext, MCPServerCostMeter, AuditChainStore]:
    sdd = tmp_path / ".sdd"
    sdd.mkdir(parents=True, exist_ok=True)
    meter = MCPServerCostMeter(ledger=ledger, feature_label="x402-settlement")
    chain = AuditChainStore(sdd / "audit", key=_KEY)
    ctx = SettlementContext(
        config=config,
        hmac_key=_KEY,
        workdir=tmp_path,
        lineage_root=sdd / "lineage",
        wal_sdd_dir=sdd,
        wal_run_id="gw-test",
        intent=intent,
        meter=meter,
        audit_chain=chain,
        now=lambda: 1_700_000_000,
    )
    return ctx, meter, chain


def _record_wal_call(tmp_path: Path, run_id: str = "gw-test") -> WALEntry:
    """Append a settled mcp_tool_call to the WAL and return the entry."""
    writer = WALWriter(run_id=run_id, sdd_dir=tmp_path / ".sdd")
    return writer.append(
        decision_type="mcp_tool_call",
        inputs={
            "method": "tools/call",
            "server_name": _SERVER,
            "tool_name": _TOOL,
            "arguments": {"q": "widgets", "_x402_payment": {"payment_ref": "pay-ref-0001"}},
            "request_id": 7,
        },
        output={"result": {"content": []}, "error": None, "latency_ms": 3.0},
        actor="mcp_gateway",
    )


# ---------------------------------------------------------------------------
# Phase 1 -- challenge detection + config gate
# ---------------------------------------------------------------------------


class TestChallengeDetection:
    def test_parses_402_error_with_x402_data(self) -> None:
        challenge = parse_challenge(_challenge_response())
        assert challenge is not None
        assert challenge.x402_version == 1
        assert challenge.max_amount_required() == "4000000"
        assert challenge.resolved_amount_usd() == pytest.approx(4.0)

    def test_ignores_ordinary_success(self) -> None:
        assert parse_challenge(_settled_response()) is None

    def test_ignores_ordinary_error(self) -> None:
        resp = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}
        assert parse_challenge(resp) is None

    def test_challenge_hash_is_stable_and_sensitive(self) -> None:
        a = parse_challenge(_challenge_response(amount_usd=4.0))
        b = parse_challenge(_challenge_response(amount_usd=4.0))
        c = parse_challenge(_challenge_response(amount_usd=9.0))
        assert a is not None and b is not None and c is not None
        assert a.challenge_hash() == b.challenge_hash()
        assert a.challenge_hash() != c.challenge_hash()

    def test_build_retry_request_injects_payment_ref(self) -> None:
        retried = build_retry_request(_call_message(), "pay-ref-0001")
        assert retried["params"]["arguments"]["_x402_payment"] == {"payment_ref": "pay-ref-0001"}
        # Original untouched.
        assert "_x402_payment" not in _call_message()["params"]["arguments"]

    def test_config_defaults_off(self) -> None:
        assert X402Config().enabled is False
        assert X402Config().is_active() is False
        assert X402Config(enabled=True).is_active() is False  # no hook
        assert X402Config(enabled=True, hook=_RecordingHook()).is_active() is True


# ---------------------------------------------------------------------------
# AC1 -- default off
# ---------------------------------------------------------------------------


class TestDefaultOff:
    def test_disabled_config_skips_without_hook_lookup(self, tmp_path: Path) -> None:
        hook = _RecordingHook()
        cfg = X402Config(enabled=False, hook=hook)
        ctx, _meter, _chain = _context(tmp_path, config=cfg, intent=_signed_intent())
        coord = X402SettlementCoordinator(ctx)

        challenge = parse_challenge(_challenge_response())
        assert challenge is not None
        pre = coord.pre_authorize(challenge, server_name=_SERVER, tool_name=_TOOL)

        assert pre.status is SettlementStatus.SKIPPED
        assert hook.calls == []  # no hook lookup
        assert pre.payment_ref is None


# ---------------------------------------------------------------------------
# AC2 -- no matching mandate refuses fail closed with a chain-anchored receipt
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_no_intent_refuses_and_anchors_receipt(self, tmp_path: Path) -> None:
        hook = _RecordingHook()
        cfg = X402Config(enabled=True, hook=hook)
        ctx, _meter, chain = _context(tmp_path, config=cfg, intent=None)
        coord = X402SettlementCoordinator(ctx)

        challenge = parse_challenge(_challenge_response())
        assert challenge is not None
        pre = coord.pre_authorize(challenge, server_name=_SERVER, tool_name=_TOOL)

        assert pre.status is SettlementStatus.REFUSED
        assert hook.calls == []  # never pay when no mandate authorizes
        assert pre.refusal_receipt is not None
        # Refusal is a chain-anchored receipt.
        events = chain.query(event_type="x402.settlement_refused")
        assert len(events) == 1
        ok, _errs = chain.verify()
        assert ok is True

    def test_tool_outside_intent_refuses(self, tmp_path: Path) -> None:
        hook = _RecordingHook()
        cfg = X402Config(enabled=True, hook=hook)
        ctx, _meter, _chain = _context(tmp_path, config=cfg, intent=_signed_intent(allowed=("other_tool",)))
        coord = X402SettlementCoordinator(ctx)

        challenge = parse_challenge(_challenge_response())
        assert challenge is not None
        pre = coord.pre_authorize(challenge, server_name=_SERVER, tool_name=_TOOL)

        assert pre.status is SettlementStatus.REFUSED
        assert hook.calls == []

    def test_cap_breach_refuses_before_hook(self, tmp_path: Path) -> None:
        hook = _RecordingHook()
        cfg = X402Config(enabled=True, hook=hook)
        ctx, _meter, _chain = _context(tmp_path, config=cfg, intent=_signed_intent(cap=1.0))
        coord = X402SettlementCoordinator(ctx)

        challenge = parse_challenge(_challenge_response(amount_usd=4.0))
        assert challenge is not None
        pre = coord.pre_authorize(challenge, server_name=_SERVER, tool_name=_TOOL)

        assert pre.status is SettlementStatus.REFUSED
        assert hook.calls == []  # cap breach is caught before paying


# ---------------------------------------------------------------------------
# AC3 -- happy path: emit a verifiable spend receipt; tamper fails verification
# ---------------------------------------------------------------------------


class TestHappyPath:
    def _settle(self, tmp_path: Path, ledger: SpendLedger | None = None):
        hook = _RecordingHook()
        cfg = X402Config(enabled=True, hook=hook)
        intent = _signed_intent()
        ctx, meter, chain = _context(tmp_path, config=cfg, intent=intent, ledger=ledger)
        coord = X402SettlementCoordinator(ctx)

        challenge = parse_challenge(_challenge_response())
        assert challenge is not None
        pre = coord.pre_authorize(challenge, server_name=_SERVER, tool_name=_TOOL)
        assert pre.status is SettlementStatus.AUTHORIZED
        assert pre.payment_ref == "pay-ref-0001"
        assert len(hook.calls) == 1

        retried = build_retry_request(_call_message(), pre.payment_ref)
        wal_entry = _record_wal_call(tmp_path)
        receipt = coord.record_settlement(
            challenge,
            server_name=_SERVER,
            tool_name=_TOOL,
            payment_ref=pre.payment_ref,
            amount_usd=pre.amount_usd,
            retried_request=retried,
            wal_entry=wal_entry,
        )
        return coord, receipt, intent, meter, chain, wal_entry

    def test_emits_spend_receipt_that_verifies_offline(self, tmp_path: Path) -> None:
        _coord, receipt, intent, _meter, chain, wal_entry = self._settle(tmp_path)
        assert receipt.wal_invocation_digest == wal_entry.entry_hash
        assert receipt.journal_entry_hash  # anchored

        result = verify_spend_receipt(
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            wal_sdd_dir=tmp_path / ".sdd",
            spend_receipt_hash=receipt.receipt_hash(),
            intent=intent,
        )
        assert result.ok is True, result.reason

        events = chain.query(event_type="x402.settlement")
        assert len(events) == 1

    def test_mutating_recorded_amount_fails_verification(self, tmp_path: Path) -> None:
        _coord, receipt, intent, _meter, _chain, _wal = self._settle(tmp_path)
        path = tmp_path / ".sdd" / "x402" / "settlements" / f"{receipt.receipt_hash().replace(':', '_')}.json"
        raw = path.read_text(encoding="utf-8")
        # Bump the settled amount on disk.
        tampered = raw.replace('"amount_usd":4.0', '"amount_usd":0.01')
        assert tampered != raw
        path.write_text(tampered, encoding="utf-8")

        stored = read_spend_receipt(tmp_path, receipt.receipt_hash())
        assert stored is not None
        result = verify_spend_receipt(
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            wal_sdd_dir=tmp_path / ".sdd",
            spend_receipt_hash=receipt.receipt_hash(),
            intent=intent,
        )
        assert result.ok is False

    def test_mutating_challenge_digest_fails_verification(self, tmp_path: Path) -> None:
        _coord, receipt, intent, _meter, _chain, _wal = self._settle(tmp_path)
        path = tmp_path / ".sdd" / "x402" / "settlements" / f"{receipt.receipt_hash().replace(':', '_')}.json"
        raw = path.read_text(encoding="utf-8")
        tampered = raw.replace(receipt.settlement_ref.challenge_hash, "sha256:" + "0" * 64)
        assert tampered != raw
        path.write_text(tampered, encoding="utf-8")

        result = verify_spend_receipt(
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            wal_sdd_dir=tmp_path / ".sdd",
            spend_receipt_hash=receipt.receipt_hash(),
            intent=intent,
        )
        assert result.ok is False

    def test_mutating_wal_invocation_output_fails_verification(self, tmp_path: Path) -> None:
        _coord, receipt, intent, _meter, _chain, _wal = self._settle(tmp_path)
        # Tamper the WAL invocation record the receipt paid for.
        wal_path = tmp_path / ".sdd" / "runtime" / "wal" / "gw-test.wal.jsonl"
        raw = wal_path.read_text(encoding="utf-8")
        tampered = raw.replace('"text": "ok"', '"text": "tampered"').replace('"content":[]', '"content":[1]')
        # Whatever the exact output shape, ensure something changed.
        if tampered == raw:
            tampered = raw.replace('"latency_ms":3.0', '"latency_ms":9999.0')
        wal_path.write_text(tampered, encoding="utf-8")

        result = verify_spend_receipt(
            workdir=tmp_path,
            lineage_root=tmp_path / ".sdd" / "lineage",
            hmac_key=_KEY,
            wal_sdd_dir=tmp_path / ".sdd",
            spend_receipt_hash=receipt.receipt_hash(),
            intent=intent,
        )
        assert result.ok is False


# ---------------------------------------------------------------------------
# AC5 -- spend ledger rollups include settled amounts tagged per server
# ---------------------------------------------------------------------------


class TestLedgerRollup:
    def test_settled_amount_flushed_per_server(self, tmp_path: Path) -> None:
        ledger = SpendLedger(path=tmp_path / ".sdd" / "cost" / "ledger.jsonl", run_id="gw-test")
        happy = TestHappyPath()
        _coord, _receipt, _intent, meter, _chain, _wal = happy._settle(tmp_path, ledger=ledger)

        # Per-server accumulation on the meter.
        assert meter.cost_for("task-x402", _SERVER) == pytest.approx(4.0)
        # Flushed into the shared ledger and visible in the task rollup.
        assert ledger.totals_by("task").get("task-x402", 0.0) == pytest.approx(4.0)
        # Tagged with the server name.
        entries = SpendLedger.load_entries(ledger.path)
        assert any(e.tags.get("mcp_server") == _SERVER for e in entries)
