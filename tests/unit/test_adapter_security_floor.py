"""Tests for the adapter security-floor spawn preflight (issue #2515).

Covers the acceptance criteria: a below-floor spawn is refused by default and
the refusal is a chain-anchored, offline-verifiable receipt; every preflight
decision (permit / refusal / warn-override) is chain-anchored so a contiguous
slice proves no below-floor spawn was permitted; the receipt payload is
deterministic modulo its timestamp; and the floor map's content hash pins the
map so a mutation is flagged at verification.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.adapters.advisories import ADAPTER_MIN_SAFE_VERSIONS
from bernstein.adapters.security_floor import (
    POLICY_BLOCK,
    POLICY_WARN,
    VERDICT_PERMIT,
    VERDICT_REFUSE,
    VERDICT_UNKNOWN_VERSION,
    VERDICT_WARN_OVERRIDE,
    AdapterSecurityFloorRefusal,
    audit_preflight_no_below_floor,
    build_preflight_receipt,
    evaluate_spawn_floor,
    floor_map_content_hash,
    policy_from_env,
    preflight_spawn_floor,
    receipt_floor_map_matches,
    receipt_sha256,
    security_floor_for,
    verify_preflight_receipt,
    write_preflight_receipt,
)
from bernstein.core.security.audit_chain import (
    EVENT_ADAPTER_SPAWN_PREFLIGHT,
    AuditChainStore,
)

_GENERATED_AT = "2026-07-16T00:00:00+00:00"

# A tracked adapter and a version comfortably below and above its floor.
_ADAPTER = "aider"
_FLOOR = ADAPTER_MIN_SAFE_VERSIONS[_ADAPTER].min_safe_version  # "0.60.0"
_BELOW = "0.1.0"
_ABOVE = "999.0.0"


def _store(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def _which(_name: str) -> str | None:
    return f"/usr/bin/{_name}"


# ---------------------------------------------------------------------------
# Pure verdict evaluation
# ---------------------------------------------------------------------------


class TestEvaluate:
    def test_below_floor_blocks_under_block_policy(self) -> None:
        v = evaluate_spawn_floor(_ADAPTER, _ADAPTER, _BELOW, policy=POLICY_BLOCK)
        assert v.verdict == VERDICT_REFUSE
        assert v.blocked is True
        assert v.floor == _FLOOR
        assert v.advisory_id == ADAPTER_MIN_SAFE_VERSIONS[_ADAPTER].advisory_id

    def test_below_floor_is_override_under_warn_policy(self) -> None:
        v = evaluate_spawn_floor(_ADAPTER, _ADAPTER, _BELOW, policy=POLICY_WARN)
        assert v.verdict == VERDICT_WARN_OVERRIDE
        assert v.blocked is False

    def test_at_or_above_floor_permits(self) -> None:
        assert evaluate_spawn_floor(_ADAPTER, _ADAPTER, _FLOOR).verdict == VERDICT_PERMIT
        assert evaluate_spawn_floor(_ADAPTER, _ADAPTER, _ABOVE).verdict == VERDICT_PERMIT

    def test_unknown_version_is_not_blocked(self) -> None:
        v = evaluate_spawn_floor(_ADAPTER, _ADAPTER, None)
        assert v.verdict == VERDICT_UNKNOWN_VERSION
        assert v.blocked is False

    def test_unparseable_version_is_unknown_not_refuse(self) -> None:
        v = evaluate_spawn_floor(_ADAPTER, _ADAPTER, "not-a-version")
        assert v.verdict == VERDICT_UNKNOWN_VERSION
        assert v.blocked is False

    def test_untracked_adapter_permits_with_no_floor(self) -> None:
        v = evaluate_spawn_floor("claude", "claude", "0.0.1")
        assert v.verdict == VERDICT_PERMIT
        assert v.floor is None
        assert v.tracked is False

    def test_security_floor_for(self) -> None:
        assert security_floor_for(_ADAPTER) == _FLOOR
        assert security_floor_for("claude") is None


class TestPolicyFromEnv:
    def test_default_is_block(self) -> None:
        assert policy_from_env({}) == POLICY_BLOCK

    @pytest.mark.parametrize("token", ["warn", "warn-only", "warn_only", "advisory", "WARN"])
    def test_warn_tokens_select_warn(self, token: str) -> None:
        assert policy_from_env({"BERNSTEIN_ADAPTER_FLOOR_POLICY": token}) == POLICY_WARN

    def test_unknown_token_is_block(self) -> None:
        assert policy_from_env({"BERNSTEIN_ADAPTER_FLOOR_POLICY": "nonsense"}) == POLICY_BLOCK


# ---------------------------------------------------------------------------
# Receipt determinism + tamper (AC: determinism, floor-map tamper detection)
# ---------------------------------------------------------------------------


class TestReceipts:
    def test_receipt_is_deterministic_modulo_timestamp(self) -> None:
        v = evaluate_spawn_floor(_ADAPTER, _ADAPTER, _BELOW)
        a = build_preflight_receipt(v, generated_at=_GENERATED_AT)
        b = build_preflight_receipt(v, generated_at=_GENERATED_AT)
        assert a == b
        assert receipt_sha256(a) == receipt_sha256(b)

    def test_receipt_changes_with_timestamp_only_via_field(self) -> None:
        v = evaluate_spawn_floor(_ADAPTER, _ADAPTER, _BELOW)
        a = build_preflight_receipt(v, generated_at=_GENERATED_AT)
        b = build_preflight_receipt(v, generated_at="2026-07-16T00:00:01+00:00")
        assert a != b
        # Only the timestamp differs; strip it and the payloads are identical.
        a.pop("generated_at")
        b.pop("generated_at")
        assert a == b

    def test_receipt_carries_floor_verdict_fields(self) -> None:
        v = evaluate_spawn_floor(_ADAPTER, _ADAPTER, _BELOW)
        r = build_preflight_receipt(v, generated_at=_GENERATED_AT)
        assert r["verdict"] == VERDICT_REFUSE
        assert r["floor"] == _FLOOR
        assert r["advisory_id"] == ADAPTER_MIN_SAFE_VERSIONS[_ADAPTER].advisory_id
        assert r["floor_map_hash"] == floor_map_content_hash()
        assert r["installed_version"] == _BELOW

    def test_written_receipt_verifies_and_body_tamper_is_detected(self, tmp_path: Path) -> None:
        v = evaluate_spawn_floor(_ADAPTER, _ADAPTER, _BELOW)
        r = build_preflight_receipt(v, generated_at=_GENERATED_AT)
        path = write_preflight_receipt(tmp_path, r)
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert verify_preflight_receipt(doc)
        doc["receipt"]["installed_version"] = _ABOVE
        assert not verify_preflight_receipt(doc)

    def test_floor_map_mutation_is_flagged(self) -> None:
        v = evaluate_spawn_floor(_ADAPTER, _ADAPTER, _BELOW)
        r = build_preflight_receipt(v, generated_at=_GENERATED_AT)
        doc = {"receipt": r, "receipt_sha256": receipt_sha256(r)}
        # Matches the live map now.
        assert receipt_floor_map_matches(doc)
        # A map mutated after the fact yields a hash mismatch at verification.
        assert not receipt_floor_map_matches(doc, current_hash="sha256:" + "0" * 64)


# ---------------------------------------------------------------------------
# Enforcement: the refusal IS the chain-anchored receipt (AC1)
# ---------------------------------------------------------------------------


class TestPreflightEnforcement:
    def test_below_floor_refused_and_anchored(self, tmp_path: Path) -> None:
        chain = _store(tmp_path)
        with pytest.raises(AdapterSecurityFloorRefusal) as exc_info:
            preflight_spawn_floor(
                adapter=_ADAPTER,
                chain=chain,
                generated_at=_GENERATED_AT,
                which=_which,
                version_probe=lambda _p: _BELOW,
                policy=POLICY_BLOCK,
            )
        refusal = exc_info.value
        assert refusal.receipt["verdict"] == VERDICT_REFUSE
        assert refusal.receipt_sha256

        # The refusal is a chain-anchored event, verifiable offline.
        rows = chain.query(event_type=EVENT_ADAPTER_SPAWN_PREFLIGHT)
        assert len(rows) == 1
        assert rows[0].details["verdict"] == VERDICT_REFUSE
        assert rows[0].details["receipt_sha256"] == refusal.receipt_sha256
        assert rows[0].details["floor_map_hash"] == floor_map_content_hash()
        assert "prev_chain_digest" in rows[0].details
        ok, errors = chain.verify()
        assert ok, errors

    def test_refusal_chain_tamper_is_detected(self, tmp_path: Path) -> None:
        chain = _store(tmp_path)
        with pytest.raises(AdapterSecurityFloorRefusal):
            preflight_spawn_floor(
                adapter=_ADAPTER,
                chain=chain,
                generated_at=_GENERATED_AT,
                which=_which,
                version_probe=lambda _p: _BELOW,
            )
        assert chain.verify()[0]

        # Forge the recorded floor (as if a below-floor spawn were relabelled
        # a permit): the HMAC no longer matches.
        log_path = sorted((tmp_path / "audit").glob("*.jsonl"))[0]
        lines = log_path.read_text().splitlines()
        entry = json.loads(lines[-1])
        entry["details"]["verdict"] = VERDICT_PERMIT
        entry["details"]["blocked"] = False
        lines[-1] = json.dumps(entry, sort_keys=True)
        log_path.write_text("\n".join(lines) + "\n")

        ok_after, errors = chain.verify()
        assert not ok_after
        assert any("HMAC mismatch" in e for e in errors)

    def test_permit_is_recorded_too(self, tmp_path: Path) -> None:
        chain = _store(tmp_path)
        verdict = preflight_spawn_floor(
            adapter=_ADAPTER,
            chain=chain,
            generated_at=_GENERATED_AT,
            which=_which,
            version_probe=lambda _p: _ABOVE,
        )
        assert verdict.verdict == VERDICT_PERMIT
        rows = chain.query(event_type=EVENT_ADAPTER_SPAWN_PREFLIGHT)
        assert len(rows) == 1
        assert rows[0].details["verdict"] == VERDICT_PERMIT
        assert rows[0].details["blocked"] is False

    def test_warn_override_permits_and_records(self, tmp_path: Path) -> None:
        chain = _store(tmp_path)
        verdict = preflight_spawn_floor(
            adapter=_ADAPTER,
            chain=chain,
            generated_at=_GENERATED_AT,
            which=_which,
            version_probe=lambda _p: _BELOW,
            policy=POLICY_WARN,
        )
        assert verdict.verdict == VERDICT_WARN_OVERRIDE
        assert verdict.blocked is False
        rows = chain.query(event_type=EVENT_ADAPTER_SPAWN_PREFLIGHT)
        assert rows[0].details["verdict"] == VERDICT_WARN_OVERRIDE

    def test_untracked_adapter_is_noop_no_receipt(self, tmp_path: Path) -> None:
        chain = _store(tmp_path)
        verdict = preflight_spawn_floor(
            adapter="claude",
            chain=chain,
            generated_at=_GENERATED_AT,
            which=_which,
            version_probe=lambda _p: "0.0.1",
        )
        assert verdict.verdict == VERDICT_PERMIT
        assert chain.query(event_type=EVENT_ADAPTER_SPAWN_PREFLIGHT) == []


# ---------------------------------------------------------------------------
# Offline proof over a chain slice (AC2)
# ---------------------------------------------------------------------------


class TestOfflineAudit:
    def test_block_policy_slice_proves_no_below_floor_permitted(self, tmp_path: Path) -> None:
        chain = _store(tmp_path)
        # A permit and a refusal, both under block policy.
        preflight_spawn_floor(
            adapter=_ADAPTER,
            chain=chain,
            generated_at=_GENERATED_AT,
            which=_which,
            version_probe=lambda _p: _ABOVE,
        )
        with pytest.raises(AdapterSecurityFloorRefusal):
            preflight_spawn_floor(
                adapter=_ADAPTER,
                chain=chain,
                generated_at=_GENERATED_AT,
                which=_which,
                version_probe=lambda _p: _BELOW,
            )
        rows = chain.query(event_type=EVENT_ADAPTER_SPAWN_PREFLIGHT)
        ok, violations = audit_preflight_no_below_floor(rows)
        assert ok
        assert violations == []
        # Chain continuity: a gap is detectable, not silently exculpatory.
        assert chain.verify()[0]

    def test_warn_override_is_flagged_as_permitted_below_floor(self, tmp_path: Path) -> None:
        chain = _store(tmp_path)
        preflight_spawn_floor(
            adapter=_ADAPTER,
            chain=chain,
            generated_at=_GENERATED_AT,
            which=_which,
            version_probe=lambda _p: _BELOW,
            policy=POLICY_WARN,
        )
        rows = chain.query(event_type=EVENT_ADAPTER_SPAWN_PREFLIGHT)
        ok, violations = audit_preflight_no_below_floor(rows)
        assert not ok
        assert len(violations) == 1
        assert _ADAPTER in violations[0]
