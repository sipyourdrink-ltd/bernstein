"""Shared real-chain builders for run-attestation CLI and security tests."""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.identity.agent_card import AgentIdentityCard
from bernstein.core.lineage.identity import AgentCard
from bernstein.core.security.agent_card_signer import generate_ed25519_keypair, sign_agent_card
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.identity_spawn_anchor import IdentitySpawnAnchor
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter
from bernstein.core.security.native_toolcall_evidence import NativeToolCallEvidenceProvider
from bernstein.core.security.toolcall_identity import LineageToolCallIdentitySigner
from bernstein.core.security.toolcall_interlock import ToolCallIntent

HMAC_KEY = b"r" * 32
SIGNING_SEED = b"s" * 32


def kms(tmp_path: Path, *, seed: bytes = SIGNING_SEED, kid: str = "run-receipt-key") -> FileBasedKMSAdapter:
    """Write a deterministic receipt-signing key and return its adapter."""
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


def intent(request_id: int = 1) -> ToolCallIntent:
    """Return one deterministic tool-call intent for the test run."""
    return ToolCallIntent.from_request(
        scope_id="scope:run-1:agent-1",
        server_name="filesystem",
        method="tools/call",
        tool_name="read_file",
        request_id=request_id,
        span_id=f"span-{request_id}",
        arguments={"path": "/secret-that-must-not-be-retained"},
    )


def anchored_provider(tmp_path: Path) -> NativeToolCallEvidenceProvider:
    """Create a provider whose run identity is anchored in a real audit chain."""
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
