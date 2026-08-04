"""Tests for the multi-tier verifier ladder receipts (#2927).

The ladder receipt carries one sealed record per verifier tier that ran and a
composite ``merge_eligible`` claim that is a pure function of those tier
verdicts. Verification never trusts the stored claim: it re-hashes the body,
re-runs :func:`derive_ladder_verdict` over the stored records, and re-checks
every per-tier spine anchor against the spine entry's content hash. All
fixtures are hermetic: on-disk spine and chain under ``tmp_path``, a fixed
timestamp, no network.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bernstein.core.quality.verifier_ladder import (
    LADDER_RECEIPT_SCHEMA_VERSION,
    VERIFIER_LADDER_RUN_ID,
    LadderReceipt,
    TierRecord,
    VerifierTier,
    build_ladder_receipt,
    canonical_hash,
    derive_ladder_verdict,
    ladder_receipt_path,
    read_ladder_receipt,
    recompute_ladder_receipt_hash,
    verify_ladder_receipt,
)
from bernstein.core.security.audit_chain import (
    EVENT_VERIFIER_TIER,
    AuditChainStore,
)

_KEY = b"k" * 32
_TS = 1_700_000_000
_GOLDEN_PATH = Path(__file__).resolve().parent.parent.parent / "golden" / "verifier_ladder_receipt.json"


def _rec(tier: VerifierTier, verdict: str = "pass", *, salt: str = "") -> TierRecord:
    return TierRecord(
        tier=tier,
        config_hash=canonical_hash({"config": tier.value, "salt": salt}),
        inputs_hash=canonical_hash({"inputs": "attributed-diff", "salt": salt}),
        evidence_hash=canonical_hash({"evidence": tier.value, "verdict": verdict, "salt": salt}),
        verdict=verdict,
    )


def _build(
    workdir: Path,
    records: list[TierRecord],
    *,
    chain: AuditChainStore | None = None,
    required: tuple[VerifierTier, ...] = (VerifierTier.DETERMINISTIC,),
) -> LadderReceipt:
    return build_ladder_receipt(
        task_id="T-001",
        records=records,
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_KEY,
        timestamp=_TS,
        required_tiers=required,
        chain=chain,
    )


def _verify(workdir: Path, receipt_hash: str):
    return verify_ladder_receipt(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_KEY,
        receipt_hash=receipt_hash,
    )


# ---------------------------------------------------------------------------
# 1. Byte-stable canonicalisation
# ---------------------------------------------------------------------------


class TestByteStableCanonicalisation:
    def test_two_constructions_of_same_evidence_are_byte_identical(self) -> None:
        a = _rec(VerifierTier.DETERMINISTIC)
        b = _rec(VerifierTier.DETERMINISTIC)
        assert a.canonical_bytes() == b.canonical_bytes()
        assert canonical_hash(a.to_dict()) == canonical_hash(b.to_dict())

    def test_round_trip_preserves_canonical_bytes(self) -> None:
        rec = _rec(VerifierTier.JUDGE, "fail")
        again = TierRecord.from_dict(rec.to_dict())
        assert again.canonical_bytes() == rec.canonical_bytes()

    def test_invalid_verdict_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="verdict"):
            TierRecord(
                tier=VerifierTier.DETERMINISTIC,
                config_hash=canonical_hash({}),
                inputs_hash=canonical_hash({}),
                evidence_hash=canonical_hash({}),
                verdict="maybe",
            )

    def test_non_canonical_hash_is_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="sha256"):
            TierRecord(
                tier=VerifierTier.DETERMINISTIC,
                config_hash="not-a-hash",
                inputs_hash=canonical_hash({}),
                evidence_hash=canonical_hash({}),
                verdict="pass",
            )


# ---------------------------------------------------------------------------
# 2. Fail-closed derive
# ---------------------------------------------------------------------------


class TestDeriveLadderVerdictFailsClosed:
    def test_empty_record_set_is_not_eligible(self) -> None:
        assert derive_ladder_verdict([]) is False

    def test_absent_required_rung_is_not_eligible(self) -> None:
        records = [_rec(VerifierTier.JUDGE, "pass")]
        assert derive_ladder_verdict(records) is False

    def test_mixed_verdicts_fail_closed(self) -> None:
        records = [_rec(VerifierTier.DETERMINISTIC, "pass"), _rec(VerifierTier.JUDGE, "fail")]
        assert derive_ladder_verdict(records) is False

    def test_skip_blocks_eligibility(self) -> None:
        # A tier that was consulted but did not adjudicate cannot support the
        # composite claim: skip is honest coverage, not a pass.
        records = [_rec(VerifierTier.DETERMINISTIC, "pass"), _rec(VerifierTier.JUDGE, "skip")]
        assert derive_ladder_verdict(records) is False

    def test_all_pass_with_required_present_is_eligible(self) -> None:
        assert derive_ladder_verdict([_rec(VerifierTier.DETERMINISTIC, "pass")]) is True
        full = [
            _rec(VerifierTier.DETERMINISTIC, "pass"),
            _rec(VerifierTier.JUDGE, "pass"),
            _rec(VerifierTier.HUMAN, "pass"),
        ]
        assert derive_ladder_verdict(full) is True


# ---------------------------------------------------------------------------
# Build / verify round trip
# ---------------------------------------------------------------------------


class TestBuildAndVerifyRoundTrip:
    def test_untampered_receipt_round_trips(self, tmp_path: Path) -> None:
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC), _rec(VerifierTier.JUDGE)])
        assert receipt.schema_version == LADDER_RECEIPT_SCHEMA_VERSION
        assert receipt.merge_eligible is True
        assert all(r.spine_entry_hash for r in receipt.records)
        assert receipt.spine_entry_hash

        result = _verify(tmp_path, receipt.receipt_hash)
        assert result.ok, result.reason
        assert result.status == "ok"

    def test_read_missing_receipt_returns_none(self, tmp_path: Path) -> None:
        assert read_ladder_receipt(tmp_path, "sha256:" + "a" * 64) is None

    def test_missing_receipt_verifies_as_missing(self, tmp_path: Path) -> None:
        result = _verify(tmp_path, "sha256:" + "a" * 64)
        assert not result.ok
        assert result.status == "missing"

    def test_duplicate_tier_records_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC), _rec(VerifierTier.DETERMINISTIC)])

    def test_records_are_sealed_in_ladder_order(self, tmp_path: Path) -> None:
        receipt = _build(
            tmp_path,
            [_rec(VerifierTier.JUDGE), _rec(VerifierTier.DETERMINISTIC)],
        )
        assert [r.tier for r in receipt.records] == [VerifierTier.DETERMINISTIC, VerifierTier.JUDGE]


# ---------------------------------------------------------------------------
# 4. Tampered merge_eligible rejected by re-derivation
# ---------------------------------------------------------------------------


class TestTamperedMergeEligibleRejected:
    def test_flipped_merge_eligible_is_rejected_even_when_internally_consistent(self, tmp_path: Path) -> None:
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC), _rec(VerifierTier.JUDGE, "fail")])
        assert receipt.merge_eligible is False

        path = ladder_receipt_path(tmp_path, receipt.receipt_hash)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["merge_eligible"] = True
        # Recompute the hash so the forged receipt is internally consistent;
        # re-derivation over the stored tier verdicts must still reject it.
        payload["receipt_hash"] = recompute_ladder_receipt_hash(payload)
        forged_path = ladder_receipt_path(tmp_path, payload["receipt_hash"])
        forged_path.write_text(json.dumps(payload), encoding="utf-8")

        result = _verify(tmp_path, payload["receipt_hash"])
        assert not result.ok
        assert "entail" in result.reason or "re-deriv" in result.reason

    def test_flipped_merge_eligible_without_rehash_is_rejected(self, tmp_path: Path) -> None:
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC, "fail")])
        path = ladder_receipt_path(tmp_path, receipt.receipt_hash)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["merge_eligible"] = True
        path.write_text(json.dumps(payload), encoding="utf-8")

        result = _verify(tmp_path, receipt.receipt_hash)
        assert not result.ok
        assert "recompute" in result.reason or "tamper" in result.reason


# ---------------------------------------------------------------------------
# Malformed-but-internally-consistent receipts fail verification, never crash
# ---------------------------------------------------------------------------


class TestMalformedReceiptFailsClosed:
    def test_a_receipt_with_an_unknown_required_tier_fails_verification_instead_of_crashing(
        self, tmp_path: Path
    ) -> None:
        # receipt_hash is an unsigned content hash anyone can recompute over a
        # tampered body, so a forged required_tiers policy passes the
        # hash-recompute check; the enum conversion must not crash the
        # verifier before the checks that would name the forgery.
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC)])
        path = ladder_receipt_path(tmp_path, receipt.receipt_hash)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["required_tiers"] = ["unknown"]
        payload["receipt_hash"] = recompute_ladder_receipt_hash(payload)
        forged_path = ladder_receipt_path(tmp_path, payload["receipt_hash"])
        forged_path.write_text(json.dumps(payload), encoding="utf-8")

        result = _verify(tmp_path, payload["receipt_hash"])

        assert not result.ok
        assert result.status == "failed"
        assert "required_tiers" in result.reason
        assert "unknown" in result.reason

    def test_a_receipt_with_an_unknown_record_tier_fails_verification_not_missing(self, tmp_path: Path) -> None:
        # A readable receipt whose tier records do not construct must report
        # as failed verification, not vanish as "missing".
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC)])
        path = ladder_receipt_path(tmp_path, receipt.receipt_hash)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["records"][0]["tier"] = "unknown"
        payload["receipt_hash"] = recompute_ladder_receipt_hash(payload)
        forged_path = ladder_receipt_path(tmp_path, payload["receipt_hash"])
        forged_path.write_text(json.dumps(payload), encoding="utf-8")

        result = _verify(tmp_path, payload["receipt_hash"])

        assert not result.ok
        assert result.status == "failed"


# ---------------------------------------------------------------------------
# Symlinked receipt store is refused, not followed
# ---------------------------------------------------------------------------


class _JunctionProbePath(Path):
    """POSIX stand-in for an NTFS junction on the store walk.

    ``is_junction()`` answers ``True`` for the marked component while
    ``is_symlink()`` keeps its real (``False``) answer, so
    ``is_filesystem_link`` takes its junction branch through the real
    component walk rather than having the whole check stubbed away.
    """

    _junction_component = "ladder"

    def is_junction(self) -> bool:
        return self.name == self._junction_component


class TestSymlinkedLadderDirectoryRefused:
    def test_a_symlinked_ladder_directory_is_refused_not_followed(self, tmp_path: Path) -> None:
        # A genuine receipt, sealed elsewhere, planted in an outside directory
        # that a symlinked ladder store points at. realpath-vs-realpath
        # containment passes vacuously in that layout, so the store must
        # refuse the symlink itself.
        elsewhere = tmp_path / "elsewhere"
        receipt = _build(elsewhere, [_rec(VerifierTier.DETERMINISTIC)])
        receipt_json = ladder_receipt_path(elsewhere, receipt.receipt_hash).read_text(encoding="utf-8")

        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / f"{receipt.receipt_hash}.json").write_text(receipt_json, encoding="utf-8")

        victim = tmp_path / "victim"
        (victim / ".sdd" / "quality").mkdir(parents=True)
        (victim / ".sdd" / "quality" / "ladder").symlink_to(outside)

        with pytest.raises(ValueError, match="symlink"):
            ladder_receipt_path(victim, receipt.receipt_hash)
        assert read_ladder_receipt(victim, receipt.receipt_hash) is None
        result = _verify(victim, receipt.receipt_hash)
        assert not result.ok

    def test_a_junction_store_component_is_refused_like_a_symlink(self, tmp_path: Path) -> None:
        # Path.is_symlink() is False for NTFS junctions, so a symlink-only
        # probe is bypassed by a junctioned store component on Windows. A
        # component that is a filesystem link of any kind must be refused
        # before the realpath containment check, with no candidate returned.
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC)])
        workdir = _JunctionProbePath(tmp_path)

        with pytest.raises(ValueError, match="symlink or junction"):
            ladder_receipt_path(workdir, receipt.receipt_hash)
        assert read_ladder_receipt(workdir, receipt.receipt_hash) is None


# ---------------------------------------------------------------------------
# 5. Substituted evidence_hash rejected by the spine content re-check
# ---------------------------------------------------------------------------


class TestSubstitutedEvidenceRejected:
    def test_swapping_a_failing_tiers_evidence_for_a_passing_tiers_is_rejected(self, tmp_path: Path) -> None:
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC), _rec(VerifierTier.JUDGE, "fail")])

        path = ladder_receipt_path(tmp_path, receipt.receipt_hash)
        payload = json.loads(path.read_text(encoding="utf-8"))
        det, judge = payload["records"]
        judge["evidence_hash"] = det["evidence_hash"]
        payload["receipt_hash"] = recompute_ladder_receipt_hash(payload)
        forged_path = ladder_receipt_path(tmp_path, payload["receipt_hash"])
        forged_path.write_text(json.dumps(payload), encoding="utf-8")

        result = _verify(tmp_path, payload["receipt_hash"])
        assert not result.ok
        assert "anchor" in result.reason


# ---------------------------------------------------------------------------
# 6. Dangling spine_entry_hash rejected
# ---------------------------------------------------------------------------


class TestDanglingSpineAnchorRejected:
    def test_anchor_resolving_to_no_spine_entry_fails(self, tmp_path: Path) -> None:
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC)])

        path = ladder_receipt_path(tmp_path, receipt.receipt_hash)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["records"][0]["spine_entry_hash"] = "sha256:" + "f" * 64
        payload["receipt_hash"] = recompute_ladder_receipt_hash(payload)
        forged_path = ladder_receipt_path(tmp_path, payload["receipt_hash"])
        forged_path.write_text(json.dumps(payload), encoding="utf-8")

        result = _verify(tmp_path, payload["receipt_hash"])
        assert not result.ok
        assert "anchor" in result.reason


# ---------------------------------------------------------------------------
# 7. Substrate removed: fails closed
# ---------------------------------------------------------------------------


class TestSubstrateRemovedFailsClosed:
    def test_verification_fails_when_the_spine_is_gone(self, tmp_path: Path) -> None:
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC), _rec(VerifierTier.JUDGE)])
        assert _verify(tmp_path, receipt.receipt_hash).ok

        shutil.rmtree(tmp_path / ".sdd" / "lineage")

        result = _verify(tmp_path, receipt.receipt_hash)
        assert not result.ok
        assert "spine" in result.reason

    def test_tampered_spine_row_fails_verification(self, tmp_path: Path) -> None:
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC)])
        spine_log = tmp_path / ".sdd" / "lineage" / VERIFIER_LADDER_RUN_ID / "spine.jsonl"
        raw = spine_log.read_bytes()
        spine_log.write_bytes(raw.replace(b'"actor"', b'"actox"', 1))

        result = _verify(tmp_path, receipt.receipt_hash)
        assert not result.ok
        assert "spine" in result.reason


# ---------------------------------------------------------------------------
# 9. Determinism with an injected clock
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_evidence_and_clock_build_byte_identical_receipts(self, tmp_path: Path) -> None:
        records_a = [_rec(VerifierTier.DETERMINISTIC), _rec(VerifierTier.JUDGE)]
        records_b = [_rec(VerifierTier.DETERMINISTIC), _rec(VerifierTier.JUDGE)]
        machine_a = _build(tmp_path / "a", records_a)
        machine_b = _build(tmp_path / "b", records_b)
        assert machine_a.receipt_hash == machine_b.receipt_hash
        assert machine_a.canonical_payload_without_anchor() == machine_b.canonical_payload_without_anchor()


# ---------------------------------------------------------------------------
# 10. Golden fixture byte-stability
# ---------------------------------------------------------------------------


class TestGoldenFixture:
    def test_committed_fixture_matches_freshly_built_canonical_bytes(self, tmp_path: Path) -> None:
        receipt = _build(
            tmp_path,
            [
                _rec(VerifierTier.DETERMINISTIC, "pass", salt="golden"),
                _rec(VerifierTier.JUDGE, "fail", salt="golden"),
            ],
        )
        golden = _GOLDEN_PATH.read_text(encoding="utf-8").rstrip("\n")
        assert receipt.canonical_payload_without_anchor() == golden


# ---------------------------------------------------------------------------
# Audit-chain mirroring
# ---------------------------------------------------------------------------


class TestChainMirror:
    def test_build_mirrors_one_event_per_tier(self, tmp_path: Path) -> None:
        chain = AuditChainStore(tmp_path / "audit", key=_KEY)
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC), _rec(VerifierTier.JUDGE, "fail")], chain=chain)

        events = chain.query(event_type=EVENT_VERIFIER_TIER)
        assert len(events) == 2
        assert {e.details["tier"] for e in events} == {"deterministic", "judge"}
        assert all(e.details["receipt_hash"] == receipt.receipt_hash for e in events)
        ok, errors = chain.verify()
        assert ok, errors

    def test_no_chain_no_events(self, tmp_path: Path) -> None:
        receipt = _build(tmp_path, [_rec(VerifierTier.DETERMINISTIC)])
        assert _verify(tmp_path, receipt.receipt_hash).ok
