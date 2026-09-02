"""Unit tests for :mod:`bernstein.core.security.secret_rotation`.

The pipeline under test rotates a secret that lives on an *external target*:
mint the new material, store it with bounded per-``(target, principal)``
history, then apply it to the target, in that order. Every test uses real
objects -- a real broker over an in-memory backend, a real version store, a
real on-disk journal -- so the ordering and the "previous version survives a
failed apply" property are exercised where they actually hold.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.security.secret_rotation import (
    RotationAuditFinding,
    RotationError,
    RotationJournal,
    RotationReceipt,
    RotationStep,
    RotationTarget,
    SecretRotator,
    SecretVersionStore,
    audit_rotation_receipts,
)
from bernstein.core.security.secrets_broker import (
    BrokerConfig,
    SecretsBackend,
    SecretsBroker,
    SecretsBrokerError,
    clear_redaction_registry,
)

# ---------------------------------------------------------------------------
# Fixtures: in-memory backend (same style as test_secrets_broker.py) and a
# fake external target that records every apply.
# ---------------------------------------------------------------------------


class _MemoryBackend(SecretsBackend):
    name = "memory"

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets.copy()

    def read(self, secret_name: str) -> str:
        if secret_name not in self._secrets:
            raise SecretsBrokerError(f"memory: no entry for {secret_name!r}")
        return self._secrets[secret_name]


class _FakeTarget(RotationTarget):
    """A target that records applies and can be told to fail the apply step."""

    name = "fake-target"

    def __init__(self, *, fail_apply: bool = False) -> None:
        self.fail_apply = fail_apply
        self.applied: dict[str, str] = {}
        self.apply_calls: list[str] = []
        self.receipts: dict[str, RotationReceipt] = {}
        self.calls: list[str] = []

    def apply(self, *, principal: str, value: str) -> None:
        self.calls.append("apply")
        self.apply_calls.append(principal)
        if self.fail_apply:
            raise RuntimeError("target refused the new credential")
        self.applied[principal] = value

    def write_receipt(self, *, principal: str, receipt: RotationReceipt) -> None:
        self.calls.append("write_receipt")
        self.receipts[principal] = receipt

    def read_receipt(self, *, principal: str) -> RotationReceipt | None:
        return self.receipts.get(principal)


@pytest.fixture(autouse=True)
def _isolated_registry() -> None:
    clear_redaction_registry()
    yield
    clear_redaction_registry()


@pytest.fixture
def broker() -> SecretsBroker:
    backend = _MemoryBackend({"DEPLOY_KEY": "backing-value-0123456789"})
    return SecretsBroker(backend, config=BrokerConfig(backend="file_encrypted"))


# ---------------------------------------------------------------------------
# 1. Bounded, configurable version history (slice 1)
# ---------------------------------------------------------------------------


class TestVersionHistory:
    def test_version_history_per_target_and_principal_is_bounded_and_configurable(self) -> None:
        store = SecretVersionStore(max_versions=2)

        for i in range(4):
            store.store(
                target="t1",
                principal="svc-deploy",
                value=f"value-{i}",
                version_id=f"v{i}",
                created_at=100.0 + i,
            )
        # A different principal on the same target keeps its own history.
        store.store(target="t1", principal="svc-other", value="other", version_id="o0", created_at=100.0)
        # ...and so does the same principal on a different target.
        store.store(target="t2", principal="svc-deploy", value="elsewhere", version_id="e0", created_at=100.0)

        kept = store.versions(target="t1", principal="svc-deploy")
        assert [v.version_id for v in kept] == ["v3", "v2"], "newest first, oldest evicted at the bound"
        assert store.get(target="t1", principal="svc-deploy", version_id="v1") is None

        assert [v.version_id for v in store.versions(target="t1", principal="svc-other")] == ["o0"]
        assert [v.version_id for v in store.versions(target="t2", principal="svc-deploy")] == ["e0"]

        # The bound is configurable, not hard-coded.
        wider = SecretVersionStore(max_versions=5)
        for i in range(6):
            wider.store(target="t1", principal="p", value=f"v{i}", version_id=f"v{i}", created_at=float(i))
        assert len(wider.versions(target="t1", principal="p")) == 5

    def test_history_bound_below_two_is_rejected(self) -> None:
        with pytest.raises(RotationError, match="max_versions"):
            SecretVersionStore(max_versions=1)

    def test_stored_version_repr_does_not_leak_the_secret_value(self) -> None:
        store = SecretVersionStore(max_versions=2)
        version = store.store(
            target="t1", principal="p", value="super-secret-material", version_id="v0", created_at=1.0
        )
        assert "super-secret-material" not in repr(version)
        assert version.value == "super-secret-material"


# ---------------------------------------------------------------------------
# 2. Ordering (slice 2)
# ---------------------------------------------------------------------------


class TestRotationOrder:
    def test_rotation_runs_mint_then_store_then_apply_in_order(self, broker: SecretsBroker, tmp_path: Path) -> None:
        store = SecretVersionStore(max_versions=3)
        journal = RotationJournal(tmp_path / "rotation.jsonl")
        target = _FakeTarget()
        rotator = SecretRotator(broker=broker, store=store, journal=journal)

        observed: list[str] = []
        original_store = store.store

        def _tracking_store(**kwargs: object):  # type: ignore[no-untyped-def]
            observed.append("store")
            return original_store(**kwargs)  # type: ignore[arg-type]

        store.store = _tracking_store  # type: ignore[method-assign]

        original_apply = target.apply

        def _tracking_apply(*, principal: str, value: str) -> None:
            observed.append("apply")
            original_apply(principal=principal, value=value)

        target.apply = _tracking_apply  # type: ignore[method-assign]

        run = rotator.rotate(
            target=target,
            principal="svc-deploy",
            secret_name="DEPLOY_KEY",
            task_id="t-42",
        )

        assert observed == ["store", "apply"], "store must complete before apply"
        assert run.ok is True
        assert run.step_reached is RotationStep.RECEIPT

        # The applied value is the version the store recorded, not a re-mint.
        stored = store.get(target=target.name, principal="svc-deploy", version_id=run.version_id)
        assert stored is not None
        assert target.applied["svc-deploy"] == stored.value

        rows = journal.rows()
        assert [row["step_reached"] for row in rows] == ["receipt"]
        assert rows[0]["ok"] is True
        assert stored.value not in json.dumps(rows), "the journal never carries secret material"

    def test_minted_token_carries_the_version_id_the_store_recorded(
        self, broker: SecretsBroker, tmp_path: Path
    ) -> None:
        store = SecretVersionStore(max_versions=3)
        target = _FakeTarget()
        rotator = SecretRotator(broker=broker, store=store, journal=RotationJournal(tmp_path / "j.jsonl"))

        run = rotator.rotate(target=target, principal="svc-deploy", secret_name="DEPLOY_KEY", task_id="t-1")

        live = {t.token_id: t for t in broker.list_live()}
        token = live.get(run.token_id)
        assert token is not None
        assert token.version_id == run.version_id
        assert store.get(target=target.name, principal="svc-deploy", version_id=token.version_id) is not None


# ---------------------------------------------------------------------------
# 3. Failure after store (load-bearing)
# ---------------------------------------------------------------------------


class TestFailureAfterStore:
    def test_failure_after_store_leaves_previous_version_retrievable_and_target_unchanged(
        self, broker: SecretsBroker, tmp_path: Path
    ) -> None:
        store = SecretVersionStore(max_versions=3)
        journal = RotationJournal(tmp_path / "rotation.jsonl")
        rotator = SecretRotator(broker=broker, store=store, journal=journal)

        good = _FakeTarget()
        first = rotator.rotate(target=good, principal="svc-deploy", secret_name="DEPLOY_KEY", task_id="t-1")
        first_version = store.get(target=good.name, principal="svc-deploy", version_id=first.version_id)
        assert first_version is not None
        applied_before = dict(good.applied)
        receipt_before = good.read_receipt(principal="svc-deploy")

        good.fail_apply = True
        with pytest.raises(RotationError) as excinfo:
            rotator.rotate(target=good, principal="svc-deploy", secret_name="DEPLOY_KEY", task_id="t-2")

        failed = excinfo.value.run
        assert failed.step_reached is RotationStep.STORE, "the run got as far as store, no further"
        assert failed.ok is False

        # Both versions are retrievable: the one the target still holds and the
        # one that was minted and stored but never applied.
        versions = store.versions(target=good.name, principal="svc-deploy")
        assert [v.version_id for v in versions] == [failed.version_id, first.version_id]
        assert store.get(target=good.name, principal="svc-deploy", version_id=first.version_id) is not None
        assert store.get(target=good.name, principal="svc-deploy", version_id=failed.version_id) is not None

        # The target is unchanged: same credential, same receipt.
        assert good.applied == applied_before
        assert good.read_receipt(principal="svc-deploy") == receipt_before

        # And the run is journaled with the step it reached.
        rows = journal.rows()
        assert [row["step_reached"] for row in rows] == ["receipt", "store"]
        assert rows[1]["ok"] is False
        assert "target refused the new credential" in rows[1]["error"]


# ---------------------------------------------------------------------------
# 4. Receipt staleness (slice 3)
# ---------------------------------------------------------------------------


class TestReceiptStaleness:
    def test_rotation_receipt_older_than_policy_window_is_an_audit_finding(
        self, broker: SecretsBroker, tmp_path: Path
    ) -> None:
        store = SecretVersionStore(max_versions=3)
        target = _FakeTarget()
        rotator = SecretRotator(
            broker=broker,
            store=store,
            journal=RotationJournal(tmp_path / "j.jsonl"),
            clock=lambda: 1_000_000.0,
        )
        rotator.rotate(target=target, principal="svc-deploy", secret_name="DEPLOY_KEY", task_id="t-1")

        receipt = target.read_receipt(principal="svc-deploy")
        assert receipt is not None
        assert receipt.rotated_at == 1_000_000

        fresh = audit_rotation_receipts(
            target=target,
            principals=["svc-deploy"],
            max_age_seconds=86_400,
            now=1_000_100.0,
        )
        assert fresh == []

        stale = audit_rotation_receipts(
            target=target,
            principals=["svc-deploy"],
            max_age_seconds=86_400,
            now=1_000_000.0 + 86_401,
        )
        assert len(stale) == 1
        assert stale[0].finding == RotationAuditFinding.NAME
        assert stale[0].receipt_present is True
        assert stale[0].age_seconds is not None and stale[0].age_seconds > 86_400

    def test_missing_receipt_and_stale_receipt_share_one_finding_name(
        self, broker: SecretsBroker, tmp_path: Path
    ) -> None:
        store = SecretVersionStore(max_versions=3)
        target = _FakeTarget()
        rotator = SecretRotator(
            broker=broker,
            store=store,
            journal=RotationJournal(tmp_path / "j.jsonl"),
            clock=lambda: 500.0,
        )
        rotator.rotate(target=target, principal="rotated", secret_name="DEPLOY_KEY", task_id="t-1")

        findings = audit_rotation_receipts(
            target=target,
            principals=["rotated", "never-rotated"],
            max_age_seconds=10,
            now=1_000.0,
        )
        assert {f.principal for f in findings} == {"rotated", "never-rotated"}
        assert {f.finding for f in findings} == {RotationAuditFinding.NAME}
        by_principal = {f.principal: f for f in findings}
        assert by_principal["never-rotated"].receipt_present is False
        assert by_principal["never-rotated"].age_seconds is None
        assert by_principal["rotated"].receipt_present is True


# ---------------------------------------------------------------------------
# Receipt round-trip
# ---------------------------------------------------------------------------


class TestReceiptSerialisation:
    def test_receipt_round_trips_through_canonical_bytes(self) -> None:
        receipt = RotationReceipt(
            target="t1",
            principal="svc-deploy",
            version_id="v-1",
            fingerprint="abcd1234abcd1234",
            rotated_at=1_000_000,
        )
        assert RotationReceipt.from_bytes(receipt.to_canonical_bytes()) == receipt
