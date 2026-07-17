"""Broker <-> grant integration tests (issue #2516).

The secrets broker in grant-enforcing mode refuses to mint a token unless a
verifiable, chain-anchored grant exists for the (task id, secret name) pair;
the refusal is itself a chain event. A minted token inherits the grant's task
id, audience, and expiry, and the token id is recorded in the grant lifecycle
so token-to-grant resolution works offline. ``resolve()`` refuses a token
presented outside its granted audience, and audience, expiry, and revocation
refusals are all recorded as chain-anchored records, not only in-process
callbacks. Revocation appends a signed revocation record referencing the grant,
and a run's full credential history reconstructs offline from the chain alone.
"""

from __future__ import annotations

import pytest

from bernstein.core.identity import grants
from bernstein.core.security.secrets_broker import (
    BrokerConfig,
    SecretsBackend,
    SecretsBroker,
    SecretsBrokerError,
    clear_redaction_registry,
)


class _MemoryBackend(SecretsBackend):
    name = "memory"

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = dict(secrets)

    def read(self, secret_name: str) -> str:
        if secret_name not in self._secrets:
            raise SecretsBrokerError(f"memory: no entry for {secret_name!r}")
        return self._secrets[secret_name]


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_redaction_registry()
    yield
    clear_redaction_registry()


def _build(tmp_path, *, require_grant: bool = True, clock_value: float = 1_000.0):
    now = [clock_value]

    def clock() -> float:
        return now[0]

    signer = grants.GrantSigner.generate(issuer="manager:test")
    ledger = grants.GrantLedger(root=tmp_path, key=b"k" * 32, signer=signer)
    backend = _MemoryBackend({"K": "raw-value-K", "J": "raw-value-J"})
    cfg = BrokerConfig(backend="file_encrypted", ttl_seconds_default=900)
    broker = SecretsBroker(
        backend,
        config=cfg,
        clock=clock,
        grant_ledger=ledger,
        require_grant=require_grant,
    )
    return broker, ledger, now


class TestMintRequiresGrant:
    def test_mint_without_grant_refuses_and_records_chain_event(self, tmp_path) -> None:
        broker, ledger, _ = _build(tmp_path)
        with pytest.raises(SecretsBrokerError, match="grant"):
            broker.mint(secret_name="K", task_id="t-1", run_id="run-1")
        # The refusal is itself a chain-anchored record.
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid
        assert result.records[-1].kind == grants.GRANT_REFUSED
        assert result.records[-1].reason

    def test_mint_with_verifiable_grant_inherits_scope_and_records_exchange(self, tmp_path) -> None:
        broker, ledger, _ = _build(tmp_path)
        grant = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="api.anthropic.com",
            expiry=5_000,
        )
        token = broker.mint(secret_name="K", task_id="t-1", grant=grant)
        assert token.task_id == "t-1"
        assert token.audience == "api.anthropic.com"
        # Token inherits the grant expiry rather than the config TTL default.
        assert token.expires_at == 5_000
        # The exchange is recorded, binding token id to the grant.
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        life = result.lifecycles()
        assert token.token_id in life[grant.grant_id]["token_ids"]

    def test_mint_rejects_grant_for_wrong_secret(self, tmp_path) -> None:
        broker, ledger, _ = _build(tmp_path)
        grant = ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=5_000)
        with pytest.raises(SecretsBrokerError, match="grant"):
            broker.mint(secret_name="J", task_id="t-1", grant=grant)

    def test_mint_rejects_grant_for_wrong_task(self, tmp_path) -> None:
        broker, ledger, _ = _build(tmp_path)
        grant = ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=5_000)
        with pytest.raises(SecretsBrokerError, match="grant"):
            broker.mint(secret_name="K", task_id="t-OTHER", grant=grant)

    def test_mint_rejects_already_revoked_grant(self, tmp_path) -> None:
        broker, ledger, _ = _build(tmp_path)
        grant = ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=5_000)
        ledger.revoke_grant(run_id="run-1", grant_id=grant.grant_id, reason="pre-revoked")
        with pytest.raises(SecretsBrokerError, match="grant"):
            broker.mint(secret_name="K", task_id="t-1", grant=grant)


class TestResolveAudienceScoping:
    def test_resolve_refuses_wrong_audience_and_records_event(self, tmp_path) -> None:
        broker, ledger, _ = _build(tmp_path)
        grant = ledger.issue_grant(
            run_id="run-1", task_id="t-1", secret_name="K", audience="api.anthropic.com", expiry=5_000
        )
        token = broker.mint(secret_name="K", task_id="t-1", grant=grant)
        # Correct audience resolves.
        assert broker.resolve(token.value, audience="api.anthropic.com") == "raw-value-K"
        # Wrong audience is refused.
        with pytest.raises(SecretsBrokerError, match="audience"):
            broker.resolve(token.value, audience="evil.example")
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        refusals = [r for r in result.records if r.kind == grants.GRANT_REFUSED]
        assert any("audience" in r.reason for r in refusals)

    def test_resolve_without_audience_is_allowed(self, tmp_path) -> None:
        broker, ledger, _ = _build(tmp_path)
        grant = ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=5_000)
        token = broker.mint(secret_name="K", task_id="t-1", grant=grant)
        # A caller that does not assert an audience still resolves (broker-internal path).
        assert broker.resolve(token.value) == "raw-value-K"


class TestRevocationChainEvent:
    def test_revoke_writes_signed_revocation_record(self, tmp_path) -> None:
        broker, ledger, _ = _build(tmp_path)
        grant = ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=5_000)
        token = broker.mint(secret_name="K", task_id="t-1", grant=grant)
        assert broker.revoke(token.token_id, reason="task-exit") is True
        with pytest.raises(SecretsBrokerError):
            broker.resolve(token.value)
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.lifecycles()[grant.grant_id]["revoked"] is True

    def test_full_history_reconstructs_offline(self, tmp_path) -> None:
        broker, ledger, _ = _build(tmp_path)
        grant = ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=5_000)
        token = broker.mint(secret_name="K", task_id="t-1", grant=grant)
        broker.revoke(token.token_id, reason="task-exit")
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        kinds = [r.kind for r in result.records]
        assert grants.GRANT_ISSUED in kinds
        assert grants.GRANT_EXCHANGED in kinds
        assert grants.GRANT_REVOKED in kinds
        assert result.valid


class TestLegacyModeUnchanged:
    def test_non_enforcing_broker_mints_without_grant(self, tmp_path) -> None:
        broker, _, _ = _build(tmp_path, require_grant=False)
        token = broker.mint(secret_name="K", task_id="t-1")
        assert broker.resolve(token.value) == "raw-value-K"
