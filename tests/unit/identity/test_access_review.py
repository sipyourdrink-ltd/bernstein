"""Tests for the chain-derived per-principal access review (issue #4974).

The review is a projection over the delegation receipts and the grant chain:
every grant issued, every delegation made, and every capability-ceiling change
in a window, each row naming the authorizing principal and the chain event that
records it. The document is signed and its digest is what a reviewer's sign-off
attaches to, so a sign-off cannot be carried over to a document whose bytes
differ.
"""

from __future__ import annotations

import json

import pytest

from bernstein.core.identity import access_review, delegation, grants

KEY = b"k" * 32

#: Fixed epochs so every assertion is about the window, never about wall clock.
BEFORE = 1_700_000_000
INSIDE_A = 1_700_100_000
INSIDE_B = 1_700_200_000
AFTER = 1_700_900_000

SINCE = 1_700_050_000
UNTIL = 1_700_500_000


@pytest.fixture
def signer() -> grants.GrantSigner:
    return grants.GrantSigner.generate(issuer="manager:test")


@pytest.fixture
def seeded(tmp_path, signer):
    """Seed a grant chain and a delegation chain that straddle the window."""
    grant_ledger = grants.GrantLedger(root=tmp_path, key=KEY, signer=signer)
    grant_ledger.issue_grant(
        run_id="run-a",
        task_id="t-1",
        secret_name="ANTHROPIC_API_KEY",
        audience="api.anthropic.com",
        expiry=2_000_000_000,
        capability_ceiling=("read",),
        grant_id="g-before",
        created=BEFORE,
    )
    grant_ledger.issue_grant(
        run_id="run-a",
        task_id="t-1",
        secret_name="ANTHROPIC_API_KEY",
        audience="api.anthropic.com",
        expiry=2_000_000_000,
        capability_ceiling=("read", "write"),
        grant_id="g-inside",
        created=INSIDE_A,
    )
    grant_ledger.revoke_grant(
        run_id="run-a",
        grant_id="g-inside",
        reason="rotation",
        created=INSIDE_B,
    )
    grant_ledger.issue_grant(
        run_id="run-a",
        task_id="t-2",
        secret_name="VAULT_TOKEN",
        audience="vault.internal",
        capability_ceiling=("read",),
        grant_id="g-after",
        created=AFTER,
    )

    hops = delegation.DelegationLedger(root=tmp_path, key=KEY)
    hops.record_hop(
        run_id="run-a",
        issuer="principal:alex",
        subject="orchestrator",
        audience="orchestrator",
        act="run.authorize",
        created=INSIDE_A,
    )
    hops.record_hop(
        run_id="run-a",
        issuer="orchestrator",
        subject="orchestrator",
        audience="sub-agent:backend",
        act="task.spawn",
        created=INSIDE_B,
    )
    hops.record_hop(
        run_id="run-a",
        issuer="orchestrator",
        subject="orchestrator",
        audience="sub-agent:frontend",
        act="task.spawn",
        created=AFTER,
    )
    return tmp_path


def _review(root, signer, *, since=SINCE, until=UNTIL):
    return access_review.build_review(root=root, key=KEY, since=since, until=until, signer=signer)


class TestWindow:
    def test_review_lists_every_grant_and_delegation_inside_the_window(self, seeded, signer) -> None:
        review = _review(seeded, signer)
        events = sorted((row["event"], row["principal"]) for row in review.rows)
        assert events == [
            ("capability_ceiling_change", "task:t-1"),
            ("delegation", "orchestrator"),
            ("delegation", "sub-agent:backend"),
            ("grant_issued", "task:t-1"),
            ("grant_revoked", "task:t-1"),
        ]

    def test_review_omits_events_outside_the_window(self, seeded, signer) -> None:
        review = _review(seeded, signer)
        assert all(SINCE <= row["created"] < UNTIL for row in review.rows)
        rendered = json.dumps(review.rows)
        assert "g-before" not in rendered
        assert "g-after" not in rendered
        assert "sub-agent:frontend" not in rendered

    def test_open_ended_window_includes_the_tail(self, seeded, signer) -> None:
        review = _review(seeded, signer, until=None)
        assert any(row["principal"] == "sub-agent:frontend" for row in review.rows)
        assert review.document["period"]["until"] is None


