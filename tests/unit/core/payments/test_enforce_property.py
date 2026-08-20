"""Property test (AC7): concurrent authorize never admits spend beyond max_amount.

Interleaved authorize calls against one shared mandate, driven from multiple
threads, must never let the cumulative authorized total exceed the mandate's
``max_amount``. The invariant is enforced by the ``flock``-guarded
read-aggregate-decide-append critical section in :mod:`bernstein.core.payments.enforce`.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bernstein.core.payments._identity import load_operator_identity
from bernstein.core.payments.enforce import (
    TransactionRequest,
    authorize,
    cumulative_authorized_nanos,
)
from bernstein.core.payments.mandate import PresenceMode, SpendMandate
from bernstein.core.payments.receipt import Decision
from bernstein.core.security.audit_chain import AuditChainStore

_KEY = b"p" * 32


def _run_one(
    workdir: Path, mandate: SpendMandate, identity, chain, amount: str, nonce: str, out: list, lock: threading.Lock
) -> None:
    req = TransactionRequest.build(
        amount=amount,
        currency="USD",
        recipient="vendor:acme",
        category="data",
        presence_mode=PresenceMode.DELEGATED,
        now=1_900_000_000,
    )
    receipt = authorize(
        request=req,
        mandate=mandate,
        workdir=workdir,
        hmac_key=_KEY,
        identity=identity,
        chain=chain,
        nonce=nonce,
    )
    with lock:
        out.append(receipt)


@settings(max_examples=25, deadline=None, derandomize=True, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    amounts=st.lists(st.integers(min_value=1, max_value=40), min_size=2, max_size=8),
    max_units=st.integers(min_value=10, max_value=60),
)
def test_concurrent_authorize_never_exceeds_max(tmp_path: Path, amounts: list[int], max_units: int) -> None:
    # Fresh isolated workdir per example (function-scoped tmp_path is reused
    # across Hypothesis examples, so carve a unique subdir each time).
    workdir = tmp_path / uuid.uuid4().hex
    workdir.mkdir()

    identity = load_operator_identity(workdir / ".bernstein" / "keys")
    chain = AuditChainStore(workdir / ".sdd" / "audit", key=_KEY)
    mandate = SpendMandate.issue(
        private_key_pem=identity.private_pem,
        public_key_pem=identity.public_pem,
        kid=identity.kid,
        presence_mode=PresenceMode.DELEGATED,
        max_amount=f"{max_units}.00",
        currency="USD",
        recipient="vendor:acme",
        not_after=2_000_000_000,
        issued_at=1_800_000_000,
        nonce="n0",
        per_tx_cap=None,
        allowed_categories=None,
    )
    mandate_hash = mandate.mandate_hash()

    receipts: list = []
    append_lock = threading.Lock()
    threads = [
        threading.Thread(
            target=_run_one,
            args=(workdir, mandate, identity, chain, f"{amt}.00", f"r{i}", receipts, append_lock),
        )
        for i, amt in enumerate(amounts)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    max_nanos = int(mandate.max_amount_nanos)
    authorized_total = sum(int(r.amount_nanos) for r in receipts if r.decision == Decision.AUTHORIZED.value)

    # Core invariant: admitted spend never exceeds the cap, under any interleaving.
    assert authorized_total <= max_nanos
    # The ledger's read-time aggregation agrees with the receipts we collected.
    assert cumulative_authorized_nanos(workdir, mandate_hash) == authorized_total
    # Every attempt produced a receipt (approved or refused), so no attempt was lost.
    assert len(receipts) == len(amounts)
    # The audit chain stays linear despite concurrent writers.
    ok, errors = chain.verify()
    assert ok, errors
