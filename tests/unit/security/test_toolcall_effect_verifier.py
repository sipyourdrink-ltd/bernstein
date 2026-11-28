"""Verifier and retry linkage contracts for tool-call effects (#6270 slices 2 & 3).

Covers both ``tools/verify_audit_receipt.py`` (wrapped by ``verify-audit-receipt``)
and the standalone ``verify_cli/bernstein_verify_receipt`` package so both
offline verifiers enforce the exact same decision -> dispatch -> effect triple
and ``retry_of`` semantics without importing ``bernstein.*``.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import io
import json
import os
import stat
import subprocess
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
from bernstein.core.security import native_toolcall_evidence as native_evidence_mod
from bernstein.core.security import toolcall_interlock as interlock_mod
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
    VerifiedDispatchEvidence,
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


def test_unreachable_security_controls_gate_passes_for_effect_helpers() -> None:
    """B1/S-b/N4 (#5053): internal preimage helpers stay private, shared in one module, and off the allowlist."""
    interlock_path = REPO_ROOT / "src" / "bernstein" / "core" / "security" / "toolcall_interlock.py"
    native_path = REPO_ROOT / "src" / "bernstein" / "core" / "security" / "native_toolcall_evidence.py"
    allowlist_text = (REPO_ROOT / "unreachable_controls_allowlist.txt").read_text(encoding="utf-8")

    def _top_level_defs(path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return {
            node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }

    interlock_defs = _top_level_defs(interlock_path)
    native_defs = _top_level_defs(native_path)

    for public_name in ("canonical_effect_preimage", "effect_preimage_for_connector_response"):
        assert public_name not in interlock_defs
        assert public_name not in native_defs
        assert public_name not in interlock_mod.__all__
        assert public_name not in native_evidence_mod.__all__
        assert f"::{public_name}" not in allowlist_text

    # S-b: exactly one definition in toolcall_interlock.py, shared by native_toolcall_evidence.py.
    for private_name in ("_canonical_effect_preimage", "_effect_preimage_for_connector_response"):
        assert private_name in interlock_defs
        assert private_name not in native_defs
    assert (
        native_evidence_mod._effect_preimage_for_connector_response  # pyright: ignore[reportPrivateUsage]
        is interlock_mod._effect_preimage_for_connector_response  # pyright: ignore[reportPrivateUsage]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
@pytest.mark.parametrize("bad_duration", [float("nan"), float("inf")])
async def test_non_finite_duration_ms_fails_effect_leg(
    tmp_path: Path,
    verifier_kind: str,
    bad_duration: float,
) -> None:
    """B2: NaN and Infinity duration_ms must fail closed in both offline verifiers."""
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain)
    intent = _intent(request_id=31, span_id="span-nan")
    ev = await provider.prepare_dispatch(intent)
    chain.log_with_prev_digest(
        event_type="toolcall.effect",
        actor="bernstein.toolcall-interlock",
        resource_type="toolcall_scope",
        resource_id=intent.scope_id,
        details={
            "attestation_ref": ev.attestation_ref,
            "dispatch_ref": ev.dispatch_ref,
            "intent_digest": ev.intent_digest,
            "effect_digest": canonical_effect_digest({"ok": True}),
            "outcome": "ok",
            "duration_ms": bad_duration,
        },
    )

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path / "out")
    rc, out = _run_verifier(verifier_kind, receipt_path, "--public-key", str(pub_path))
    assert rc == 1, out
    assert "OVERALL: FAIL" in out
    assert "[FAIL] toolcall_effects" in out
    assert "duration_ms" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_retry_of_must_be_sha256_digest_across_intent_provider_and_verifiers(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    """B3: retry_of must match ^sha256:[0-9a-f]{64}\\Z in from_request, prepare_dispatch, and verifiers."""
    for bad_retry in ("not-a-sha256", "sha256:" + ("A" * 64), "sha256:" + ("a" * 64) + "\n"):
        with pytest.raises(ValueError, match="retry_of"):
            _intent(request_id=41, span_id="span-bad-req", retry_of=bad_retry)

    chain = AuditChainStore(tmp_path / "audit", key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain)
    bypassed_intent = dataclasses.replace(_intent(request_id=42, span_id="span-bad-prov"), retry_of="not-a-sha256")
    with pytest.raises(ValueError, match="retry_of"):
        await provider.prepare_dispatch(bypassed_intent)

    valid_intent = _intent(request_id=43, span_id="span-forged-retry")
    chain.log_with_prev_digest(
        event_type=EVENT_TOOLCALL_ATTESTATION,
        actor="bernstein.toolcall-interlock",
        resource_type="toolcall_scope",
        resource_id=valid_intent.scope_id,
        details={
            "attestation_ref": "sha256:" + ("c" * 64),
            "intent_digest": valid_intent.digest(),
            "scope_id": valid_intent.scope_id,
            "server_name": valid_intent.server_name,
            "method": valid_intent.method,
            "tool_name": valid_intent.tool_name,
            "request_id": valid_intent.request_id,
            "span_id": valid_intent.span_id,
            "args_digest": valid_intent.args_digest,
            "retry_of": "not-a-sha256-digest",
        },
    )
    receipt_path, pub_path = _export_receipt(tmp_path / "audit", tmp_path / "out")
    rc, out = _run_verifier(verifier_kind, receipt_path, "--public-key", str(pub_path))
    assert rc == 1, out
    assert "[FAIL] toolcall_effects" in out
    assert "retry_of" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_malformed_intent_digest_fails_without_effects_dir(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    """B3: intent_digest format must be validated even when --effects-dir is omitted."""
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    att_ref = "sha256:" + ("d" * 64)
    bad_intent_digest = "not-a-sha256-intent-digest"
    chain.log_with_prev_digest(
        event_type=EVENT_TOOLCALL_ATTESTATION,
        actor="bernstein.toolcall-interlock",
        resource_type="toolcall_scope",
        resource_id="scope:run-1:agent-1",
        details={
            "attestation_ref": att_ref,
            "intent_digest": bad_intent_digest,
            "scope_id": "scope:run-1:agent-1",
            "server_name": "filesystem",
            "method": "tools/call",
            "tool_name": "read_file",
            "request_id": "51",
            "span_id": "span-51",
            "args_digest": "sha256:" + ("e" * 64),
        },
    )
    disp = chain.log_with_prev_digest(
        event_type=EVENT_TOOLCALL_ENFORCED_DISPATCH,
        actor="bernstein.toolcall-interlock",
        resource_type="toolcall_scope",
        resource_id="scope:run-1:agent-1",
        details={"attestation_ref": att_ref, "intent_digest": bad_intent_digest},
    )
    record_toolcall_effect(
        chain=chain,
        intent_digest=bad_intent_digest,
        effect_digest=canonical_effect_digest({"ok": True}),
        outcome="ok",
        duration_ms=1.0,
        attestation_ref=att_ref,
        dispatch_ref="hmac:" + disp.hmac,
        resource_id="scope:run-1:agent-1",
    )

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path / "out")
    rc, out = _run_verifier(verifier_kind, receipt_path, "--public-key", str(pub_path))
    assert rc == 1, out
    assert "[FAIL] toolcall_effects" in out
    assert "intent_digest" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
@pytest.mark.parametrize(
    "override_kwargs",
    [
        {"scope_id": "scope:run-1:other-agent"},
        {"server_name": "other-server"},
        {"tool_name": "drop_table"},
    ],
)
async def test_in_range_retry_of_must_match_scope_server_and_tool(
    tmp_path: Path,
    verifier_kind: str,
    override_kwargs: dict[str, str],
) -> None:
    """B3: an in-range retry_of must match (scope_id, server_name, tool_name) of the prior attempt."""
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain)

    first = _intent(request_id=61, span_id="span-61")
    await provider.prepare_dispatch(first)

    retry_kwargs: dict[str, Any] = {
        "scope_id": "scope:run-1:agent-1",
        "server_name": "filesystem",
        "method": "tools/call",
        "tool_name": "read_file",
        "request_id": "61:retry",
        "span_id": "span-61-retry",
        "arguments": {"path": "/tmp/example.txt"},
        "retry_of": first.digest(),
        **override_kwargs,
    }
    mismatched_retry = ToolCallIntent.from_request(**retry_kwargs)
    ev_retry = await provider.prepare_dispatch(mismatched_retry)
    await provider.record_effect(
        mismatched_retry,
        ev_retry,
        response={"result": {"ok": True}},
        duration_ms=2.0,
        outcome="ok",
        received=b'{"result":{"ok":true}}',
    )

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path / "out")
    rc, out = _run_verifier(verifier_kind, receipt_path, "--public-key", str(pub_path))
    assert rc == 1, out
    assert "[FAIL] toolcall_effects" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_duplicate_intent_digest_fails_effect_leg(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    """B4: duplicate intent_digest across distinct attestation_refs must fail closed."""
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain)

    intent = _intent(request_id=71, span_id="span-71")
    ev1 = await provider.prepare_dispatch(intent)
    await provider.record_effect(
        intent,
        ev1,
        response={"result": {"ok": True}},
        duration_ms=1.0,
        outcome="ok",
        received=b'{"result":{"ok":true}}',
    )

    second_att_ref = "sha256:" + ("f" * 64)
    chain.log_with_prev_digest(
        event_type=EVENT_TOOLCALL_ATTESTATION,
        actor="bernstein.toolcall-interlock",
        resource_type="toolcall_scope",
        resource_id=intent.scope_id,
        details={
            "attestation_ref": second_att_ref,
            "intent_digest": intent.digest(),
            "scope_id": intent.scope_id,
            "server_name": intent.server_name,
            "method": intent.method,
            "tool_name": intent.tool_name,
            "request_id": "72",
            "span_id": intent.span_id,
            "args_digest": intent.args_digest,
        },
    )
    disp2 = chain.log_with_prev_digest(
        event_type=EVENT_TOOLCALL_ENFORCED_DISPATCH,
        actor="bernstein.toolcall-interlock",
        resource_type="toolcall_scope",
        resource_id=intent.scope_id,
        details={"attestation_ref": second_att_ref, "intent_digest": intent.digest()},
    )
    record_toolcall_effect(
        chain=chain,
        intent_digest=intent.digest(),
        effect_digest=canonical_effect_digest(b'{"result":{"ok":true}}'),
        outcome="ok",
        duration_ms=1.0,
        attestation_ref=second_att_ref,
        dispatch_ref="hmac:" + disp2.hmac,
        resource_id=intent.scope_id,
    )

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path / "out")
    rc, out = _run_verifier(verifier_kind, receipt_path, "--public-key", str(pub_path))
    assert rc == 1, out
    assert "[FAIL] toolcall_effects" in out
    assert "intent_digest" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_duplicate_same_scope_request_id_without_retry_of_fails_effect_leg(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    """B4: duplicate same-scope request_id with no retry_of must fail closed."""
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain)

    first = _intent(request_id=81, span_id="span-81-a")
    second = _intent(request_id=81, span_id="span-81-b")
    ev1 = await provider.prepare_dispatch(first)
    await provider.record_effect(first, ev1, response={"result": {"ok": True}}, duration_ms=1.0, outcome="ok")
    ev2 = await provider.prepare_dispatch(second)
    await provider.record_effect(second, ev2, response={"result": {"ok": True}}, duration_ms=1.0, outcome="ok")

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path / "out")
    rc, out = _run_verifier(verifier_kind, receipt_path, "--public-key", str(pub_path))
    assert rc == 1, out
    assert "[FAIL] toolcall_effects" in out
    assert "request_id" in out


