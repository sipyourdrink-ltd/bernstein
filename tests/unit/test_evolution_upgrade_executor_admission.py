"""Reviewer approval is not admission.

Without a gate here, an approved upgrade auto-executes on the reviewer verdict
alone -- the bypass #3701 exists to close. This path reaches the same admission
service the FileUpgradeExecutor path uses, not a second gate with its own
semantics.
"""

from __future__ import annotations

import pytest

from bernstein.core.config.upgrade_executor import (
    UpgradeExecutor,
    UpgradeStatus,
    UpgradeTransaction,
    UpgradeType,
)
from bernstein.core.quality.empirical_confidence import ConfidenceQuery
from bernstein.evolution.admission import (
    AdmissionMode,
    AdmissionPolicy,
    ColdStartMode,
    decision_key,
    producer_identity,
)


@pytest.fixture()
def query(tmp_path) -> ConfidenceQuery:
    return ConfidenceQuery(db_path=tmp_path / "confidence.db", min_samples=5)


def _transaction(produced_by: str = "manager-agent") -> UpgradeTransaction:
    return UpgradeTransaction(
        id="txn-1",
        upgrade_type=UpgradeType.CONFIG_ADJUSTMENT,
        title="Tune a setting",
        description="desc",
        produced_by=produced_by,
    )


def test_transaction_satisfies_the_admission_protocol() -> None:
    """The gate takes a category, a trigger and a producer. A transaction and a
    proposal are separate lineages that both supply those."""
    transaction = _transaction()
    assert producer_identity(transaction) == "manager-agent"
    key = decision_key(transaction)
    assert key.startswith("category:")
    assert "|trigger:reviewer_approval" in key


def test_the_trigger_distinguishes_this_path_from_the_loop() -> None:
    """Both executor families share one service, but their histories must not
    merge -- a producer reliable through the evolution loop has said nothing
    about its behaviour under reviewer auto-execute."""
    assert "trigger:reviewer_approval" in decision_key(_transaction())


def test_unattributed_transactions_get_their_own_history() -> None:
    assert producer_identity(_transaction(produced_by="")) == "unattributed"


def test_executor_defaults_to_the_shared_service(tmp_path) -> None:
    executor = UpgradeExecutor(workdir=tmp_path, auto_git=False)
    assert executor._admission is not None
    assert executor._admission.mode is AdmissionMode.OBSERVE


def test_a_measured_bad_producer_is_refused(tmp_path, query) -> None:
    transaction = _transaction()
    key = decision_key(transaction)
    for outcome in (True, False, False, False, False):
        query.record("manager-agent", key, outcome)

    policy = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE)
    decision = policy.evaluate(transaction)

    assert decision.admitted is False
    assert decision.confidence.samples == 5


def test_observe_mode_lets_history_accumulate(tmp_path, query) -> None:
    """The same bootstrap argument as the loop path: enforcing on a cold
    database deadlocks, because nothing applies and so nothing is recorded."""
    transaction = _transaction()
    policy = AdmissionPolicy(query=query)

    for _ in range(5):
        policy.record_outcome(policy.evaluate(transaction), True)

    enforcing = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE)
    assert enforcing.evaluate(transaction).admitted is True


def test_refusal_marks_the_transaction_rejected(tmp_path, query) -> None:
    """A refused upgrade is not silently dropped: the transaction carries why."""
    transaction = _transaction()
    key = decision_key(transaction)
    for _ in range(5):
        query.record("manager-agent", key, False)

    policy = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE)
    decision = policy.evaluate(transaction)
    assert decision.admitted is False

    # Mirror what _review_upgrade does on refusal.
    transaction.status = UpgradeStatus.REVIEW_REJECTED
    transaction.error_message = f"Refused by admission policy: {decision.reason}"

    assert transaction.status is UpgradeStatus.REVIEW_REJECTED
    assert "admission policy" in transaction.error_message


def test_cold_start_config_typo_fails_closed(query, monkeypatch) -> None:
    monkeypatch.setenv("BERNSTEIN_ADMISSION_COLD_START", "fail-open")
    policy = AdmissionPolicy(query=query, mode=AdmissionMode.ENFORCE)
    assert policy.cold_start is ColdStartMode.FAIL_CLOSED
    assert policy.evaluate(_transaction()).admitted is False
