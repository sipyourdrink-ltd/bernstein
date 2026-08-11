"""Adversarial contracts for the provisional #2931 run receipt slice."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.lineage.identity import AgentCard
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair, sign_agent_card
from bernstein.core.security.agent_identity import AgentIdentityCard
from bernstein.core.security.audit_chain import (
    EVENT_IDENTITY_SPAWN_ATTESTATION,
    AuditChainStore,
)
from bernstein.core.security.identity_spawn_anchor import IdentitySpawnAnchor
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter
from bernstein.core.security.native_toolcall_evidence import NativeToolCallEvidenceProvider
from bernstein.core.security.run_attestation_receipt import (
    RUN_ATTESTATION_RECEIPT_TYPE,
    RunAttestationReceiptError,
    build_run_attestation_receipt,
    verify_run_attestation_projection,
)
from bernstein.core.security.toolcall_identity import LineageToolCallIdentitySigner
from bernstein.core.security.toolcall_interlock import AttestationVerdict, ToolCallIntent

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFIER_SCRIPT = REPO_ROOT / "tools" / "verify_audit_receipt.py"
HMAC_KEY = b"r" * 32
SIGNING_SEED = b"s" * 32


def _kms(tmp_path: Path, *, seed: bytes = SIGNING_SEED, kid: str = "run-receipt-key") -> FileBasedKMSAdapter:
    tmp_path.mkdir(parents=True, exist_ok=True)
    key_path = tmp_path / "receipt-signing.pem"
    key_path.write_bytes(
        Ed25519PrivateKey.from_private_bytes(seed).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return FileBasedKMSAdapter(key_path, kid=kid)


def _load_standard_verifier() -> Any:
    spec = importlib.util.spec_from_file_location("verify_run_attestation_audit_receipt", VERIFIER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _standard_verify(path: Path, *args: str) -> tuple[int, str]:
    verifier = _load_standard_verifier()
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = verifier.main(["--receipt", str(path), *args])
    return rc, stdout.getvalue()


def _intent(request_id: int = 1) -> ToolCallIntent:
    return ToolCallIntent.from_request(
        scope_id="scope:run-1:agent-1",
        server_name="filesystem",
        method="tools/call",
        tool_name="read_file",
        request_id=request_id,
        span_id=f"span-{request_id}",
        arguments={"path": "/secret-that-must-not-be-retained"},
    )


def _anchored_provider(tmp_path: Path) -> NativeToolCallEvidenceProvider:
    chain = AuditChainStore(tmp_path / "audit", key=HMAC_KEY)
    card_private, card_public = generate_ed25519_keypair()
    tool_private, tool_public = generate_ed25519_keypair()
    card = AgentIdentityCard(
        agent_id="agent-1",
        role="coder",
        adapter="codex",
        model="gpt",
        created_at=100,
        expires_at=200,
    )
    card_signature = sign_agent_card(card, card_private, kid="spawn-key")
    identity = IdentitySpawnAnchor(chain, {"spawn-key": card_public}, clock=lambda: 150).anchor(
        run_id="run-1",
        card=card,
        signature=card_signature,
        run_journal_head="journal:fixed",
        tool_signing_card=AgentCard("agent-1", "tool-key", tool_public.decode()),
    )
    return NativeToolCallEvidenceProvider(
        chain,
        run_identity=identity,
        signer=LineageToolCallIdentitySigner(tool_private.decode(), "tool-key"),
        run_journal_head=lambda: "journal:fixed",
        clock_ns=lambda: 123_000_000_001,
    )


@pytest_asyncio.fixture
async def receipt_env(tmp_path: Path) -> dict[str, Any]:
    provider = _anchored_provider(tmp_path)
    provider.chain.log(
        event_type="other.run.event",
        actor="other-agent",
        resource_type="run",
        resource_id="run-other",
        details={"run_id": "run-other", "value": "interleaved"},
    )
    await provider.prepare_dispatch(_intent())
    provider.chain.log(
        event_type="run.progress",
        actor="agent-1",
        resource_type="run",
        resource_id="run-1",
        details={"run_id": "run-1", "progress": 1},
    )
    receipt = build_run_attestation_receipt(
        tmp_path / "audit",
        run_id="run-1",
        key=HMAC_KEY,
        kms_adapter=_kms(tmp_path / "signing"),
        output_dir=tmp_path / "out",
    )
    assert receipt.receipt_path is not None
    return {"provider": provider, "receipt": receipt, "tmp": tmp_path}


@pytest.mark.asyncio
async def test_valid_range_is_standard_verifiable_but_whole_run_stays_observed(receipt_env: dict[str, Any]) -> None:
    receipt = receipt_env["receipt"]
    assert receipt.receipt["receipt_type"] == RUN_ATTESTATION_RECEIPT_TYPE
    assert receipt.dispatch_evidence_verdict is AttestationVerdict.COMPLETE
    assert receipt.whole_run_verdict is AttestationVerdict.OBSERVED
    assert receipt.receipt["run_attestation"]["terminal_boundary"] is None
    assert receipt.receipt["run_attestation"]["provisional"] is True

    semantic = verify_run_attestation_projection(receipt.receipt)
    assert semantic.ok, semantic.errors
    rc, output = _standard_verify(receipt.receipt_path)
    assert rc == 0, output
    assert "OVERALL: PASS" in output


@pytest.mark.asyncio
async def test_projection_begins_at_anchor_and_keeps_interleaved_events(receipt_env: dict[str, Any]) -> None:
    events = receipt_env["receipt"].receipt["events"]
    assert events[0]["event_type"] == EVENT_IDENTITY_SPAWN_ATTESTATION
    assert any(event["event_type"] == "other.run.event" for event in events)
    assert [event["details"]["_original_hmac"] for event in events]


@pytest.mark.asyncio
async def test_timestamp_is_observational_not_a_membership_rule(receipt_env: dict[str, Any]) -> None:
    receipt = receipt_env["receipt"].receipt
    assert receipt["range"]["selection"] == "authenticated-chain-position"
    assert receipt["range"]["source_start_hmac"] == receipt["run_attestation"]["identity_anchor_hmac"]
    assert receipt["range"]["source_end_hmac"] == receipt["run_attestation"]["through_hmac"]


@pytest.mark.asyncio
async def test_explicit_authenticated_head_excludes_only_the_suffix(tmp_path: Path) -> None:
    provider = _anchored_provider(tmp_path)
    await provider.prepare_dispatch(_intent())
    boundary = provider.chain.query(include_archived=True)[-1].hmac
    provider.chain.log(
        event_type="later.event",
        actor="agent-1",
        resource_type="run",
        resource_id="run-1",
        details={"run_id": "run-1"},
    )
    receipt = build_run_attestation_receipt(
        tmp_path / "audit",
        run_id="run-1",
        key=HMAC_KEY,
        kms_adapter=_kms(tmp_path / "signing"),
        through_hmac=boundary,
        write=False,
    )
    assert receipt.through_hmac == boundary
    assert all(event["event_type"] != "later.event" for event in receipt.receipt["events"])


@pytest.mark.asyncio
async def test_boundary_before_anchor_is_rejected(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=HMAC_KEY)
    before = chain.log(
        event_type="before.run",
        actor="operator",
        resource_type="system",
        resource_id="system",
    ).hmac
    _anchored_provider(tmp_path)
    with pytest.raises(RunAttestationReceiptError, match="precedes"):
        build_run_attestation_receipt(
            tmp_path / "audit",
            run_id="run-1",
            key=HMAC_KEY,
            kms_adapter=_kms(tmp_path / "signing"),
            through_hmac=before,
            write=False,
        )


@pytest.mark.asyncio
async def test_missing_or_duplicate_anchor_fails_closed(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=HMAC_KEY)
    chain.log(event_type="plain", actor="a", resource_type="run", resource_id="run-1")
    with pytest.raises(RunAttestationReceiptError, match="found 0"):
        build_run_attestation_receipt(
            tmp_path / "audit",
            run_id="run-1",
            key=HMAC_KEY,
            kms_adapter=_kms(tmp_path / "signing"),
            write=False,
        )

    provider = _anchored_provider(tmp_path / "duplicate")
    provider.chain.log_with_prev_digest(
        event_type=EVENT_IDENTITY_SPAWN_ATTESTATION,
        actor="operator",
        resource_type="run",
        resource_id="run-1",
        details={"run_id": "run-1"},
    )
    with pytest.raises(RunAttestationReceiptError, match="found 2"):
        build_run_attestation_receipt(
            tmp_path / "duplicate" / "audit",
            run_id="run-1",
            key=HMAC_KEY,
            kms_adapter=_kms(tmp_path / "duplicate-signing"),
            write=False,
        )


@pytest.mark.asyncio
async def test_empty_and_legacy_runs_are_representable_but_never_complete(tmp_path: Path) -> None:
    _anchored_provider(tmp_path / "empty")
    empty = build_run_attestation_receipt(
        tmp_path / "empty" / "audit",
        run_id="run-1",
        key=HMAC_KEY,
        kms_adapter=_kms(tmp_path / "empty-signing"),
        write=False,
    )
    assert empty.dispatch_evidence_verdict is AttestationVerdict.OBSERVED

    chain = AuditChainStore(tmp_path / "legacy" / "audit", key=HMAC_KEY)
    private, public = generate_ed25519_keypair()
    card = AgentIdentityCard(
        agent_id="agent-1",
        role="coder",
        adapter="codex",
        model="gpt",
        created_at=100,
        expires_at=200,
    )
    IdentitySpawnAnchor(chain, {"spawn-key": public}, clock=lambda: 150).anchor(
        run_id="run-1",
        card=card,
        signature=sign_agent_card(card, private, kid="spawn-key"),
        run_journal_head="journal:fixed",
    )
    legacy = build_run_attestation_receipt(
        tmp_path / "legacy" / "audit",
        run_id="run-1",
        key=HMAC_KEY,
        kms_adapter=_kms(tmp_path / "legacy-signing"),
        write=False,
    )
    assert legacy.whole_run_verdict is AttestationVerdict.OBSERVED
    assert verify_run_attestation_projection(legacy.receipt).ok


@pytest.mark.asyncio
async def test_stripped_identity_envelope_downgrades_dispatch_evidence(receipt_env: dict[str, Any]) -> None:
    mutated = deepcopy(receipt_env["receipt"].receipt)
    attestation = next(event for event in mutated["events"] if event["event_type"] == "toolcall.attestation")
    del attestation["details"]["identity_envelope"]
    result = verify_run_attestation_projection(mutated)
    assert result.dispatch_evidence_verdict is AttestationVerdict.OBSERVED
    assert not result.ok


@pytest.mark.asyncio
async def test_serialized_complete_claim_is_refused(receipt_env: dict[str, Any]) -> None:
    mutated = deepcopy(receipt_env["receipt"].receipt)
    mutated["run_attestation"]["whole_run_verdict"] = "complete"
    mutated["run_attestation"]["provisional"] = False
    result = verify_run_attestation_projection(mutated)
    assert not result.ok
    assert any("unsupported whole-run" in error for error in result.errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("drop", ["prefix", "middle", "suffix"])
async def test_deleting_any_declared_range_part_breaks_standard_verification(
    receipt_env: dict[str, Any], drop: str
) -> None:
    data = deepcopy(receipt_env["receipt"].receipt)
    if drop == "prefix":
        data["events"] = data["events"][1:]
    elif drop == "middle":
        del data["events"][len(data["events"]) // 2]
    else:
        data["events"] = data["events"][:-1]
    path = receipt_env["tmp"] / f"dropped-{drop}.json"
    path.write_text(json.dumps(data))
    rc, output = _standard_verify(path)
    assert rc == 1
    assert "[FAIL] subject_binding" in output


@pytest.mark.asyncio
async def test_source_chain_byte_flip_prevents_construction(tmp_path: Path) -> None:
    _anchored_provider(tmp_path)
    source = next((tmp_path / "audit").glob("*.jsonl"))
    raw = source.read_bytes()
    assert b"agent-1" in raw
    source.write_bytes(raw.replace(b"agent-1", b"agent-X", 1))
    with pytest.raises(RunAttestationReceiptError, match="source audit chain verification failed"):
        build_run_attestation_receipt(
            tmp_path / "audit",
            run_id="run-1",
            key=HMAC_KEY,
            kms_adapter=_kms(tmp_path / "signing"),
            write=False,
        )


@pytest.mark.asyncio
async def test_build_is_read_only_with_respect_to_source_chain(tmp_path: Path) -> None:
    _anchored_provider(tmp_path)
    before = {path.name: path.read_bytes() for path in (tmp_path / "audit").glob("*") if path.is_file()}
    build_run_attestation_receipt(
        tmp_path / "audit",
        run_id="run-1",
        key=HMAC_KEY,
        kms_adapter=_kms(tmp_path / "signing"),
        write=False,
    )
    after = {path.name: path.read_bytes() for path in (tmp_path / "audit").glob("*") if path.is_file()}
    assert after == before


@pytest.mark.asyncio
async def test_two_builds_are_byte_identical_and_contain_no_raw_arguments(receipt_env: dict[str, Any]) -> None:
    first = receipt_env["receipt"]
    second = build_run_attestation_receipt(
        receipt_env["tmp"] / "audit",
        run_id="run-1",
        key=HMAC_KEY,
        kms_adapter=_kms(receipt_env["tmp"] / "signing-2"),
        through_hmac=first.through_hmac,
        write=False,
    )
    assert first.receipt_bytes == second.receipt_bytes
    assert b"/secret-that-must-not-be-retained" not in first.receipt_bytes
    assert b"PRIVATE KEY" not in first.receipt_bytes


@pytest.mark.asyncio
async def test_pinned_signer_is_distinct_from_embedded_key_self_consistency(receipt_env: dict[str, Any]) -> None:
    receipt = receipt_env["receipt"]
    correct = receipt_env["tmp"] / "correct.jwk"
    correct.write_text(json.dumps(receipt.receipt["signing"]["public_key_jwk"]))
    rc, output = _standard_verify(receipt.receipt_path, "--jwk", str(correct))
    assert rc == 0, output

    wrong_kms = _kms(receipt_env["tmp"] / "wrong", seed=b"w" * 32, kid="wrong")
    wrong = receipt_env["tmp"] / "wrong.jwk"
    wrong.write_text(json.dumps(wrong_kms.public_key_jwk()))
    rc, output = _standard_verify(receipt.receipt_path, "--jwk", str(wrong))
    assert rc == 1
    assert "[FAIL] public_key" in output


@pytest.mark.asyncio
async def test_failed_build_does_not_repair_or_append_source(tmp_path: Path) -> None:
    _anchored_provider(tmp_path)
    source = next((tmp_path / "audit").glob("*.jsonl"))
    source.write_bytes(source.read_bytes() + b"not-json\n")
    before = source.read_bytes()
    with pytest.raises(RunAttestationReceiptError):
        build_run_attestation_receipt(
            tmp_path / "audit",
            run_id="run-1",
            key=HMAC_KEY,
            kms_adapter=_kms(tmp_path / "signing"),
            write=False,
        )
    assert source.read_bytes() == before