@pytest.mark.asyncio
async def test_observed_mode_failed_pre_dispatch_does_not_emit_dangling_retry_of_on_x402_retry(
    tmp_path: Path,
) -> None:
    """S1: mcp_gateway emits retry_of only when prior_prepared[1] is not None."""
    audit_dir = tmp_path / "audit"
    sdd = tmp_path / ".sdd"
    sdd.mkdir(exist_ok=True)
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    inner = NativeToolCallEvidenceProvider(chain)
    calls = 0

    class _FlakyObservedProvider:
        async def prepare_dispatch(self, intent: ToolCallIntent) -> VerifiedDispatchEvidence:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("transient audit write error")
            return await inner.prepare_dispatch(intent)

        async def record_effect(self, *args: Any, **kwargs: Any) -> None:
            await inner.record_effect(*args, **kwargs)

    gateway = MCPGateway(
        upstream_cmd=[],
        wal_writer=WALWriter(run_id="observed-retry", sdd_dir=sdd),
        server_name="filesystem",
        attestation_interlock=ToolCallAttestationInterlock(
            provider=_FlakyObservedProvider(),
            scope_id="scope:run-1:agent-1",
            mode=AttestationMode.OBSERVED,
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
        side_effect=[(challenge, 4.0), (settled, 5.0)],
    ):
        assert await gateway.handle_jsonrpc(request) == settled

    attestations = [
        e for e in chain.query(resource_id="scope:run-1:agent-1") if e.event_type == EVENT_TOOLCALL_ATTESTATION
    ]
    assert len(attestations) == 1
    assert "retry_of" not in attestations[0].details
    assert attestations[0].details["request_id"] == "7"


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_effects_dir_rejects_symlink_and_sha256_regex_uses_strict_end_anchor(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    """S2: --effects-dir rejects symlinks and _SHA256_PREFIXED_RE uses \\Z rather than $."""
    verifier = _load_verifier(verifier_kind)
    trailing_newline = "sha256:" + ("a" * 64) + "\n"
    assert verifier._SHA256_PREFIXED_RE.match(trailing_newline) is None

    audit_dir = tmp_path / "audit"
    effects_dir = tmp_path / "effects"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain, effects_dir=effects_dir)
    intent = _intent(request_id=91, span_id="span-symlink")
    with pytest.raises(ValueError, match="retry_of"):
        await provider.prepare_dispatch(dataclasses.replace(intent, retry_of=trailing_newline))
    raw = b'{"jsonrpc":"2.0","id":91,"result":{"ok":true}}'
    ev = await provider.prepare_dispatch(intent)
    await provider.record_effect(intent, ev, response=json.loads(raw), duration_ms=2.0, outcome="ok", received=raw)

    outside_target = tmp_path / "outside.effect"
    outside_target.write_bytes(raw)
    artifact = _artifact_file(effects_dir, intent.digest())
    artifact.unlink()
    artifact.symlink_to(outside_target)

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path / "out")
    rc, out = _run_verifier(
        verifier_kind,
        receipt_path,
        "--public-key",
        str(pub_path),
        "--effects-dir",
        str(effects_dir),
    )
    assert rc == 1, out
    assert "[FAIL] toolcall_effects" in out
    assert "symlink" in out.lower()


