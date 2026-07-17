"""Self-contained packaging check for the SPIFFE credential path (issue #2516).

Runs off the INSTALLED bernstein wheel (not the repo pytest tree) so it needs no
test-only plugins or config. Two modes:

* ``--mode no-extra``: proves the wheel imports and the grant / broker paths
  work end to end with the ``spiffe`` extra ABSENT (default Ed25519 identity);
* ``--mode extra``: proves that with the ``spiffe`` extra installed the grant
  issuer resolves to the workload SPIFFE ID via an injected Workload API factory
  (no live SPIRE agent required).

Exits non-zero on any failed assertion; used by ``.github/workflows/
spiffe-extra-e2e.yml``.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
from pathlib import Path

_HMAC_KEY = b"k" * 32


def _check_no_extra() -> None:
    assert importlib.util.find_spec("spiffe") is None, "spiffe extra unexpectedly present"
    print("spiffe extra absent: OK")

    from bernstein.core.identity import grants
    from bernstein.core.identity.spiffe.grant_identity import spiffe_grant_issuer
    from bernstein.core.identity.spiffe.workload_api import spiffe_extra_available

    assert spiffe_extra_available() is False, "spiffe_extra_available() should be False"
    assert spiffe_grant_issuer() is None, "issuer should fall back to None without the extra"
    print("default Ed25519 identity path active: OK")

    from bernstein.core.security.secrets_broker import (
        BrokerConfig,
        SecretsBackend,
        SecretsBroker,
        SecretsBrokerError,
    )

    class _Mem(SecretsBackend):
        name = "memory"

        def read(self, secret_name: str) -> str:
            return "raw-" + secret_name

    root = Path(tempfile.mkdtemp())
    signer = grants.GrantSigner.generate(issuer="ci:manager")
    ledger = grants.GrantLedger(root=root, key=_HMAC_KEY, signer=signer)
    broker = SecretsBroker(
        _Mem(),
        config=BrokerConfig(backend="file_encrypted"),
        grant_ledger=ledger,
        require_grant=True,
    )

    # Refuse without a grant; the refusal is a chain record.
    try:
        broker.mint(secret_name="K", task_id="t", run_id="run")
        raise AssertionError("mint should refuse without a grant")
    except SecretsBrokerError:
        pass

    grant = ledger.issue_grant(run_id="run", task_id="t", secret_name="K", audience="aud", expiry=2_000_000_000)
    token = broker.mint(secret_name="K", task_id="t", grant=grant)
    assert token.audience == "aud", token.audience
    assert token.value != "raw-K", "token must not equal the raw secret"
    assert broker.resolve(token.value, audience="aud") == "raw-K"

    try:
        broker.resolve(token.value, audience="evil")
        raise AssertionError("resolve should refuse a wrong audience")
    except SecretsBrokerError:
        pass

    assert broker.revoke(token.token_id, reason="task-exit") is True
    result = grants.verify_grant_chain(root=root, run_id="run", key=_HMAC_KEY)
    assert result.valid, result.errors
    kinds = {r.kind for r in result.records}
    assert {grants.GRANT_ISSUED, grants.GRANT_EXCHANGED, grants.GRANT_REVOKED} <= kinds, kinds
    print("grant issue/exchange/revoke reconstructs offline: OK")

    # Tamper detection: flip the expiry on disk and confirm verification fails.
    path = ledger.receipt_path("run")
    path.write_text(path.read_text().replace("2000000000", "9999999999", 1), encoding="utf-8")
    assert grants.verify_grant_chain(root=root, run_id="run", key=_HMAC_KEY).valid is False
    print("tamper detection: OK")
    print("NO-EXTRA PACKAGED PATH OK")


def _check_extra() -> None:
    from bernstein.core.identity.spiffe import grant_identity
    from bernstein.core.identity.spiffe.svid import X509Svid
    from bernstein.core.identity.spiffe.workload_api import spiffe_extra_available

    assert spiffe_extra_available() is True, "spiffe extra should be importable"
    print("spiffe extra present: OK")

    def _factory(_socket: str | None) -> X509Svid:
        return X509Svid(
            spiffe_id="spiffe://example.org/bernstein/inst/agent-1",
            cert_chain_pem=b"leaf",
            private_key_pem=b"key",
            bundle_pem=b"bundle",
            expires_at=2_000_000_000.0,
        )

    issuer = grant_identity.spiffe_grant_issuer(client_factory=_factory)
    assert issuer == "spiffe://example.org/bernstein/inst/agent-1", issuer
    print("grant issuer resolves to SPIFFE ID via Workload API: OK")
    print("EXTRA-PRESENT PACKAGED PATH OK")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("no-extra", "extra"), required=True)
    args = parser.parse_args()
    if args.mode == "no-extra":
        _check_no_extra()
    else:
        _check_extra()
    return 0


if __name__ == "__main__":
    sys.exit(main())
