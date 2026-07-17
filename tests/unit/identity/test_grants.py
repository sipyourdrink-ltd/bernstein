"""Tests for chain-anchored, Ed25519-signed per-task credential grants.

Covers issue #2516: a scoped grant (task id, secret name, audience, expiry,
capability ceiling) is an Ed25519-signed record anchored in the same
HMAC-chained construction as the per-hop delegation receipts. The grant is the
authorization artifact the secrets broker exchanges for a short-lived token;
its issue, exchange, and revocation history reconstruct offline from the chain
alone, and mutating, deleting, or reordering any record flips verification to a
failure naming the offending record.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.identity import grants


@pytest.fixture
def signer() -> grants.GrantSigner:
    return grants.GrantSigner.generate(issuer="manager:test")


@pytest.fixture
def ledger(tmp_path, signer) -> grants.GrantLedger:
    return grants.GrantLedger(root=tmp_path, key=b"k" * 32, signer=signer)


class TestGrantIssuance:
    def test_issue_grant_chains_to_genesis_and_is_signed(self, ledger, signer) -> None:
        g = ledger.issue_grant(
            run_id="run-1",
            task_id="t-42",
            secret_name="ANTHROPIC_API_KEY",
            audience="api.anthropic.com",
            expiry=2_000_000_000,
            capability_ceiling=("read",),
        )
        assert g.kind == grants.GRANT_ISSUED
        assert g.prev_hmac == grants.GENESIS_HMAC
        assert g.record_index == 0
        assert g.grant_id
        assert g.hmac
        assert g.signature
        assert g.issuer == "manager:test"
        # The signature verifies against the embedded issuer public key.
        assert grants.verify_grant_signature(g.issuer_pubkey, g.signed_body(), g.signature)

    def test_grant_carries_scope_fields(self, ledger) -> None:
        g = ledger.issue_grant(
            run_id="run-1",
            task_id="t-42",
            secret_name="K",
            audience="vault.internal",
            expiry=1_900_000_000,
            capability_ceiling=("read", "list"),
        )
        assert g.task_id == "t-42"
        # The persisted/returned secret_name is a digest, never the raw name.
        assert g.secret_name == grants.digest_secret_name("K")
        assert g.secret_name != "K"
        assert g.audience == "vault.internal"
        assert g.expiry == 1_900_000_000
        assert tuple(g.capability_ceiling) == ("list", "read")  # sorted, canonical

    def test_exchange_and_revoke_chain_to_previous(self, ledger) -> None:
        g = ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=0)
        ex = ledger.record_exchange(run_id="run-1", grant_id=g.grant_id, token_id="brn-tok-1")
        rv = ledger.revoke_grant(run_id="run-1", grant_id=g.grant_id, reason="task-exit")
        assert ex.kind == grants.GRANT_EXCHANGED
        assert ex.prev_hmac == g.hmac
        assert ex.token_id == "brn-tok-1"
        assert rv.kind == grants.GRANT_REVOKED
        assert rv.prev_hmac == ex.hmac
        assert rv.grant_id == g.grant_id

    def test_refusal_is_a_chain_record(self, ledger) -> None:
        r = ledger.record_refusal(run_id="run-1", task_id="t-1", secret_name="K", reason="no_grant")
        assert r.kind == grants.GRANT_REFUSED
        assert r.reason == "no_grant"
        assert r.hmac

    def test_persisted_record_never_contains_raw_secret_name(self, ledger) -> None:
        """No persisted surface (JSONL entry, receipt, or report) carries the
        raw secret name in clear text -- only its ``sha256:`` digest.
        """
        raw_secret = "ANTHROPIC_API_KEY_SUPER_SECRET_VALUE"
        g = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name=raw_secret,
            audience="aud",
            expiry=2_000_000_000,
        )
        ledger.record_exchange(run_id="run-1", grant_id=g.grant_id, token_id="brn-tok-1")
        ledger.revoke_grant(run_id="run-1", grant_id=g.grant_id, reason="task-exit", secret_name=raw_secret)

        # On-disk JSONL (the audit chain entry) never contains the raw name.
        raw_file = ledger.receipt_path("run-1").read_text(encoding="utf-8")
        assert raw_secret not in raw_file
        assert grants.digest_secret_name(raw_secret) in raw_file

        # Neither does the in-memory receipt returned to the caller, nor its
        # serialized JSONL entry.
        assert g.secret_name == grants.digest_secret_name(raw_secret)
        assert raw_secret not in g.secret_name
        assert raw_secret not in json.dumps(g.to_entry())

        # Nor a rendered offline verification report.
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        report = grants.render_report(result, run_id="run-1")
        assert raw_secret not in report


class TestOfflineReconstruction:
    def _seed(self, ledger) -> grants.GrantReceipt:
        g = ledger.issue_grant(
            run_id="run-1",
            task_id="t-1",
            secret_name="K",
            audience="aud",
            expiry=2_000_000_000,
            capability_ceiling=("read",),
        )
        ledger.record_exchange(run_id="run-1", grant_id=g.grant_id, token_id="brn-tok-1")
        ledger.revoke_grant(run_id="run-1", grant_id=g.grant_id, reason="task-exit")
        return g

    def test_full_history_reconstructs_offline(self, ledger) -> None:
        self._seed(ledger)
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert result.valid
        assert result.errors == []
        kinds = [r.kind for r in result.records]
        assert kinds == [grants.GRANT_ISSUED, grants.GRANT_EXCHANGED, grants.GRANT_REVOKED]

    def test_lifecycle_marks_grant_revoked(self, ledger) -> None:
        g = self._seed(ledger)
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        life = result.lifecycles()
        assert life[g.grant_id]["issued"] is True
        assert life[g.grant_id]["revoked"] is True
        assert "brn-tok-1" in life[g.grant_id]["token_ids"]

    def test_tampered_expiry_field_fails_naming_record(self, ledger) -> None:
        self._seed(ledger)
        path = ledger.receipt_path("run-1")
        raw = path.read_text(encoding="utf-8")
        # Flip the grant's expiry in the persisted record.
        tampered = raw.replace("2000000000", "9999999999", 1)
        assert tampered != raw
        path.write_text(tampered, encoding="utf-8")
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert not result.valid
        assert result.errors
        # The failing record is named (record 0).
        assert any("record 0" in e or "index 0" in e for e in result.errors)

    def test_deleted_record_breaks_linkage(self, ledger) -> None:
        self._seed(ledger)
        path = ledger.receipt_path("run-1")
        lines = path.read_text(encoding="utf-8").splitlines()
        # Drop the middle (exchange) record -> revoke no longer links.
        path.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert not result.valid

    def test_reordered_records_break_linkage(self, ledger) -> None:
        self._seed(ledger)
        path = ledger.receipt_path("run-1")
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text(lines[1] + "\n" + lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert not result.valid

    def test_forged_signature_fails(self, ledger) -> None:
        self._seed(ledger)
        path = ledger.receipt_path("run-1")
        obj = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        # Swap the audience without the manager key: the HMAC will not match,
        # but even recomputing HMAC would leave the Ed25519 signature broken.
        obj["audience"] = "attacker.example"
        # Re-chain the HMAC as an attacker who holds the install audit key would
        # NOT be able to; here we only prove the signature layer also rejects.
        body = {k: v for k, v in obj.items() if k not in ("hmac", "signature")}
        assert not grants.verify_grant_signature(obj["issuer_pubkey"], body, obj["signature"])

    def test_wrong_hmac_key_fails(self, ledger) -> None:
        self._seed(ledger)
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"x" * 32)
        assert not result.valid

    def test_missing_run_is_empty_not_valid(self, tmp_path) -> None:
        result = grants.verify_grant_chain(root=tmp_path, run_id="absent", key=b"k" * 32)
        assert result.records == []
        assert not result.valid


class TestByteIdenticalReport:
    def test_two_verifiers_produce_identical_report(self, ledger) -> None:
        g = ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=2_000_000_000)
        ledger.record_exchange(run_id="run-1", grant_id=g.grant_id, token_id="brn-tok-1")
        r1 = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        r2 = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        report1 = grants.render_report(r1, run_id="run-1")
        report2 = grants.render_report(r2, run_id="run-1")
        assert report1 == report2
        # Report is canonical JSON that names the run and every record.
        parsed = json.loads(report1)
        assert parsed["run"] == "run-1"
        assert parsed["valid"] is True


class TestActiveGrantLookup:
    def test_find_active_grant_matches_task_and_secret(self, ledger) -> None:
        ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=2_000_000_000)
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        g = grants.find_active_grant(result, task_id="t-1", secret_name="K", now=1_000_000_000)
        assert g is not None
        assert g.audience == "aud"

    def test_find_active_grant_skips_revoked(self, ledger) -> None:
        g = ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=2_000_000_000)
        ledger.revoke_grant(run_id="run-1", grant_id=g.grant_id, reason="revoked")
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert grants.find_active_grant(result, task_id="t-1", secret_name="K", now=1_000_000_000) is None

    def test_find_active_grant_skips_expired(self, ledger) -> None:
        ledger.issue_grant(run_id="run-1", task_id="t-1", secret_name="K", audience="aud", expiry=1_000)
        result = grants.verify_grant_chain(root=ledger.root, run_id="run-1", key=b"k" * 32)
        assert grants.find_active_grant(result, task_id="t-1", secret_name="K", now=2_000) is None
