"""Verifier and retry linkage contracts for tool-call effects (#6270 slices 2 & 3).

Covers both ``tools/verify_audit_receipt.py`` (wrapped by ``verify-audit-receipt``)
and the standalone ``verify_cli/bernstein_verify_receipt`` package so both
offline verifiers enforce the exact same decision -> dispatch -> effect triple
and ``retry_of`` semantics without importing ``bernstein.*``.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.cli.commands.audit_cmd import audit_group
from bernstein.core.persistence.wal import WALWriter
from bernstein.core.protocols.mcp.mcp_gateway import MCPGateway
from bernstein.core.protocols.payments.mandates import IntentMandate
from bernstein.core.protocols.payments.x402 import SettlementContext, X402Config, X402SettlementCoordinator
from bernstein.core.security.audit import AuditLog
from bernstein.core.security.audit_chain import (
    EVENT_TOOLCALL_ATTESTATION,
    EVENT_TOOLCALL_ENFORCED_DISPATCH,
    AuditChainStore,
    record_toolcall_effect,
)
from bernstein.core.security.audit_receipt import build_receipt
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter
from bernstein.core.security.native_toolcall_evidence import NativeToolCallEvidenceProvider
from bernstein.core.security.toolcall_interlock import (
    AttestationMode,
    AttestationVerdict,
    ToolCallAttestationInterlock,
    ToolCallIntent,
    canonical_effect_digest,
    derive_attestation_verdict,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLS_VERIFIER = REPO_ROOT / "tools" / "verify_audit_receipt.py"
VERIFY_CLI_ROOT = REPO_ROOT / "verify_cli"
VERIFY_CLI_MODULE = VERIFY_CLI_ROOT / "bernstein_verify_receipt" / "verify.py"

_HMAC_KEY = b"k" * 32
_SIGN_SEED = b"i" * 32
_SINCE = "2020-01-01T00:00:00.000000Z"
_UNTIL = "2100-01-01T00:00:00.000000Z"


def _load_verifier(kind: str) -> Any:
    """Load either the repo tools script or the standalone verify_cli module."""
    if kind == "tools":
        path = TOOLS_VERIFIER
        name = "test_6270_tools_verify_audit_receipt"
    elif kind == "verify_cli":
        path = VERIFY_CLI_MODULE
        name = "test_6270_cli_verify_audit_receipt"
    else:
        raise ValueError(f"unknown verifier kind: {kind}")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_verifier(kind: str, receipt_path: Path, *extra: str) -> tuple[int, str]:
    verifier = _load_verifier(kind)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = verifier.main(["--receipt", str(receipt_path), *extra])
    return rc, buf.getvalue()


def _signing_pair(tmp_path: Path) -> tuple[FileBasedKMSAdapter, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.from_private_bytes(_SIGN_SEED)
    priv_path = tmp_path / "sign.pem"
    priv_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    pub_path = tmp_path / "sign.pub.pem"
    pub_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )
    return FileBasedKMSAdapter(priv_path, kid="test-6270-key"), pub_path


def _export_receipt(audit_dir: Path, tmp_path: Path) -> tuple[Path, Path]:
    kms, pub_path = _signing_pair(tmp_path)
    receipt = build_receipt(
        audit_dir,
        since=_SINCE,
        until=_UNTIL,
        key=_HMAC_KEY,
        kms_adapter=kms,
        output_dir=tmp_path / "receipts",
        write=True,
    )
    assert receipt.receipt_path is not None
    return receipt.receipt_path, pub_path


def _intent(*, request_id: int | str = 7, span_id: str = "span-1", retry_of: str | None = None) -> ToolCallIntent:
    kwargs: dict[str, Any] = {
        "scope_id": "scope:run-1:agent-1",
        "server_name": "filesystem",
        "method": "tools/call",
        "tool_name": "read_file",
        "request_id": request_id,
        "span_id": span_id,
        "arguments": {"path": "/tmp/example.txt"},
    }
    if retry_of is not None:
        kwargs["retry_of"] = retry_of
    return ToolCallIntent.from_request(**kwargs)


def _artifact_file(effects_dir: Path, intent_digest: str) -> Path:
    assert intent_digest.startswith("sha256:")
    return effects_dir / f"{intent_digest.removeprefix('sha256:')}.effect"


class _SettlementHook:
    def settle(self, challenge: Any, *, server_name: str, tool_name: str, amount_usd: float) -> str | None:
        del challenge, server_name, tool_name, amount_usd
        return "pay-ref-0001"


def _x402_settlement(tmp_path: Path) -> X402SettlementCoordinator:
    key = b"x" * 32
    sdd = tmp_path / ".sdd"
    sdd.mkdir(parents=True, exist_ok=True)
    mandate = IntentMandate(task_id="task-x402", allowed_tool_calls=("read_file",), spend_cap_usd=100.0).sign(key)
    return X402SettlementCoordinator(
        SettlementContext(
            config=X402Config(enabled=True, hook=_SettlementHook()),
            hmac_key=key,
            workdir=tmp_path,
            lineage_root=sdd / "lineage",
            wal_sdd_dir=sdd,
            wal_run_id="effect-retry",
            intent=mandate,
            meter=None,
            audit_chain=AuditChainStore(sdd / "x402-audit", key=key),
            now=lambda: 1_700_000_000,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_closed_decision_dispatch_effect_triple_verifies_with_recorded_response(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    audit_dir = tmp_path / "audit"
    effects_dir = tmp_path / "effects"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain, effects_dir=effects_dir)

    intent = _intent()
    received = b'  {"id": 7, "jsonrpc": "2.0", "result": {"text": "\\u20ac"}}\r\n'.strip()
    response = json.loads(received)
    evidence = await provider.prepare_dispatch(intent)
    await provider.record_effect(
        intent,
        evidence,
        response=response,
        duration_ms=11.5,
        outcome="ok",
        received=received,
    )

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path)
    assert _artifact_file(effects_dir, intent.digest()).read_bytes() == received

    rc, out = _run_verifier(
        verifier_kind,
        receipt_path,
        "--public-key",
        str(pub_path),
        "--effects-dir",
        str(effects_dir),
    )
    assert rc == 0, out
    assert "OVERALL: PASS" in out
    assert "[PASS] toolcall_effects" in out
    assert "status=closed" in out
    assert "outcome=ok" in out
    assert "payload=verified" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_tampered_response_fails_the_effect_leg(tmp_path: Path, verifier_kind: str) -> None:
    audit_dir = tmp_path / "audit"
    effects_dir = tmp_path / "effects"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain, effects_dir=effects_dir)

    # First call: raw JSON-RPC connector response.
    first_intent = _intent(request_id=7, span_id="span-json")
    raw_response = b'{"jsonrpc": "2.0", "id": 7, "result": {"ok": true}}'
    first_evidence = await provider.prepare_dispatch(first_intent)
    await provider.record_effect(
        first_intent,
        first_evidence,
        response=json.loads(raw_response),
        duration_ms=8.0,
        outcome="ok",
        received=raw_response,
    )

    # Second call: file-changing patch diff (same preimage as ResultBundle.patch_sha256).
    patch_intent = _intent(request_id=8, span_id="span-patch")
    patch_text = "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n-old\n+new\n"
    patch_evidence = await provider.prepare_dispatch(patch_intent)
    await provider.record_effect(
        patch_intent,
        patch_evidence,
        response={"jsonrpc": "2.0", "id": 8, "result": patch_text},
        duration_ms=9.25,
        outcome="ok",
        patch=patch_text,
    )

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path)

    # Prove PASS before tampering.
    rc_ok, out_ok = _run_verifier(
        verifier_kind,
        receipt_path,
        "--public-key",
        str(pub_path),
        "--effects-dir",
        str(effects_dir),
    )
    assert rc_ok == 0, out_ok

    # Tamper the recorded response bytes while leaving the signed receipt untouched.
    json_artifact = _artifact_file(effects_dir, first_intent.digest())
    json_artifact.write_bytes(b'{"jsonrpc": "2.0", "id": 7, "result": {"ok": false}}')

    rc_bad, out_bad = _run_verifier(
        verifier_kind,
        receipt_path,
        "--public-key",
        str(pub_path),
        "--effects-dir",
        str(effects_dir),
    )
    assert rc_bad == 1, out_bad
    assert "OVERALL: FAIL" in out_bad
    assert "[PASS] subject_binding" in out_bad
    assert "[PASS] cose" in out_bad
    assert "[PASS] intoto" in out_bad
    assert "[PASS] transparency" in out_bad
    assert "[FAIL] toolcall_effects" in out_bad
    assert "mismatch" in out_bad.lower()

    # Restore JSON response and tamper the patch diff instead.
    json_artifact.write_bytes(raw_response)
    patch_artifact = _artifact_file(effects_dir, patch_intent.digest())
    patch_artifact.write_text(patch_text.replace("+new", "+tampered"), encoding="utf-8")

    rc_patch_bad, out_patch_bad = _run_verifier(
        verifier_kind,
        receipt_path,
        "--public-key",
        str(pub_path),
        "--effects-dir",
        str(effects_dir),
    )
    assert rc_patch_bad == 1, out_patch_bad
    assert "[FAIL] toolcall_effects" in out_patch_bad
    assert "OVERALL: FAIL" in out_patch_bad


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_missing_effect_record_reads_unobserved_not_success(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain)
    await provider.prepare_dispatch(_intent())

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path)
    rc, out = _run_verifier(verifier_kind, receipt_path, "--public-key", str(pub_path))

    assert rc == 0, out
    assert "OVERALL: PASS" in out
    assert "[PASS] toolcall_effects" in out
    assert "dispatched, effect unobserved" in out
    assert "status=closed" not in out
    assert "outcome=ok" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_pre_effect_chains_still_verify(tmp_path: Path, verifier_kind: str) -> None:
    # Shape 1: legacy chain with no toolcall events at all.
    legacy_dir = tmp_path / "legacy-audit"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    log = AuditLog(legacy_dir, key=_HMAC_KEY)
    log.log("task.created", "alice", "task", "T-1", {"role": "backend"})
    log.log("task.completed", "alice", "task", "T-1", {"status": "ok"})
    legacy_receipt, legacy_pub = _export_receipt(legacy_dir, tmp_path / "legacy")

    rc_legacy, out_legacy = _run_verifier(verifier_kind, legacy_receipt, "--public-key", str(legacy_pub))
    assert rc_legacy == 0, out_legacy
    assert "OVERALL: PASS" in out_legacy
    assert "[PASS] toolcall_effects" in out_legacy
    assert "effect unobserved" in out_legacy

    # Shape 2: pre-slice-1 toolcall chain (attestation + dispatch, no toolcall.effect).
    pre_effect_dir = tmp_path / "pre-effect-audit"
    chain = AuditChainStore(pre_effect_dir, key=_HMAC_KEY)
    await NativeToolCallEvidenceProvider(chain).prepare_dispatch(_intent())
    pre_receipt, pre_pub = _export_receipt(pre_effect_dir, tmp_path / "pre-effect")

    rc_pre, out_pre = _run_verifier(verifier_kind, pre_receipt, "--public-key", str(pre_pub))
    assert rc_pre == 0, out_pre
    assert "OVERALL: PASS" in out_pre
    assert "[PASS] toolcall_effects" in out_pre
    assert "effect unobserved" in out_pre


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_retry_is_a_second_intent_linked_by_retry_of(tmp_path: Path, verifier_kind: str) -> None:
    # 1. Non-retry digest stays identical when retry_of is omitted.
    base_intent = _intent(request_id=7, span_id="span-1")
    explicit_none = ToolCallIntent.from_request(
        scope_id="scope:run-1:agent-1",
        server_name="filesystem",
        method="tools/call",
        tool_name="read_file",
        request_id=7,
        span_id="span-1",
        arguments={"path": "/tmp/example.txt"},
        retry_of=None,
    )
    assert base_intent.digest() == explicit_none.digest()

    # 2. End-to-end gateway x402 retry emits a second intent with its own
    # request_id linked to the first intent by retry_of.
    audit_dir = tmp_path / "audit"
    effects_dir = tmp_path / "effects"
    sdd = tmp_path / ".sdd"
    sdd.mkdir(exist_ok=True)
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain, effects_dir=effects_dir)
    gateway = MCPGateway(
        upstream_cmd=[],
        wal_writer=WALWriter(run_id="effect-retry", sdd_dir=sdd),
        server_name="filesystem",
        attestation_interlock=ToolCallAttestationInterlock(
            provider=provider,
            scope_id="scope:run-1:agent-1",
            mode=AttestationMode.ENFORCED,
        ),
        settlement=_x402_settlement(tmp_path),
    )

    challenge = {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": 402,
            "message": "Payment Required",
            "data": {
                "x402Version": 1,
                "accepts": [{"scheme": "exact", "maxAmountRequired": "4000000", "amountUsd": 4.0}],
            },
        },
    }
    settled = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
    request = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "/tmp/example.txt"}},
    }
    with patch.object(
        gateway,
        "_send_request",
        new_callable=AsyncMock,
        side_effect=[(challenge, 5.0), (settled, 6.0)],
    ):
        assert await gateway.handle_jsonrpc(request) == settled

    events = chain.query(resource_id="scope:run-1:agent-1")
    attestations = [e for e in events if e.event_type == EVENT_TOOLCALL_ATTESTATION]
    dispatches = [e for e in events if e.event_type == EVENT_TOOLCALL_ENFORCED_DISPATCH]
    assert len(attestations) == 2
    assert len(dispatches) == 2

    first_att, retry_att = attestations
    assert "retry_of" not in first_att.details
    assert retry_att.details["retry_of"] == first_att.details["intent_digest"]
    assert retry_att.details["intent_digest"] != first_att.details["intent_digest"]
    assert retry_att.details["attestation_ref"] != first_att.details["attestation_ref"]
    assert retry_att.details["request_id"] != first_att.details["request_id"]

    # One-attestation-one-dispatch invariant stays intact.
    as_dicts = [{"event_type": e.event_type, "details": e.details} for e in events]
    assert derive_attestation_verdict(as_dicts) is AttestationVerdict.COMPLETE
    duplicated = [*as_dicts, {"event_type": EVENT_TOOLCALL_ENFORCED_DISPATCH, "details": dispatches[0].details}]
    assert derive_attestation_verdict(duplicated) is AttestationVerdict.OBSERVED

    # Receipt verifier reports both triples closed and prints the retry linkage line.
    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path)
    rc, out = _run_verifier(
        verifier_kind,
        receipt_path,
        "--public-key",
        str(pub_path),
        "--effects-dir",
        str(effects_dir),
    )
    assert rc == 0, out
    assert "OVERALL: PASS" in out
    assert "[PASS] toolcall_effects" in out
    assert f"retry_of={first_att.details['intent_digest'][:19]}" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_broken_effect_bindings_and_duplicate_dispatch_fail_effect_leg(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    # Case A: wrong dispatch_ref on toolcall.effect fails the effect leg even on a cleanly signed receipt.
    bad_dispatch_dir = tmp_path / "bad-dispatch"
    chain_a = AuditChainStore(bad_dispatch_dir, key=_HMAC_KEY)
    provider_a = NativeToolCallEvidenceProvider(chain_a)
    intent_a = _intent(request_id=11, span_id="span-a")
    ev_a = await provider_a.prepare_dispatch(intent_a)
    record_toolcall_effect(
        chain=chain_a,
        intent_digest=ev_a.intent_digest,
        effect_digest=canonical_effect_digest({"ok": True}),
        outcome="ok",
        duration_ms=1.0,
        attestation_ref=ev_a.attestation_ref,
        dispatch_ref="hmac:" + ("0" * 64),
        resource_id=intent_a.scope_id,
    )
    receipt_a, pub_a = _export_receipt(bad_dispatch_dir, tmp_path / "out-a")
    rc_a, out_a = _run_verifier(verifier_kind, receipt_a, "--public-key", str(pub_a))
    assert rc_a == 1, out_a
    assert "[FAIL] toolcall_effects" in out_a
    assert "dispatch_ref" in out_a

    # Case B: duplicate enforced_dispatch for one attestation_ref violates one-attestation-one-dispatch.
    dup_dir = tmp_path / "dup-dispatch"
    chain_b = AuditChainStore(dup_dir, key=_HMAC_KEY)
    provider_b = NativeToolCallEvidenceProvider(chain_b)
    intent_b = _intent(request_id=12, span_id="span-b")
    ev_b = await provider_b.prepare_dispatch(intent_b)
    chain_b.log_with_prev_digest(
        event_type=EVENT_TOOLCALL_ENFORCED_DISPATCH,
        actor="bernstein.toolcall-interlock",
        resource_type="toolcall_scope",
        resource_id=intent_b.scope_id,
        details={
            "attestation_ref": ev_b.attestation_ref,
            "intent_digest": ev_b.intent_digest,
            "request_id": intent_b.request_id,
            "tool_name": intent_b.tool_name,
        },
    )
    receipt_b, pub_b = _export_receipt(dup_dir, tmp_path / "out-b")
    rc_b, out_b = _run_verifier(verifier_kind, receipt_b, "--public-key", str(pub_b))
    assert rc_b == 1, out_b
    assert "[FAIL] toolcall_effects" in out_b

    # Case C: retry reusing the prior attempt's request_id fails the effect leg.
    same_req_dir = tmp_path / "same-req-retry"
    chain_c = AuditChainStore(same_req_dir, key=_HMAC_KEY)
    provider_c = NativeToolCallEvidenceProvider(chain_c)
    first_c = _intent(request_id=20, span_id="span-c1")
    await provider_c.prepare_dispatch(first_c)
    bad_retry_c = _intent(request_id=20, span_id="span-c2", retry_of=first_c.digest())
    await provider_c.prepare_dispatch(bad_retry_c)
    receipt_c, pub_c = _export_receipt(same_req_dir, tmp_path / "out-c")
    rc_c, out_c = _run_verifier(verifier_kind, receipt_c, "--public-key", str(pub_c))
    assert rc_c == 1, out_c
    assert "[FAIL] toolcall_effects" in out_c
    assert "request_id" in out_c


@pytest.mark.asyncio
async def test_cli_surfaces_forward_effects_dir_and_verifiers_stay_bernstein_free(tmp_path: Path) -> None:
    for script in (TOOLS_VERIFIER, VERIFY_CLI_MODULE):
        source = script.read_text(encoding="utf-8")
        offending = [
            ln
            for ln in source.splitlines()
            if ln.strip().startswith(("import bernstein", "from bernstein")) and not ln.lstrip().startswith("#")
        ]
        assert not offending, f"{script.name} imports bernstein: {offending}"

    audit_dir = tmp_path / "audit"
    effects_dir = tmp_path / "effects"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain, effects_dir=effects_dir)
    intent = _intent(request_id=99, span_id="span-cli")
    raw = b'{"jsonrpc":"2.0","id":99,"result":{"ok":true}}'
    ev = await provider.prepare_dispatch(intent)
    await provider.record_effect(intent, ev, response=json.loads(raw), duration_ms=3.0, outcome="ok", received=raw)

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path)

    runner = CliRunner()
    res = runner.invoke(
        audit_group,
        [
            "receipt",
            "verify",
            str(receipt_path),
            "--public-key",
            str(pub_path),
            "--effects-dir",
            str(effects_dir),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "OVERALL: PASS" in res.output
    assert "payload=verified" in res.output

    # Standalone Click CLI (bernstein-verify-receipt verify --effects-dir ...)
    import importlib

    sys.path.insert(0, str(VERIFY_CLI_ROOT))
    try:
        standalone_mod: Any = importlib.import_module("bernstein_verify_receipt.__main__")
        standalone_cli: Any = standalone_mod.cli
    finally:
        if str(VERIFY_CLI_ROOT) in sys.path:
            sys.path.remove(str(VERIFY_CLI_ROOT))

    res_cli = runner.invoke(
        standalone_cli,
        [
            "verify",
            str(receipt_path),
            "--public-key",
            str(pub_path),
            "--effects-dir",
            str(effects_dir),
        ],
    )
    assert res_cli.exit_code == 0, res_cli.output
    assert "payload=verified" in res_cli.output