class TestRowProvenance:
    def test_every_row_names_its_authorizing_principal_and_chain_event(self, seeded, signer) -> None:
        review = _review(seeded, signer)
        chain_events = {r.hmac for r in grants.verify_grant_chain(root=seeded, run_id="run-a", key=KEY).records} | {
            r.hmac for r in delegation.verify_run_chain(root=seeded, run_id="run-a", key=KEY).receipts
        }
        for row in review.rows:
            assert row["authorized_by"], row
            assert row["chain_event"] in chain_events, row
            assert row["chain"] in ("grants", "delegation")

    def test_grant_rows_name_the_issuing_manager_and_delegation_rows_the_delegator(self, seeded, signer) -> None:
        review = _review(seeded, signer)
        by_event = {(row["event"], row["principal"]): row for row in review.rows}
        assert by_event[("grant_issued", "task:t-1")]["authorized_by"] == "manager:test"
        assert by_event[("delegation", "orchestrator")]["authorized_by"] == "principal:alex"
        assert by_event[("delegation", "sub-agent:backend")]["authorized_by"] == "orchestrator"

    def test_capability_ceiling_change_is_a_row_naming_both_ceilings(self, seeded, signer) -> None:
        review = _review(seeded, signer)
        changes = [row for row in review.rows if row["event"] == "capability_ceiling_change"]
        assert len(changes) == 1
        detail = changes[0]["detail"]
        assert detail["from"] == ["read"]
        assert detail["to"] == ["read", "write"]
        # The prior ceiling was set outside the window; the change is still
        # derived from the whole chain, only the row is windowed.
        assert changes[0]["created"] == INSIDE_A


class TestDeterminism:
    def test_regenerating_the_review_from_the_chain_is_byte_identical(self, seeded, signer) -> None:
        first = _review(seeded, signer)
        second = _review(seeded, signer)
        assert first.envelope_bytes() == second.envelope_bytes()
        assert first.digest == second.digest

    def test_signature_verifies_offline_against_the_embedded_key(self, seeded, signer) -> None:
        review = _review(seeded, signer)
        verdict = access_review.verify_signed_review(review.envelope_bytes())
        assert verdict.ok, verdict.errors
        assert verdict.digest == review.digest


class TestTamperedSourceChain:
    def test_rows_from_a_tampered_source_chain_are_excluded_and_named(self, seeded, signer) -> None:
        path = seeded / "grants" / "run-a.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["capability_ceiling"] = ["read", "write", "admin"]
        lines[1] = json.dumps(entry, sort_keys=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        review = _review(seeded, signer)
        assert not [row for row in review.rows if row["chain"] == "grants"]
        unverified = review.document["unverified"]
        assert [u["chain"] for u in unverified] == ["grants"]
        assert unverified[0]["run_id"] == "run-a"
        assert unverified[0]["error"]
        # The delegation chain is untouched, so its rows survive.
        assert [row for row in review.rows if row["chain"] == "delegation"]


class TestSignoff:
    def test_signoff_appends_a_chain_event_naming_reviewer_and_document_digest(self, seeded, signer) -> None:
        review = _review(seeded, signer)
        record = access_review.record_signoff(
            root=seeded,
            key=KEY,
            reviewer="alex@example.com",
            review=review,
            decision="approved",
            note="quarterly",
            created=INSIDE_B,
        )
        assert record.prev_hmac == access_review.GENESIS_HMAC
        assert record.kind == access_review.SIGNOFF_KIND
        assert record.reviewer == "alex@example.com"
        assert record.review_digest == review.digest
        assert record.hmac

        chain = access_review.verify_signoff_chain(root=seeded, key=KEY)
        assert chain.valid, chain.errors
        assert [r.review_digest for r in chain.records] == [review.digest]

    def test_signoff_does_not_verify_against_a_document_whose_bytes_differ(self, seeded, signer) -> None:
        review = _review(seeded, signer)
        access_review.record_signoff(
            root=seeded,
            key=KEY,
            reviewer="alex@example.com",
            review=review,
            decision="approved",
            created=INSIDE_B,
        )
        assert access_review.verify_signoff(root=seeded, key=KEY, envelope_bytes=review.envelope_bytes()).ok

        envelope = json.loads(review.envelope_bytes())
        envelope["review"]["rows"][0]["authorized_by"] = "manager:attacker"
        tampered = json.dumps(envelope, sort_keys=True, indent=2, ensure_ascii=False).encode() + b"\n"

        verdict = access_review.verify_signoff(root=seeded, key=KEY, envelope_bytes=tampered)
        assert not verdict.ok
        assert verdict.record is None
        assert verdict.errors

    def test_signoff_chain_break_is_reported_rather_than_silently_accepted(self, seeded, signer) -> None:
        review = _review(seeded, signer)
        access_review.record_signoff(
            root=seeded,
            key=KEY,
            reviewer="alex@example.com",
            review=review,
            decision="approved",
            created=INSIDE_B,
        )
        path = access_review.signoff_path(seeded)
        entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        entry["reviewer"] = "someone-else"
        path.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")

        chain = access_review.verify_signoff_chain(root=seeded, key=KEY)
        assert not chain.valid
        assert chain.errors
        assert not access_review.verify_signoff(root=seeded, key=KEY, envelope_bytes=review.envelope_bytes()).ok