def _run_verifier_subprocess(
    verifier_kind: str,
    receipt_path: Path,
    *extra: str,
    timeout: float = 5.0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if verifier_kind == "tools":
        cmd = [sys.executable, str(TOOLS_VERIFIER), "--receipt", str(receipt_path), *extra]
    elif verifier_kind == "verify_cli":
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{VERIFY_CLI_ROOT}{os.pathsep}{existing}" if existing else str(VERIFY_CLI_ROOT)
        cmd = [sys.executable, "-m", "bernstein_verify_receipt.verify", "--receipt", str(receipt_path), *extra]
    else:
        raise ValueError(f"unknown verifier kind: {verifier_kind}")
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=env, timeout=timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_b4r_case_a_duplicate_request_id_with_outside_range_retry_of_fails(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    """B4-R(a): same-scope duplicate request_id with an outside-range retry_of must FAIL."""
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain)

    first = _intent(request_id=81, span_id="span-b4ra-1")
    ev1 = await provider.prepare_dispatch(first)
    await provider.record_effect(first, ev1, response={"result": {"ok": True}}, duration_ms=1.0, outcome="ok")

    replay = _intent(request_id=81, span_id="span-b4ra-2", retry_of="sha256:" + ("0" * 64))
    ev2 = await provider.prepare_dispatch(replay)
    await provider.record_effect(replay, ev2, response={"result": {"ok": True}}, duration_ms=1.0, outcome="ok")

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path / "out")
    rc, out = _run_verifier(verifier_kind, receipt_path, "--public-key", str(pub_path))
    assert rc == 1, out
    assert "OVERALL: FAIL" in out
    assert "[FAIL] toolcall_effects" in out
    assert "duplicate request_id" in out

    # Also verify B4-R(a) fails specifically due to duplicate request_id on a mid-chain
    # non-genesis receipt (independent of N3's genesis outside-range rule).
    receipt_data = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_data["events"][0]["prev_hmac"] = "a" * 64
    verifier = _load_verifier(verifier_kind)
    check, _lines = verifier.verify_toolcall_effects(receipt_data)
    assert check.ok is False
    assert "duplicate request_id" in check.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_b4r_case_b_duplicate_request_id_with_unrelated_in_range_retry_of_fails(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    """B4-R(b): A(req=81), C(req=99), B(req=81, retry_of=C) must FAIL in both verifiers."""
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain)

    intent_a = _intent(request_id=81, span_id="span-b4rb-a")
    ev_a = await provider.prepare_dispatch(intent_a)
    await provider.record_effect(intent_a, ev_a, response={"result": {"ok": True}}, duration_ms=1.0, outcome="ok")

    intent_c = _intent(request_id=99, span_id="span-b4rb-c")
    ev_c = await provider.prepare_dispatch(intent_c)
    await provider.record_effect(intent_c, ev_c, response={"result": {"ok": True}}, duration_ms=1.0, outcome="ok")

    intent_b = _intent(request_id=81, span_id="span-b4rb-b", retry_of=intent_c.digest())
    ev_b = await provider.prepare_dispatch(intent_b)
    await provider.record_effect(intent_b, ev_b, response={"result": {"ok": True}}, duration_ms=1.0, outcome="ok")

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path / "out")
    rc, out = _run_verifier(verifier_kind, receipt_path, "--public-key", str(pub_path))
    assert rc == 1, out
    assert "OVERALL: FAIL" in out
    assert "[FAIL] toolcall_effects" in out
    assert "duplicate request_id" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_s2r_effects_dir_rejects_directory_without_traceback(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    """S2-R: a directory at <intent>.effect must return clean OVERALL: FAIL, never a traceback."""
    audit_dir = tmp_path / "audit"
    effects_dir = tmp_path / "effects"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain, effects_dir=effects_dir)
    intent = _intent(request_id=92, span_id="span-dir")
    raw = b'{"jsonrpc":"2.0","id":92,"result":{"ok":true}}'
    ev = await provider.prepare_dispatch(intent)
    await provider.record_effect(intent, ev, response=json.loads(raw), duration_ms=1.5, outcome="ok", received=raw)

    artifact = _artifact_file(effects_dir, intent.digest())
    artifact.unlink()
    artifact.mkdir()

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path / "out")
    proc = _run_verifier_subprocess(
        verifier_kind,
        receipt_path,
        "--public-key",
        str(pub_path),
        "--effects-dir",
        str(effects_dir),
        timeout=5.0,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "OVERALL: FAIL" in proc.stdout
    assert "[FAIL] toolcall_effects" in proc.stdout
    assert "not a regular file" in proc.stdout.lower()
    assert "Traceback" not in proc.stdout + proc.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_s2r_effects_dir_rejects_fifo_without_hanging(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    """S2-R: a FIFO at <intent>.effect must return clean OVERALL: FAIL without hanging."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is not available on this platform")

    audit_dir = tmp_path / "audit"
    effects_dir = tmp_path / "effects"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain, effects_dir=effects_dir)
    intent = _intent(request_id=93, span_id="span-fifo")
    raw = b'{"jsonrpc":"2.0","id":93,"result":{"ok":true}}'
    ev = await provider.prepare_dispatch(intent)
    await provider.record_effect(intent, ev, response=json.loads(raw), duration_ms=1.5, outcome="ok", received=raw)

    artifact = _artifact_file(effects_dir, intent.digest())
    artifact.unlink()
    os.mkfifo(artifact)

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path / "out")
    proc = _run_verifier_subprocess(
        verifier_kind,
        receipt_path,
        "--public-key",
        str(pub_path),
        "--effects-dir",
        str(effects_dir),
        timeout=5.0,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "OVERALL: FAIL" in proc.stdout
    assert "[FAIL] toolcall_effects" in proc.stdout
    assert "not a regular file" in proc.stdout.lower()
    assert "Traceback" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
def test_s2r_sa_read_regular_effect_file_flags_inode_identity_and_oserror(
    tmp_path: Path,
    verifier_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S2-R + S-a: lstat before open, O_NONBLOCK|O_BINARY flags, fstat (dev, ino) check, and OSError handling."""
    verifier = _load_verifier(verifier_kind)
    digest_hex = "a" * 64
    intent_digest = f"sha256:{digest_hex}"
    target = tmp_path / f"{digest_hex}.effect"
    target.write_bytes(b"payload")

    # 1. Verify open flags include O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_BINARY (each via getattr).
    seen_flags: list[int] = []
    real_open = os.open

    def _recording_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        seen_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(verifier.os, "open", _recording_open)
    data, err = verifier._read_regular_effect_file(tmp_path, digest_hex, intent_digest)
    assert err is None and data == b"payload"
    expected_flags = (
        getattr(os, "O_RDONLY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    assert seen_flags == [expected_flags]
    monkeypatch.undo()

    # 2. Verify (st_dev, st_ino) mismatch between lstat and fstat fails closed.
    real_fstat = os.fstat

    class _SwappedStat:
        def __init__(self, base: os.stat_result) -> None:
            self.st_mode = stat.S_IFREG | 0o600
            self.st_dev = base.st_dev
            self.st_ino = base.st_ino + 1

    def _swapped_fstat(fd: int) -> _SwappedStat:
        return _SwappedStat(real_fstat(fd))

    monkeypatch.setattr(verifier.os, "fstat", _swapped_fstat)
    data_swap, err_swap = verifier._read_regular_effect_file(tmp_path, digest_hex, intent_digest)
    assert data_swap is None and err_swap is not None
    monkeypatch.undo()

    # 3. Verify OSError during fdopen/read produces a clean error instead of raising.
    def _failing_fdopen(fd: int, *args: Any, **kwargs: Any) -> Any:
        os.close(fd)
        raise OSError("simulated read error")

    monkeypatch.setattr(verifier.os, "fdopen", _failing_fdopen)
    data_err, err_read = verifier._read_regular_effect_file(tmp_path, digest_hex, intent_digest)
    assert data_err is None and err_read is not None and "simulated read error" in err_read


def test_sd_write_effect_payload_links_without_os_replace_and_refuses_clobber_or_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S-d: _write_effect_payload publishes via os.link (never os.replace), compares on EEXIST, rejects symlinks."""
    effects_dir = tmp_path / "effects"
    intent_digest = "sha256:" + ("b" * 64)

    def _forbid_replace(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("must not call os.replace")

    monkeypatch.setattr(native_evidence_mod.os, "replace", _forbid_replace)

    # Fresh write publishes without os.replace.
    written = native_evidence_mod._write_effect_payload(effects_dir, intent_digest, b"first-bytes")  # pyright: ignore[reportPrivateUsage]
    assert written.read_bytes() == b"first-bytes"
    assert not list(effects_dir.glob(".effect-*.tmp"))

    # Equal bytes on EEXIST succeeds idempotently.
    written_again = native_evidence_mod._write_effect_payload(effects_dir, intent_digest, b"first-bytes")  # pyright: ignore[reportPrivateUsage]
    assert written_again == written
    assert written.read_bytes() == b"first-bytes"

    # Different bytes on EEXIST raises FileExistsError and preserves original bytes.
    with pytest.raises(FileExistsError, match="existing effect payload"):
        native_evidence_mod._write_effect_payload(effects_dir, intent_digest, b"tampered-bytes")  # pyright: ignore[reportPrivateUsage]
    assert written.read_bytes() == b"first-bytes"
    assert not list(effects_dir.glob(".effect-*.tmp"))

    # Pre-placed (or raced) symlink raises ValueError and leaves both symlink and outside file untouched.
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"original-outside")
    sym_digest = "sha256:" + ("c" * 64)
    sym_target = effects_dir / f"{'c' * 64}.effect"
    sym_target.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        native_evidence_mod._write_effect_payload(effects_dir, sym_digest, b"new-bytes")  # pyright: ignore[reportPrivateUsage]
    assert sym_target.is_symlink()
    assert outside.read_bytes() == b"original-outside"
    assert not list(effects_dir.glob(".effect-*.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
async def test_n3_outside_range_retry_of_fails_on_genesis_receipt_and_passes_mid_chain(
    tmp_path: Path,
    verifier_kind: str,
) -> None:
    """N3: an outside-range retry_of on a receipt starting at genesis must FAIL; mid-chain stays PASS."""
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=_HMAC_KEY)
    provider = NativeToolCallEvidenceProvider(chain)

    dangling = _intent(request_id="95:retry", span_id="span-n3", retry_of="sha256:" + ("0" * 64))
    ev = await provider.prepare_dispatch(dangling)
    await provider.record_effect(dangling, ev, response={"result": {"ok": True}}, duration_ms=1.0, outcome="ok")

    receipt_path, pub_path = _export_receipt(audit_dir, tmp_path / "out")
    rc, out = _run_verifier(verifier_kind, receipt_path, "--public-key", str(pub_path))
    assert rc == 1, out
    assert "OVERALL: FAIL" in out
    assert "[FAIL] toolcall_effects" in out
    assert "genesis" in out.lower()

    # Mid-chain slice (non-genesis prev_hmac on first event) still accepts prior intent outside range.
    mid_chain = json.loads(receipt_path.read_text(encoding="utf-8"))
    mid_chain["events"][0]["prev_hmac"] = "a" * 64
    verifier = _load_verifier(verifier_kind)
    check, lines = verifier.verify_toolcall_effects(mid_chain)
    assert check.ok is True, check.detail
    assert any("prior intent outside range" in line for line in lines)


@pytest.mark.parametrize("verifier_kind", ["tools", "verify_cli"])
def test_n5_duplicate_request_id_keys_on_raw_json_value(verifier_kind: str) -> None:
    """N5: int 1, str '1', and padded ' 1 ' are distinct JSON-RPC request_ids; identical raw values collide."""
    verifier = _load_verifier(verifier_kind)

    def _att_event(att_hex: str, intent_hex: str, raw_req_id: Any) -> dict[str, Any]:
        return {
            "event_type": EVENT_TOOLCALL_ATTESTATION,
            "resource_id": "scope:run-1:agent-1",
            "details": {
                "attestation_ref": f"sha256:{att_hex}",
                "intent_digest": f"sha256:{intent_hex}",
                "scope_id": "scope:run-1:agent-1",
                "server_name": "filesystem",
                "tool_name": "read_file",
                "request_id": raw_req_id,
            },
        }

    distinct_receipt: dict[str, Any] = {
        "events": [
            _att_event("1" * 64, "a" * 64, 1),
            _att_event("2" * 64, "b" * 64, "1"),
            _att_event("3" * 64, "c" * 64, " 1 "),
        ]
    }
    check_ok, _ = verifier.verify_toolcall_effects(distinct_receipt)
    assert check_ok.ok is True, check_ok.detail

    duplicate_int_receipt: dict[str, Any] = {
        "events": [
            _att_event("1" * 64, "a" * 64, 1),
            _att_event("4" * 64, "d" * 64, 1),
        ]
    }
    check_dup, _ = verifier.verify_toolcall_effects(duplicate_int_receipt)
    assert check_dup.ok is False
    assert "duplicate request_id" in check_dup.detail
