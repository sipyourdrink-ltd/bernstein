"""Unit tests for the sovereign deployment profile (issue #2518).

Covers the acceptance criteria:

* determinism + verifiability: the effective-policy hash is a pure function of
  the config snapshot, recomputable by an auditor and matching the
  chain-anchored attestation;
* drift chaos: mutating one config key after attestation makes the recomputed
  posture diverge, and the signed drift record names the exact diverging keys
  and re-verifies under ``bernstein audit verify``;
* attestation tamper: mutating a stored attestation / drift record fails the
  offline signature verification.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.security.audit_chain import (
    EVENT_SOVEREIGN_ATTESTATION,
    EVENT_SOVEREIGN_DRIFT,
    AuditChainStore,
)
from bernstein.core.security.deployment_profile import (
    SOVEREIGN_PROFILE,
    EffectivePolicy,
    PostureDriftRefusal,
    build_posture_attestation,
    evaluate_posture_drift,
    is_local_or_eu_host,
    load_config_snapshot,
    read_posture_attestation,
    record_and_sign_drift,
    resolve_effective_policy,
    verify_sovereign_attestations,
)


def _write_config(workdir: Path, body: str) -> None:
    (workdir / "bernstein.yaml").write_text(body, encoding="utf-8")


def _audit_dir(workdir: Path) -> Path:
    d = workdir / ".sdd" / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d


_CLEAN_CONFIG = "goal: x\nstorage:\n  backend: memory\n"


# ---------------------------------------------------------------------------
# Pure projection + determinism (verifiability / determinism SOTA axis)
# ---------------------------------------------------------------------------


def test_resolve_effective_policy_is_deterministic(tmp_path: Path) -> None:
    _write_config(tmp_path, _CLEAN_CONFIG)
    snap = load_config_snapshot(tmp_path)
    a = resolve_effective_policy(SOVEREIGN_PROFILE, snap)
    b = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    assert a.posture_hash() == b.posture_hash()
    assert a.posture_hash().startswith("sha256:")


def test_effective_policy_pins_profile_constants(tmp_path: Path) -> None:
    _write_config(tmp_path, _CLEAN_CONFIG)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    doc = policy.to_canonical_document()
    assert doc["network_egress"] == "deny-all"
    assert doc["egress_allowlist"] == []
    assert doc["catalog_mode"] == "offline"
    assert doc["compliance_pack"] == "regulated"
    assert doc["storage_backend"] == "memory"
    assert doc["residency_regions"] == ["eu-central", "eu-west"]


def test_clean_config_has_no_violations(tmp_path: Path) -> None:
    _write_config(tmp_path, _CLEAN_CONFIG)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    assert policy.violations() == []


def test_empty_config_projects_compliant_defaults() -> None:
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, None)
    assert policy.storage_backend == "memory"
    assert policy.violations() == []


def test_cloud_storage_sink_is_a_violation(tmp_path: Path) -> None:
    _write_config(tmp_path, "goal: x\nstorage:\n  backend: postgres\n  database_url: postgres://db.cloud/x\n")
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    problems = policy.violations()
    assert any("storage.backend" in p for p in problems)


def test_enabled_catalog_is_a_violation(tmp_path: Path) -> None:
    _write_config(tmp_path, "goal: x\ncatalogs:\n  - name: hub\n    enabled: true\n")
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    assert any("catalog" in p for p in policy.violations())


def test_non_eu_endpoint_is_a_violation(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "goal: x\nrole_model_policy:\n  developer:\n    base_url: https://api.openai.com/v1\n    model: gpt-x\n",
    )
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    assert any("endpoint" in p for p in policy.violations())


def test_local_endpoint_is_compliant(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "goal: x\nrole_model_policy:\n  developer:\n    base_url: http://10.0.0.5:11434/v1\n    model: m\n",
    )
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    assert policy.violations() == []


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:11434", True),
        ("http://localhost:8000/v1", True),
        ("http://10.0.0.5:11434", True),
        ("http://192.168.1.9:8000", True),
        ("http://vllm.internal:8000", True),
        ("http://ollama.svc.cluster.local:11434", True),
        ("https://api.openai.com/v1", False),
        ("https://api.deepseek.com", False),
        ("http://172.32.5.5:8000", False),
        ("", False),
    ],
)
def test_is_local_or_eu_host(url: str, expected: bool) -> None:
    assert is_local_or_eu_host(url) is expected


def test_endpoint_profile_reference_is_resolved(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "goal: x\n"
        "local_endpoints:\n"
        "  eu:\n"
        "    base_url: http://10.0.0.5:11434/v1\n"
        "    model: deepseek-v4-flash\n"
        "role_model_policy:\n"
        "  developer:\n"
        "    endpoint: eu\n",
    )
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    endpoints = policy.to_canonical_document()["model_endpoints"]
    assert endpoints[0]["base_url"] == "http://10.0.0.5:11434/v1"
    assert endpoints[0]["model"] == "deepseek-v4-flash"
    assert policy.violations() == []


# ---------------------------------------------------------------------------
# Attestation: sign, anchor, recompute (AC2)
# ---------------------------------------------------------------------------


def test_auditor_recomputes_attested_hash_from_config_alone(tmp_path: Path) -> None:
    """AC2: recompute the effective-policy hash from the config snapshot alone."""
    _write_config(tmp_path, _CLEAN_CONFIG)
    _audit_dir(tmp_path)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    chain = AuditChainStore(_audit_dir(tmp_path))
    attestation = build_posture_attestation(workdir=tmp_path, policy=policy, timestamp=1234, chain=chain)

    # An auditor with only the config snapshot recomputes the identity.
    recomputed = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path)).posture_hash()
    assert recomputed == attestation.posture_hash

    # The chain-anchored attestation carries the same hash.
    log_files = list(_audit_dir(tmp_path).glob("*.jsonl"))
    anchored = [
        json.loads(line)
        for f in log_files
        for line in f.read_text().splitlines()
        if json.loads(line)["event_type"] == EVENT_SOVEREIGN_ATTESTATION
    ]
    assert anchored and anchored[0]["details"]["posture_hash"] == recomputed


def test_attestation_persisted_and_reloadable(tmp_path: Path) -> None:
    _write_config(tmp_path, _CLEAN_CONFIG)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    built = build_posture_attestation(
        workdir=tmp_path, policy=policy, timestamp=1234, chain=AuditChainStore(_audit_dir(tmp_path))
    )
    loaded = read_posture_attestation(tmp_path)
    assert loaded is not None
    assert loaded.posture_hash == built.posture_hash
    assert loaded.signature == built.signature


def test_attestation_signing_is_deterministic(tmp_path: Path) -> None:
    """Ed25519 is deterministic: same posture + timestamp -> same signature."""
    _write_config(tmp_path, _CLEAN_CONFIG)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    a = build_posture_attestation(
        workdir=tmp_path, policy=policy, timestamp=999, chain=AuditChainStore(_audit_dir(tmp_path))
    )
    b = build_posture_attestation(
        workdir=tmp_path, policy=policy, timestamp=999, chain=AuditChainStore(_audit_dir(tmp_path))
    )
    assert a.signature == b.signature


# ---------------------------------------------------------------------------
# Drift chaos (AC3)
# ---------------------------------------------------------------------------


def test_no_drift_when_config_unchanged(tmp_path: Path) -> None:
    _write_config(tmp_path, _CLEAN_CONFIG)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    build_posture_attestation(workdir=tmp_path, policy=policy, timestamp=1, chain=AuditChainStore(_audit_dir(tmp_path)))
    ev = evaluate_posture_drift(workdir=tmp_path, config_snapshot=load_config_snapshot(tmp_path))
    assert ev.drifted is False
    assert ev.diverging_keys == ()


def test_missing_attestation_is_drift(tmp_path: Path) -> None:
    _write_config(tmp_path, _CLEAN_CONFIG)
    ev = evaluate_posture_drift(workdir=tmp_path, config_snapshot=load_config_snapshot(tmp_path))
    assert ev.drifted is True
    assert ev.attested_hash == ""


def test_config_edit_after_attestation_drifts_and_names_key(tmp_path: Path) -> None:
    """AC3: adding a cloud storage sink after attestation drifts on that key."""
    _write_config(tmp_path, _CLEAN_CONFIG)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    chain = AuditChainStore(_audit_dir(tmp_path))
    build_posture_attestation(workdir=tmp_path, policy=policy, timestamp=1, chain=chain)

    # Mutate exactly one key: add a cloud storage sink.
    _write_config(tmp_path, "goal: x\nstorage:\n  backend: postgres\n  database_url: postgres://db.cloud/x\n")
    ev = evaluate_posture_drift(workdir=tmp_path, config_snapshot=load_config_snapshot(tmp_path))
    assert ev.drifted is True
    assert ev.diverging_keys == ("storage_backend",)

    record, sha = record_and_sign_drift(workdir=tmp_path, evaluation=ev, timestamp=2, chain=chain)
    assert sha.startswith("sha256:")
    assert record["diverging_keys"] == ["storage_backend"]

    # The drift record re-verifies as part of bernstein audit verify.
    result = verify_sovereign_attestations(_audit_dir(tmp_path))
    assert result.ok is True
    assert result.drift_count == 1
    assert result.attestation_count == 1


def test_non_certified_endpoint_refuses_without_hash_drift(tmp_path: Path) -> None:
    """AC4: a gated endpoint with no receipt refuses even when the hash is unchanged."""
    # A gated role points at a local endpoint (host-compliant) but has no
    # certification receipt on disk. The posture hash reflects only the config,
    # so it does not "drift", yet the spawn gate must still refuse.
    _write_config(
        tmp_path,
        "goal: x\nrole_model_policy:\n  developer:\n    base_url: http://10.0.0.5:11434/v1\n    model: m\n",
    )
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    build_posture_attestation(workdir=tmp_path, policy=policy, timestamp=1, chain=AuditChainStore(_audit_dir(tmp_path)))
    ev = evaluate_posture_drift(workdir=tmp_path, config_snapshot=load_config_snapshot(tmp_path))
    assert ev.drifted is False  # config unchanged -> hash matches
    assert ev.should_refuse is True  # but a gated endpoint lacks certification
    assert any("certification" in v for v in ev.violations)

    # The violation-only refusal record signs + anchors + re-verifies.
    record, _ = record_and_sign_drift(
        workdir=tmp_path, evaluation=ev, timestamp=2, chain=AuditChainStore(_audit_dir(tmp_path))
    )
    assert record["violations"]
    assert verify_sovereign_attestations(_audit_dir(tmp_path)).ok is True


def test_drift_record_is_anchored_in_chain(tmp_path: Path) -> None:
    _write_config(tmp_path, _CLEAN_CONFIG)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    chain = AuditChainStore(_audit_dir(tmp_path))
    build_posture_attestation(workdir=tmp_path, policy=policy, timestamp=1, chain=chain)
    _write_config(tmp_path, "goal: x\ncatalogs:\n  - name: hub\n    enabled: true\n")
    ev = evaluate_posture_drift(workdir=tmp_path, config_snapshot=load_config_snapshot(tmp_path))
    record_and_sign_drift(workdir=tmp_path, evaluation=ev, timestamp=2, chain=chain)
    drift_events = [
        json.loads(line)
        for f in _audit_dir(tmp_path).glob("*.jsonl")
        for line in f.read_text().splitlines()
        if json.loads(line)["event_type"] == EVENT_SOVEREIGN_DRIFT
    ]
    assert len(drift_events) == 1


# ---------------------------------------------------------------------------
# Offline verification + tamper (attestation tamper SOTA axis)
# ---------------------------------------------------------------------------


def test_verify_empty_chain_is_silent_pass(tmp_path: Path) -> None:
    result = verify_sovereign_attestations(_audit_dir(tmp_path))
    assert result.ok is True
    assert result.attestation_count == 0
    assert result.drift_count == 0


def test_tampered_attestation_body_fails_verify(tmp_path: Path) -> None:
    _write_config(tmp_path, _CLEAN_CONFIG)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    build_posture_attestation(workdir=tmp_path, policy=policy, timestamp=1, chain=AuditChainStore(_audit_dir(tmp_path)))
    # Mutate the signed effective-policy document inside the chained record.
    jf = next(iter(_audit_dir(tmp_path).glob("*.jsonl")))
    rows = []
    for line in jf.read_text().splitlines():
        obj = json.loads(line)
        if obj.get("event_type") == EVENT_SOVEREIGN_ATTESTATION:
            obj["details"]["signed_body"]["effective_policy"]["storage_backend"] = "postgres"
        rows.append(json.dumps(obj, sort_keys=True))
    jf.write_text("\n".join(rows) + "\n")

    result = verify_sovereign_attestations(_audit_dir(tmp_path))
    assert result.ok is False
    assert any("signature check failed" in e or "does not match" in e for e in result.errors)


def test_forged_drift_signature_fails_verify(tmp_path: Path) -> None:
    _write_config(tmp_path, _CLEAN_CONFIG)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    chain = AuditChainStore(_audit_dir(tmp_path))
    build_posture_attestation(workdir=tmp_path, policy=policy, timestamp=1, chain=chain)
    _write_config(tmp_path, "goal: x\nstorage:\n  backend: redis\n  redis_url: redis://cache.cloud/0\n")
    ev = evaluate_posture_drift(workdir=tmp_path, config_snapshot=load_config_snapshot(tmp_path))
    record_and_sign_drift(workdir=tmp_path, evaluation=ev, timestamp=2, chain=chain)

    jf = next(iter(_audit_dir(tmp_path).glob("*.jsonl")))
    rows = []
    for line in jf.read_text().splitlines():
        obj = json.loads(line)
        if obj.get("event_type") == EVENT_SOVEREIGN_DRIFT:
            obj["details"]["signed_body"]["diverging_keys"] = ["forged"]
        rows.append(json.dumps(obj, sort_keys=True))
    jf.write_text("\n".join(rows) + "\n")

    result = verify_sovereign_attestations(_audit_dir(tmp_path))
    assert result.ok is False


# ---------------------------------------------------------------------------
# Drift-refusal exception shape (spawn-gate contract)
# ---------------------------------------------------------------------------


def test_posture_drift_refusal_carries_record() -> None:
    exc = PostureDriftRefusal("drift", record={"k": "v"}, record_sha256="sha256:abc")
    assert exc.record == {"k": "v"}
    assert exc.record_sha256 == "sha256:abc"
    assert isinstance(exc, RuntimeError)


def test_effective_policy_document_round_trips() -> None:
    policy = EffectivePolicy(
        profile="sovereign",
        schema_version=1,
        network_egress="deny-all",
        egress_allowlist=(),
        catalog_mode="offline",
        compliance_pack="regulated",
        storage_backend="memory",
        residency_enforce_strict=True,
        residency_regions=("eu-central", "eu-west"),
        model_endpoints=(),
        catalogs=(),
    )
    doc = policy.to_canonical_document()
    assert json.loads(json.dumps(doc)) == doc


# ---------------------------------------------------------------------------
# Egress allow-list truthfulness (deny-all attestation must match runtime)
# ---------------------------------------------------------------------------


def test_deny_all_egress_by_default(tmp_path: Path) -> None:
    _write_config(tmp_path, _CLEAN_CONFIG)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    doc = policy.to_canonical_document()
    assert doc["network_egress"] == "deny-all"
    assert doc["egress_allowlist"] == []


def test_local_egress_allowlist_is_compliant_and_attested(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "goal: x\nsovereign:\n  enabled: true\n  allowed_egress: ['10.0.0.5:11434', 'vllm.internal:8000']\n",
    )
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    doc = policy.to_canonical_document()
    assert doc["network_egress"] == "allow-list"
    assert doc["egress_allowlist"] == ["10.0.0.5:11434", "vllm.internal:8000"]
    assert policy.violations() == []


def test_public_egress_allowlist_is_a_violation(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "goal: x\nsovereign:\n  enabled: true\n  allowed_egress: ['api.openai.com:443']\n",
    )
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    assert any("egress" in p for p in policy.violations())


def test_egress_allowlist_changes_posture_hash(tmp_path: Path) -> None:
    _write_config(tmp_path, _CLEAN_CONFIG)
    base = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path)).posture_hash()
    _write_config(tmp_path, "goal: x\nsovereign:\n  enabled: true\n  allowed_egress: ['10.0.0.5:11434']\n")
    changed = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path)).posture_hash()
    assert base != changed
