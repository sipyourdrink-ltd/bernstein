"""External secret stores as broker backends (issue #4984).

The broker treats an external secret store as a backend behind one contract:
resolve a named secret, mint a short-lived credential, report a revocation.
The credential *value* never enters the grant chain -- what the chain records
is the grant, the identity of the store that issued the credential, the
audience, and the expiry. Grant enforcement is identical for an external
backend and for the file backend, and a credential bound into the environment
for one step is absent from the environment on both sides of that step.
"""

from __future__ import annotations

import os

import pytest

from bernstein.core.identity import grants
from bernstein.core.security.external_secret_store import (
    ExternalCredential,
    ExternalSecretStore,
    ExternalStoreError,
    SecretDescriptor,
    SecretRef,
)
from bernstein.core.security.secrets_broker import (
    BrokerConfig,
    ExternalStoreBackend,
    SecretsBroker,
    SecretsBrokerError,
    clear_redaction_registry,
    get_redactable_values,
)

#: Distinctive so a chain scan can prove the value never lands on disk.
UPSTREAM_VALUE = "UPSTREAM-SECRET-b7f3c1a9-do-not-record"
SECRET_PATH = "prod/anthropic"
SECRET_NAME = f"fake-vault:{SECRET_PATH}"


class _FakeExternalStore(ExternalSecretStore):
    """In-memory stand-in for an operator's own secret store."""

    store_id = "fake-vault"

    def __init__(self, clock) -> None:
        self._clock = clock
        self._entries = {SECRET_PATH: UPSTREAM_VALUE}
        self._revoked: set[str] = set()
        self.mint_calls = 0
        self.revocation_calls = 0

    # -- operator-side helpers ------------------------------------------
    def revoke_upstream(self, path: str) -> None:
        self._revoked.add(path)

    # -- contract --------------------------------------------------------
    def resolve(self, path: str) -> SecretDescriptor:
        if path not in self._entries:
            raise ExternalStoreError(f"fake-vault: no secret at {path!r}")
        return SecretDescriptor(
            store_id=self.store_id,
            upstream_id=f"{path}#v1",
            revoked=path in self._revoked,
        )

    def mint_credential(self, path: str, *, audience: str, ttl_seconds: int) -> ExternalCredential:
        if path in self._revoked:
            raise ExternalStoreError("fake-vault: upstream secret is revoked")
        self.mint_calls += 1
        return ExternalCredential(
            value=f"{self._entries[path]}/lease-{self.mint_calls}",
            expires_at=self._clock() + ttl_seconds,
            upstream_id=f"{path}#v1",
            audience=audience,
        )

    def report_revocation(self, path: str, *, upstream_id: str) -> bool:
        self.revocation_calls += 1
        return path in self._revoked


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_redaction_registry()
    yield
    clear_redaction_registry()


def _build(tmp_path, *, require_grant: bool = True):
    now = [1_000.0]

    def clock() -> float:
        return now[0]

    store = _FakeExternalStore(clock)
    signer = grants.GrantSigner.generate(issuer="manager:test")
    ledger = grants.GrantLedger(root=tmp_path, key=b"k" * 32, signer=signer)
    backend = ExternalStoreBackend(store=store)
    cfg = BrokerConfig(backend="external", ttl_seconds_default=900)
    broker = SecretsBroker(
        backend,
        config=cfg,
        clock=clock,
        grant_ledger=ledger,
        require_grant=require_grant,
    )
    return broker, ledger, store, now


def _issue(ledger, *, expiry: int = 0):
    return ledger.issue_grant(
        run_id="run-1",
        task_id="t-1",
        secret_name=SECRET_NAME,
        audience="api.anthropic.com",
        expiry=expiry,
    )


class TestContractEndToEnd:
    def test_external_backend_satisfies_broker_contract_end_to_end(self, tmp_path) -> None:
        """1. Mint, resolve, and revoke work through an out-of-core store."""
        broker, ledger, store, _ = _build(tmp_path)
        grant = _issue(ledger)
        token = broker.mint(secret_name=SECRET_NAME, task_id="t-1", grant=grant)

        assert store.mint_calls == 1
        assert token.audience == "api.anthropic.com"
        # The agent sees the broker token, never the store's credential.
        assert UPSTREAM_VALUE not in token.value
        assert broker.resolve(token.value, audience="api.anthropic.com").startswith(UPSTREAM_VALUE)

        assert broker.revoke(token.token_id) is True
        with pytest.raises(SecretsBrokerError, match="revoked"):
            broker.resolve(token.value)

    def test_store_reported_expiry_caps_the_token_window(self, tmp_path) -> None:
        """2. A credential the store issues for less than the config TTL shortens the token."""
        broker, ledger, store, now = _build(tmp_path)
        grant = _issue(ledger)
        token = broker.mint(
            secret_name=SECRET_NAME,
            task_id="t-1",
            ttl_seconds=60,
            grant=grant,
        )
        assert token.expires_at == pytest.approx(now[0] + 60)
        now[0] += 61
        with pytest.raises(SecretsBrokerError, match="expired"):
            broker.resolve(token.value)

    def test_exchange_record_names_the_issuing_store(self, tmp_path) -> None:
        """3. The chain says which store issued the credential."""
        broker, ledger, _store, _ = _build(tmp_path)
        grant = _issue(ledger)
        broker.mint(secret_name=SECRET_NAME, task_id="t-1", grant=grant)
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid
        exchanged = [r for r in result.records if r.kind == grants.GRANT_EXCHANGED]
        assert exchanged, "mint must append a grant_exchanged record"
        assert "fake-vault" in exchanged[-1].reason


class TestNoSecretValueOnChain:
    def test_no_secret_value_appears_in_any_chain_event(self, tmp_path) -> None:
        """4. Load-bearing: scan every chain record for the credential value."""
        broker, ledger, store, _ = _build(tmp_path)
        grant = _issue(ledger)
        token = broker.mint(secret_name=SECRET_NAME, task_id="t-1", grant=grant)
        broker.revoke(token.token_id)
        # A refusal path too, so every record kind is present in the fixture.
        with pytest.raises(SecretsBrokerError):
            broker.mint(secret_name=SECRET_NAME, task_id="t-unknown", run_id="run-1")

        raw = ledger.receipt_path("run-1").read_text(encoding="utf-8")
        assert raw.strip(), "fixture chain must not be empty"
        assert UPSTREAM_VALUE not in raw
        assert token.value not in raw
        # The backing-store path is a low-entropy name and is salted, not clear.
        assert SECRET_PATH not in raw


class TestGrantInvariantHoldsForExternalBackends:
    def test_mint_without_verifying_grant_is_refused_for_external_backend(self, tmp_path) -> None:
        """5. Same refusal as the file backend, and the store is never touched."""
        broker, ledger, store, _ = _build(tmp_path)
        with pytest.raises(SecretsBrokerError, match="grant"):
            broker.mint(secret_name=SECRET_NAME, task_id="t-1", run_id="run-1")
        assert store.mint_calls == 0
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid
        assert result.records[-1].kind == grants.GRANT_REFUSED

    def test_mint_with_mismatched_grant_is_refused_for_external_backend(self, tmp_path) -> None:
        """6. A grant for another secret does not authorize this one."""
        broker, ledger, store, _ = _build(tmp_path)
        other = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="fake-vault:prod/other",
            audience="api.anthropic.com",
        )
        with pytest.raises(SecretsBrokerError, match="secret_mismatch"):
            broker.mint(secret_name=SECRET_NAME, task_id="t-1", grant=other)
        assert store.mint_calls == 0


class TestUpstreamRevocation:
    def test_revoked_upstream_secret_makes_subsequent_mints_fail(self, tmp_path) -> None:
        """7. Revoking in the operator's own store stops the broker minting."""
        broker, ledger, store, _ = _build(tmp_path)
        grant = _issue(ledger)
        broker.mint(secret_name=SECRET_NAME, task_id="t-1", grant=grant)

        store.revoke_upstream(SECRET_PATH)

        with pytest.raises(SecretsBrokerError, match="revoked"):
            broker.mint(secret_name=SECRET_NAME, task_id="t-1", grant=grant)
        assert store.mint_calls == 1
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid
        assert any("upstream_revoked" in r.reason for r in result.records if r.kind == grants.GRANT_REFUSED)


class TestScopedEnvironmentBinding:
    def test_bound_credential_is_absent_from_the_environment_outside_the_step(self, tmp_path) -> None:
        """8. Dump the environment before and after; the value is absent both times."""
        broker, ledger, _store, _ = _build(tmp_path)
        grant = _issue(ledger)
        env: dict[str, str] = {"PATH": "/usr/bin"}

        before = dict(env)
        with broker.bind_scoped(
            secret_name=SECRET_NAME,
            task_id="t-1",
            env_var="ANTHROPIC_API_KEY",
            grant=grant,
            env=env,
        ) as token:
            inside = dict(env)
        after = dict(env)

        assert "ANTHROPIC_API_KEY" not in before
        assert inside["ANTHROPIC_API_KEY"] == token.value
        assert "ANTHROPIC_API_KEY" not in after
        assert token.value not in " ".join(after.values())
        # The token is revoked and its value is no longer redaction-registered.
        assert token.value not in get_redactable_values()
        with pytest.raises(SecretsBrokerError):
            broker.resolve(token.value)

    def test_bind_scoped_restores_a_pre_existing_variable(self, tmp_path) -> None:
        """9. Binding does not clobber an operator-set variable of the same name."""
        broker, ledger, _store, _ = _build(tmp_path)
        grant = _issue(ledger)
        env = {"ANTHROPIC_API_KEY": "operator-set"}
        with broker.bind_scoped(
            secret_name=SECRET_NAME,
            task_id="t-1",
            env_var="ANTHROPIC_API_KEY",
            grant=grant,
            env=env,
        ):
            assert env["ANTHROPIC_API_KEY"] != "operator-set"
        assert env["ANTHROPIC_API_KEY"] == "operator-set"

    def test_bind_scoped_defaults_to_the_process_environment(self, tmp_path) -> None:
        """10. With no explicit mapping the binding targets os.environ and is undone."""
        broker, ledger, _store, _ = _build(tmp_path)
        grant = _issue(ledger)
        var = "BERNSTEIN_TEST_BOUND_SECRET"
        assert var not in os.environ
        try:
            with broker.bind_scoped(
                secret_name=SECRET_NAME,
                task_id="t-1",
                env_var=var,
                grant=grant,
            ) as token:
                assert os.environ[var] == token.value
        finally:
            os.environ.pop(var, None)
        assert var not in os.environ


class TestReferenceShape:
    def test_reference_names_a_store_without_carrying_a_value(self) -> None:
        """11. A spec names a secret by opaque reference: store plus store-native path."""
        ref = SecretRef.parse(SECRET_NAME)
        assert ref.store == "fake-vault"
        assert ref.path == SECRET_PATH
        assert str(ref) == SECRET_NAME
        with pytest.raises(ExternalStoreError):
            SecretRef.parse("no-store-separator")

    def test_credential_repr_does_not_expose_the_value(self) -> None:
        """12. A credential that reaches a log or traceback shows no value."""
        cred = ExternalCredential(value=UPSTREAM_VALUE, expires_at=1.0, upstream_id="u")
        assert UPSTREAM_VALUE not in repr(cred)
