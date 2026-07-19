"""Sovereign-profile hardening regression suite (issue #2638).

The headline invariant is empirical, not documentary: whatever egress posture
the signed attestation claims must equal the egress posture the runtime network
policy actually enforces. Everything else in this module guards the paths that
could let the two diverge -- a half-set marker pair that skips the gate, an
unreadable config that silently resolves to a permissive default, an
under-validated signed record that is trusted on its own say-so, and a resume
spawn that never re-checks the posture.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.deployment_profile import (
    SOVEREIGN_PROFILE,
    PostureAttestation,
    PostureDriftRefusal,
    SovereignConfigError,
    attestation_path,
    build_posture_attestation,
    egress_attestation_mismatch,
    enforced_egress_posture,
    evaluate_posture_drift,
    load_config_snapshot,
    read_posture_attestation,
    resolve_effective_policy,
    sovereign_egress_allowlist,
)
from bernstein.core.security.network_policy import (
    ENV_NETWORK_POLICY,
    ENV_PROFILE_MODE,
    ENV_SOVEREIGN_MODE,
    PROFILE_AIRGAP,
    NetworkPolicy,
    SovereignMarkerError,
    is_sovereign_profile,
    policy_from_env,
)

_SOVEREIGN_ENV = (ENV_PROFILE_MODE, ENV_NETWORK_POLICY, ENV_SOVEREIGN_MODE)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a process with no profile markers installed.

    ``install_policy`` writes ``os.environ`` directly, so a bare
    ``monkeypatch.delenv(..., raising=False)`` on an already-absent variable
    would record nothing and leave the marker set for the next module. Touching
    each variable with ``setenv`` first registers the pre-test state (including
    "absent") so teardown always restores it.
    """
    for var in _SOVEREIGN_ENV:
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var, raising=False)


def _write_config(workdir: Path, body: str) -> None:
    (workdir / ".sdd" / "audit").mkdir(parents=True, exist_ok=True)
    (workdir / "bernstein.yaml").write_text(body, encoding="utf-8")


def _activate(workdir: Path, allow_network: tuple[str, ...] = ()) -> None:
    """Run the real CLI activation path (network policy + attestation)."""
    from bernstein.cli.run_bootstrap import _activate_sovereign_profile, _install_profile_network_policy

    _install_profile_network_policy(run_profile=SOVEREIGN_PROFILE, allow_network=allow_network, workdir=workdir)
    _activate_sovereign_profile(run_profile=SOVEREIGN_PROFILE, workdir=workdir)


@pytest.fixture(autouse=True)
def _restore_socket_guard() -> Any:
    """Restore ``socket.socket`` exactly as this test found it.

    The activation path installs the runtime socket guard, which patches
    ``socket.socket.connect`` on the class and stashes the pre-patch callable in
    a class attribute. Calling ``uninstall_runtime_socket_guard()`` blindly is
    not safe here: if a stale install flag is left over from an earlier module,
    uninstall restores *that* module's captured connect over the one currently
    installed, permanently swapping in a foreign patch. Snapshotting and
    restoring the three pieces of state is exact and cannot leak either way.
    """
    import socket

    from bernstein.core.security.socket_guard import _INSTALLED_FLAG, _ORIGINAL_FLAG

    sentinel = object()
    prior_connect = socket.socket.connect
    prior_installed = getattr(socket.socket, _INSTALLED_FLAG, sentinel)
    prior_original = getattr(socket.socket, _ORIGINAL_FLAG, sentinel)
    try:
        yield
    finally:
        socket.socket.connect = prior_connect  # type: ignore[method-assign]
        for flag, value in ((_INSTALLED_FLAG, prior_installed), (_ORIGINAL_FLAG, prior_original)):
            if value is sentinel:
                if hasattr(socket.socket, flag):
                    delattr(socket.socket, flag)
            else:
                setattr(socket.socket, flag, value)


def _preflight(workdir: Path) -> None:
    AgentSpawner._preflight_posture_drift(SimpleNamespace(_workdir=workdir))  # type: ignore[arg-type]


def _spawner_shim(workdir: Path) -> SimpleNamespace:
    """A spawner stand-in exposing only what the gate touches.

    ``spawn_for_resume`` calls ``self._preflight_posture_drift()``, so the shim
    binds the real unbound method to itself. Any refusal therefore comes from
    the production gate, not from the shim running out of attributes.
    """
    shim = SimpleNamespace(_workdir=workdir)
    shim._preflight_posture_drift = lambda: AgentSpawner._preflight_posture_drift(shim)  # type: ignore[arg-type]
    return shim


# ---------------------------------------------------------------------------
# Headline invariant: attested posture == enforced posture
# ---------------------------------------------------------------------------

_COMPLIANT_DENY_ALL = "goal: x\nstorage:\n  backend: memory\n"
_COMPLIANT_ALLOW_LIST = (
    "goal: x\nstorage:\n  backend: memory\nsovereign:\n  enabled: true\n  allowed_egress:\n    - '10.0.0.5:11434'\n"
)


def _attested_egress(workdir: Path) -> tuple[str, tuple[str, ...]]:
    """Read the signed attestation from disk and project its egress claim."""
    raw = json.loads(attestation_path(workdir).read_text(encoding="utf-8"))
    document = raw["effective_policy"]
    return str(document["network_egress"]), tuple(document["egress_allowlist"])


@pytest.mark.usefixtures("clean_env")
def test_attested_posture_equals_enforced_posture_deny_all(tmp_path: Path) -> None:
    """A config declaring no egress attests deny-all and enforces deny-all."""
    _write_config(tmp_path, _COMPLIANT_DENY_ALL)
    _activate(tmp_path)

    assert _attested_egress(tmp_path) == ("deny-all", ())
    assert _attested_egress(tmp_path) == enforced_egress_posture(policy_from_env())
    assert policy_from_env().is_allowed("10.0.0.5", 11434) is False


@pytest.mark.usefixtures("clean_env")
def test_attested_posture_equals_enforced_posture_allow_list(tmp_path: Path) -> None:
    """The core defect: a policy that allows a destination must NOT attest deny-all.

    The runtime genuinely reaches ``10.0.0.5:11434``; the signed attestation must
    say so, byte-for-byte, instead of claiming a deny-all posture it does not
    enforce.
    """
    _write_config(tmp_path, _COMPLIANT_ALLOW_LIST)
    _activate(tmp_path)

    attested = _attested_egress(tmp_path)
    assert attested != ("deny-all", ()), "attestation claims deny-all while the runtime allows a destination"
    assert attested == ("allow-list", ("10.0.0.5:11434",))
    # Empirical equality: the attested claim equals what the live policy enforces.
    assert attested == enforced_egress_posture(policy_from_env())
    assert policy_from_env().is_allowed("10.0.0.5", 11434) is True
    assert policy_from_env().is_allowed("api.example.com", 443) is False


@pytest.mark.usefixtures("clean_env")
def test_attested_allowlist_tokens_are_exactly_the_enforced_rules(tmp_path: Path) -> None:
    """Token normalisation must not let the attested list drift from the rules."""
    _write_config(
        tmp_path,
        "goal: x\nstorage:\n  backend: memory\nsovereign:\n"
        "  allowed_egress: ['none', '10.0.0.0/8', ' 10.0.0.5:11434 ', '10.0.0.0/8']\n",
    )
    _activate(tmp_path)

    attested_mode, attested_tokens = _attested_egress(tmp_path)
    runtime = policy_from_env()
    assert attested_mode == "allow-list"
    assert attested_tokens == tuple(runtime.rules)
    assert (attested_mode, attested_tokens) == enforced_egress_posture(runtime)


def test_egress_mismatch_is_detected() -> None:
    """The invariant check itself must catch a deny-all claim over an open runtime."""
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, {"storage": {"backend": "memory"}})
    assert policy.network_egress == "deny-all"
    assert egress_attestation_mismatch(policy, NetworkPolicy.deny_all()) is None
    assert egress_attestation_mismatch(policy, NetworkPolicy.allow_all()) is not None
    assert egress_attestation_mismatch(policy, NetworkPolicy.from_specs(("10.0.0.5",))) is not None


def test_sovereign_egress_allowlist_normalises_tokens() -> None:
    """Normalisation is deterministic: sorted, de-duplicated, ``none`` dropped."""
    config = {"sovereign": {"allowed_egress": [" 10.0.0.5:11434 ", "NONE", "10.0.0.0/8", "10.0.0.0/8"]}}
    assert sovereign_egress_allowlist(config) == ("10.0.0.0/8", "10.0.0.5:11434")
    assert sovereign_egress_allowlist({"sovereign": {"allowed_egress": ["none"]}}) == ()


def test_sovereign_egress_allowlist_preserves_token_case() -> None:
    """Case is preserved: the runtime never folds it, so neither may we.

    Folding here would be an asymmetric normalisation against the runtime and
    would move ``posture_hash`` for configs whose attestation was already
    truthful. See the v1 hash-parity test below.
    """
    config = {"sovereign": {"allowed_egress": ["Box.Internal:443"]}}
    assert sovereign_egress_allowlist(config) == ("Box.Internal:443",)
    runtime = NetworkPolicy.from_specs(("Box.Internal:443",))
    assert enforced_egress_posture(runtime) == ("allow-list", ("Box.Internal:443",))


#: Posture hashes for configs whose pre-existing attestation was already
#: truthful. These are byte-identical to the values ``main`` produces, and they
#: must not move without an ``EFFECTIVE_POLICY_SCHEMA_VERSION`` bump: the
#: documented contract is that an auditor recomputes the identical hash from the
#: config file alone, so a silent projection change invalidates every attestation
#: an operator has already collected.
_V1_GOLDEN_POSTURE_HASHES: dict[str, tuple[dict[str, Any], str]] = {
    "bare": (
        {"storage": {"backend": "memory"}},
        "sha256:0eb7b68baa46bd75bfde2d677516ba04a777e155ba6483bfadbf2a793f25c4f6",
    ),
    "empty_egress": (
        {"storage": {"backend": "memory"}, "sovereign": {"allowed_egress": []}},
        "sha256:0eb7b68baa46bd75bfde2d677516ba04a777e155ba6483bfadbf2a793f25c4f6",
    ),
    "single_token": (
        {"storage": {"backend": "memory"}, "sovereign": {"allowed_egress": ["10.0.0.5:11434"]}},
        "sha256:3d2e59dfedcd5202e277c0c4d2ef960f67e9bc6a43185ee73da6a6a61d7026c0",
    ),
    "cidr": (
        {"storage": {"backend": "memory"}, "sovereign": {"allowed_egress": ["10.0.0.0/8"]}},
        "sha256:f3bff9e6fadb1186b685f0b02b6c1cf4438a00af1f83852351002089508c8625",
    ),
    "mixed_case": (
        {"storage": {"backend": "memory"}, "sovereign": {"allowed_egress": ["Box.Internal:443"]}},
        "sha256:35d8a113e9b736ea32926e8dcae7c470ca2cb85a763e8ba9899ae13639c1e78f",
    ),
    "whitespace": (
        {"storage": {"backend": "memory"}, "sovereign": {"allowed_egress": ["  10.0.0.5:11434  "]}},
        "sha256:3d2e59dfedcd5202e277c0c4d2ef960f67e9bc6a43185ee73da6a6a61d7026c0",
    ),
    "duplicates": (
        {"storage": {"backend": "memory"}, "sovereign": {"allowed_egress": ["10.0.0.0/8", "10.0.0.0/8"]}},
        "sha256:f3bff9e6fadb1186b685f0b02b6c1cf4438a00af1f83852351002089508c8625",
    ),
    "endpoints_catalogs": (
        {
            "storage": {"backend": "memory"},
            "catalogs": [{"name": "core", "enabled": False}],
            "role_model_policy": {"planner": {"base_url": "http://10.0.0.5:11434/v1", "model": "m"}},
        },
        "sha256:715efdab5aa936ce5238ed29eb54dc27b35deaebfcca830a4d46d57afddbdd4b",
    ),
}


@pytest.mark.parametrize("shape", sorted(_V1_GOLDEN_POSTURE_HASHES))
def test_v1_posture_hash_parity_for_truthful_configs(shape: str) -> None:
    """A truthful config's posture hash must stay byte-identical to v1."""
    config, expected = _V1_GOLDEN_POSTURE_HASHES[shape]
    assert resolve_effective_policy(SOVEREIGN_PROFILE, config).posture_hash() == expected


@pytest.mark.parametrize(
    "allowed_egress",
    [["none"], ["none", "10.0.0.5:11434"], ["NONE", "10.0.0.5:11434"]],
)
def test_none_bearing_configs_are_the_only_ones_whose_hash_moves(allowed_egress: list[str]) -> None:
    """The hash movement this PR causes is confined to configs that were lying.

    A ``none`` token in the list is discarded by ``NetworkPolicy.from_specs``,
    so the previous projection attested a token the runtime never enforced -
    and an all-``none`` list attested ``allow-list`` over a deny-all runtime.
    Dropping ``none`` is therefore required by the invariant, and the resulting
    hash change only invalidates attestations that were false.
    """
    config = {"storage": {"backend": "memory"}, "sovereign": {"allowed_egress": allowed_egress}}
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, config)
    real = [t for t in allowed_egress if t.lower() != "none"]

    assert "none" not in [t.lower() for t in policy.egress_allowlist]
    if real:
        assert policy.network_egress == "allow-list"
        runtime = NetworkPolicy.from_specs(tuple(allowed_egress))
    else:
        assert policy.network_egress == "deny-all"
        runtime = NetworkPolicy.deny_all()
    # The whole point: the projection now equals what the runtime enforces.
    assert egress_attestation_mismatch(policy, runtime) is None


# ---------------------------------------------------------------------------
# Enforcement before attestation
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_env")
def test_activation_refuses_to_attest_a_non_compliant_posture(tmp_path: Path) -> None:
    """A violating posture must never be sealed as a sovereign attestation."""
    _write_config(tmp_path, "goal: x\nstorage:\n  backend: postgres\n")
    with pytest.raises(SystemExit):
        _activate(tmp_path)
    assert not attestation_path(tmp_path).is_file(), "a non-compliant posture was attested"


@pytest.mark.usefixtures("clean_env")
def test_activation_refusal_is_anchored_as_a_signed_record(tmp_path: Path) -> None:
    """The refusal is evidence on the chain, not just a console message."""
    from bernstein.core.security.audit import AuditLog
    from bernstein.core.security.audit_chain import EVENT_SOVEREIGN_DRIFT
    from bernstein.core.security.deployment_profile import verify_sovereign_attestations

    _write_config(tmp_path, "goal: x\nstorage:\n  backend: postgres\n")
    with pytest.raises(SystemExit):
        _activate(tmp_path)
    audit_dir = tmp_path / ".sdd" / "audit"
    assert AuditLog(audit_dir=audit_dir).query(event_type=EVENT_SOVEREIGN_DRIFT), (
        "no signed refusal record was anchored"
    )
    # The refusal must survive the same offline verification an auditor runs.
    result = verify_sovereign_attestations(audit_dir)
    assert result.ok is True, result.errors
    assert result.attestation_count == 0, "a refused activation must not attest a posture"
    assert result.drift_count == 1


@pytest.mark.usefixtures("clean_env")
def test_activation_refuses_a_public_egress_destination(tmp_path: Path) -> None:
    """Egress to a non-local destination is refused before anything is attested."""
    _write_config(
        tmp_path,
        "goal: x\nstorage:\n  backend: memory\nsovereign:\n  allowed_egress: ['api.example.com:443']\n",
    )
    with pytest.raises(SystemExit):
        _activate(tmp_path)
    assert not attestation_path(tmp_path).is_file()


# ---------------------------------------------------------------------------
# Fail-closed source configuration
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_env")
def test_missing_config_fails_closed_on_activation(tmp_path: Path) -> None:
    (tmp_path / ".sdd" / "audit").mkdir(parents=True, exist_ok=True)
    with pytest.raises(SystemExit):
        _activate(tmp_path)
    assert not attestation_path(tmp_path).is_file()


@pytest.mark.usefixtures("clean_env")
def test_unreadable_config_fails_closed_on_activation(tmp_path: Path) -> None:
    _write_config(tmp_path, "goal: x\nstorage: [unbalanced\n")
    with pytest.raises(SystemExit):
        _activate(tmp_path)
    assert not attestation_path(tmp_path).is_file()


def test_load_config_snapshot_require_raises(tmp_path: Path) -> None:
    with pytest.raises(SovereignConfigError):
        load_config_snapshot(tmp_path, require=True)
    (tmp_path / "bernstein.yaml").write_text("goal: x\nstorage: [unbalanced\n", encoding="utf-8")
    with pytest.raises(SovereignConfigError):
        load_config_snapshot(tmp_path, require=True)
    (tmp_path / "bernstein.yaml").write_text("- not-a-mapping\n", encoding="utf-8")
    with pytest.raises(SovereignConfigError):
        load_config_snapshot(tmp_path, require=True)


def test_unreadable_config_refuses_the_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate must refuse rather than fall back to a permissive default posture."""
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    _write_config(tmp_path, _COMPLIANT_DENY_ALL)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    build_posture_attestation(
        workdir=tmp_path, policy=policy, timestamp=1, chain=AuditChainStore(tmp_path / ".sdd" / "audit")
    )
    _preflight(tmp_path)  # baseline: clean posture spawns fine

    (tmp_path / "bernstein.yaml").write_text("goal: x\nstorage: [unbalanced\n", encoding="utf-8")
    with pytest.raises(PostureDriftRefusal) as excinfo:
        _preflight(tmp_path)
    assert any("unreadable" in v or "missing" in v for v in excinfo.value.record["violations"])


# ---------------------------------------------------------------------------
# Signed-record contract validation
# ---------------------------------------------------------------------------


def _seal(tmp_path: Path) -> PostureAttestation:
    _write_config(tmp_path, _COMPLIANT_DENY_ALL)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(tmp_path))
    return build_posture_attestation(
        workdir=tmp_path, policy=policy, timestamp=1, chain=AuditChainStore(tmp_path / ".sdd" / "audit")
    )


_SEAL_FIELDS = ("signature", "signer_public_key_pem", "journal_entry_hash")


def _resign_body(raw: dict[str, Any], private_pem: str) -> None:
    """Re-sign *raw* in place so its signature matches its mutated body.

    Without this a test that mutates authenticated content is rejected by the
    signature check and never reaches the validator it claims to exercise - it
    would pass even if the required-field, schema, profile, or posture-hash
    checks were deleted. Re-signing makes the signature valid so the *only*
    thing left that can reject the record is the contract check under test.
    """
    from bernstein.core.security.deployment_profile import _canonical_bytes
    from bernstein.core.skills.catalog.signature import sign_payload

    body = {k: v for k, v in raw.items() if k not in _SEAL_FIELDS}
    raw["signature"] = sign_payload(_canonical_bytes(body), private_pem)


def _install_private_key(tmp_path: Path) -> str:
    """Return the install's sovereign private key PEM (created by ``_seal``)."""
    from bernstein.core.security.deployment_profile import load_or_create_sovereign_identity

    private_pem, _ = load_or_create_sovereign_identity(tmp_path / ".sdd" / "sovereign")
    return private_pem


def _rewrite_attestation(tmp_path: Path, mutate: Any, *, resign: bool = False) -> None:
    raw = json.loads(attestation_path(tmp_path).read_text(encoding="utf-8"))
    mutate(raw)
    if resign:
        _resign_body(raw, _install_private_key(tmp_path))
    attestation_path(tmp_path).write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")


@pytest.mark.parametrize(
    "field",
    [
        "record_kind",
        "profile",
        "schema_version",
        "posture_hash",
        "effective_policy",
        "timestamp",
        "signature",
        "signer_public_key_pem",
    ],
)
def test_incomplete_signed_record_is_rejected(tmp_path: Path, field: str) -> None:
    """Every field of the signed-record contract is required before we trust it.

    The record is re-signed after the field is dropped, so the signature is
    valid and only the required-field check can reject it.
    """
    _seal(tmp_path)
    resign = field not in _SEAL_FIELDS
    _rewrite_attestation(tmp_path, lambda raw: raw.pop(field), resign=resign)
    assert read_posture_attestation(tmp_path) is None


def test_unsigned_signed_record_is_rejected(tmp_path: Path) -> None:
    """An attestation whose seal fields are blank is not a signed record."""
    _seal(tmp_path)
    _rewrite_attestation(tmp_path, lambda raw: raw.update({"signature": "", "signer_public_key_pem": ""}))
    assert read_posture_attestation(tmp_path) is None


def test_forged_signed_record_is_rejected(tmp_path: Path) -> None:
    """A hand-edited posture with a stale signature must not be trusted."""
    _seal(tmp_path)
    _rewrite_attestation(tmp_path, lambda raw: raw["effective_policy"].update({"storage_backend": "postgres"}))
    assert read_posture_attestation(tmp_path) is None


def test_resigned_forgery_is_rejected_by_the_posture_hash(tmp_path: Path) -> None:
    """Editing the document and re-signing still fails: the hash no longer matches."""
    _seal(tmp_path)
    _rewrite_attestation(
        tmp_path,
        lambda raw: raw["effective_policy"].update({"storage_backend": "postgres"}),
        resign=True,
    )
    assert read_posture_attestation(tmp_path) is None


def test_record_signed_by_a_foreign_key_is_rejected(tmp_path: Path) -> None:
    """A fully self-consistent record signed by someone else is not our posture.

    Rewriting the document, generating a fresh keypair, signing with it and
    embedding its public key produces a record that verifies perfectly against
    itself. It must still be refused, because the signer is not this install's
    sovereign identity.
    """
    from bernstein.core.security.deployment_profile import _canonical_bytes, _sha256_of
    from bernstein.core.skills.catalog.signature import generate_signer_keypair, sign_payload, verify_payload

    _seal(tmp_path)
    foreign_private, foreign_public = generate_signer_keypair()
    raw = json.loads(attestation_path(tmp_path).read_text(encoding="utf-8"))
    raw["effective_policy"]["storage_backend"] = "postgres"
    raw["posture_hash"] = _sha256_of(raw["effective_policy"])
    body = {k: v for k, v in raw.items() if k not in _SEAL_FIELDS}
    raw["signature"] = sign_payload(_canonical_bytes(body), foreign_private)
    raw["signer_public_key_pem"] = foreign_public
    attestation_path(tmp_path).write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")

    # Premise: the forged record really is internally consistent.
    assert verify_payload(_canonical_bytes(body), raw["signature"], foreign_public, allow_unverified=True).verified
    # It is still refused, because the key is not the install's identity.
    assert read_posture_attestation(tmp_path) is None


def test_wrong_schema_version_is_rejected(tmp_path: Path) -> None:
    _seal(tmp_path)
    _rewrite_attestation(tmp_path, lambda raw: raw.update({"schema_version": 99}), resign=True)
    assert read_posture_attestation(tmp_path) is None


def test_wrong_record_kind_is_rejected(tmp_path: Path) -> None:
    """A drift record renamed into an attestation must not be reinterpreted."""
    _seal(tmp_path)
    _rewrite_attestation(tmp_path, lambda raw: raw.update({"record_kind": "sovereign_drift"}), resign=True)
    assert read_posture_attestation(tmp_path) is None


# The checks inside ``from_dict`` are exercised directly below rather than only
# through ``read_posture_attestation``. Going through the file path lets a
# neighbouring check (signature verification, the posture-hash recompute, the
# signer anchor) reject the record first, so those tests stay green even if the
# validator under test is deleted. Calling the validator where it lives is the
# only way to prove it carries its own weight.


def _valid_attestation_dict(tmp_path: Path) -> dict[str, Any]:
    _seal(tmp_path)
    return json.loads(attestation_path(tmp_path).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "field",
    [
        "record_kind",
        "profile",
        "schema_version",
        "posture_hash",
        "effective_policy",
        "timestamp",
        "signature",
        "signer_public_key_pem",
    ],
)
def test_from_dict_requires_each_contract_field(tmp_path: Path, field: str) -> None:
    """The required-field check rejects on its own, not via a later check."""
    raw = _valid_attestation_dict(tmp_path)
    raw.pop(field)
    with pytest.raises(ValueError, match="missing required field"):
        PostureAttestation.from_dict(raw)


def test_from_dict_rejects_a_foreign_record_kind(tmp_path: Path) -> None:
    raw = _valid_attestation_dict(tmp_path)
    raw["record_kind"] = "sovereign_drift"
    with pytest.raises(ValueError, match="record_kind"):
        PostureAttestation.from_dict(raw)


@pytest.mark.parametrize("seal", ["posture_hash", "signature", "signer_public_key_pem"])
def test_from_dict_rejects_a_blank_seal_field(tmp_path: Path, seal: str) -> None:
    """A blank seal is rejected here, not left to the signature check."""
    raw = _valid_attestation_dict(tmp_path)
    raw[seal] = "   "
    with pytest.raises(ValueError, match=seal):
        PostureAttestation.from_dict(raw)


def test_from_dict_rejects_a_foreign_profile(tmp_path: Path) -> None:
    raw = _valid_attestation_dict(tmp_path)
    raw["profile"] = "airgap"
    with pytest.raises(ValueError, match="profile"):
        PostureAttestation.from_dict(raw)


def test_from_dict_rejects_a_mistyped_timestamp(tmp_path: Path) -> None:
    raw = _valid_attestation_dict(tmp_path)
    raw["timestamp"] = "1"
    with pytest.raises(ValueError, match="timestamp"):
        PostureAttestation.from_dict(raw)


def test_corrupt_signature_alone_is_rejected(tmp_path: Path) -> None:
    """Isolates signature verification from every other check.

    The body is untouched, so the posture hash still recomputes and the contract
    is complete; the signer key is still the install's own, so the anchor
    matches. Only the signature bytes are corrupted, which leaves the signature
    check as the single thing that can reject this record.
    """
    from bernstein.core.security.deployment_profile import _canonical_bytes
    from bernstein.core.skills.catalog.signature import verify_payload

    attestation = _seal(tmp_path)
    raw = json.loads(attestation_path(tmp_path).read_text(encoding="utf-8"))

    # Corrupt one byte of the signature, keeping it well-formed base64url.
    signature = raw["signature"]
    flipped = ("B" if signature[0] != "B" else "C") + signature[1:]
    raw["signature"] = flipped
    attestation_path(tmp_path).write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")

    # Premise: everything except the signature still checks out.
    parsed = PostureAttestation.from_dict(raw)  # contract + hash recompute pass
    assert parsed.signer_public_key_pem == attestation.signer_public_key_pem  # anchor matches
    assert not verify_payload(
        _canonical_bytes(parsed.signed_body()), flipped, parsed.signer_public_key_pem, allow_unverified=True
    ).verified

    assert read_posture_attestation(tmp_path) is None


def test_posture_hash_must_match_the_recorded_document(tmp_path: Path) -> None:
    _seal(tmp_path)
    _rewrite_attestation(tmp_path, lambda raw: raw.update({"posture_hash": "sha256:" + "0" * 64}), resign=True)
    assert read_posture_attestation(tmp_path) is None


def test_untrusted_record_refuses_the_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rejecting a record must fail closed at the gate, not silently pass."""
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    _seal(tmp_path)
    _preflight(tmp_path)  # baseline
    _rewrite_attestation(tmp_path, lambda raw: raw.update({"signature": ""}))
    with pytest.raises(PostureDriftRefusal):
        _preflight(tmp_path)


def test_verify_rejects_a_record_with_no_effective_policy(tmp_path: Path) -> None:
    """``audit verify`` must not skip the hash check when the document is absent.

    The mutated body is re-signed so its signature is valid; the only thing
    that can reject it is the required-field check under test.
    """
    from bernstein.core.security.deployment_profile import _canonical_bytes, verify_sovereign_attestations
    from bernstein.core.skills.catalog.signature import sign_payload

    _seal(tmp_path)
    audit_dir = tmp_path / ".sdd" / "audit"
    assert verify_sovereign_attestations(audit_dir).ok is True

    private_pem = _install_private_key(tmp_path)
    entries = sorted(audit_dir.glob("*.jsonl"))
    assert entries, "no audit chain file was written"
    target = entries[0]
    mutated = False
    patched: list[str] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        details = row.get("details", {})
        body = details.get("signed_body")
        if isinstance(body, dict) and "effective_policy" in body:
            body.pop("effective_policy")
            details["signature"] = sign_payload(_canonical_bytes(body), private_pem)
            mutated = True
        patched.append(json.dumps(row))
    assert mutated, "no sovereign record found to mutate"
    target.write_text("\n".join(patched) + "\n", encoding="utf-8")

    result = verify_sovereign_attestations(audit_dir)
    assert result.ok is False
    assert any("effective_policy" in err or "missing required field" in err for err in result.errors), result.errors


def test_verify_rejects_a_drift_record_missing_its_refusal_vocabulary(tmp_path: Path) -> None:
    """Isolates the chain required-field check for drift records.

    ``attested_hash`` has no downstream type or value check, so dropping it and
    re-signing leaves the required-field check as the only thing that can
    reject the record. Without that check the record would verify clean.
    """
    from bernstein.core.security.deployment_profile import (
        _canonical_bytes,
        record_and_sign_drift,
        verify_sovereign_attestations,
    )
    from bernstein.core.skills.catalog.signature import sign_payload

    _write_config(tmp_path, "goal: x\nstorage:\n  backend: postgres\n")
    audit_dir = tmp_path / ".sdd" / "audit"
    snapshot = load_config_snapshot(tmp_path)
    evaluation = evaluate_posture_drift(workdir=tmp_path, config_snapshot=snapshot)
    record_and_sign_drift(workdir=tmp_path, evaluation=evaluation, timestamp=1, chain=AuditChainStore(audit_dir))
    assert verify_sovereign_attestations(audit_dir).ok is True

    private_pem = _install_private_key(tmp_path)
    target = sorted(audit_dir.glob("*.jsonl"))[0]
    mutated = False
    patched: list[str] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        details = row.get("details", {})
        body = details.get("signed_body")
        if isinstance(body, dict) and "attested_hash" in body:
            body.pop("attested_hash")
            details["signature"] = sign_payload(_canonical_bytes(body), private_pem)
            mutated = True
        patched.append(json.dumps(row))
    assert mutated, "no drift record found to mutate"
    target.write_text("\n".join(patched) + "\n", encoding="utf-8")

    result = verify_sovereign_attestations(audit_dir)
    assert result.ok is False
    assert any("attested_hash" in err for err in result.errors), result.errors


# ---------------------------------------------------------------------------
# Marker pair consistency
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_env")
def test_complete_marker_pair_is_sovereign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    assert is_sovereign_profile() is True


@pytest.mark.usefixtures("clean_env")
def test_absent_markers_are_not_sovereign() -> None:
    assert is_sovereign_profile() is False


@pytest.mark.usefixtures("clean_env")
def test_half_set_marker_pair_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sovereign claimed without the airgap network posture must not be believed."""
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    with pytest.raises(SovereignMarkerError):
        is_sovereign_profile()


@pytest.mark.usefixtures("clean_env")
def test_unrecognised_marker_value_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in the marker must fail closed, not silently disable the gate."""
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "yes")
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    with pytest.raises(SovereignMarkerError):
        is_sovereign_profile()


@pytest.mark.usefixtures("clean_env")
def test_half_set_markers_do_not_bypass_the_spawn_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bypass this closes: drop one marker, keep the other, spawn anyway."""
    _seal(tmp_path)
    (tmp_path / "bernstein.yaml").write_text("goal: x\nstorage:\n  backend: postgres\n", encoding="utf-8")
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
    with pytest.raises(PostureDriftRefusal):
        _preflight(tmp_path)


@pytest.mark.usefixtures("clean_env")
def test_half_set_markers_arm_the_gate_with_no_attestation_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolates the half-set marker guard from the attestation-presence guard.

    With no attestation on disk the only thing that can arm the gate is the
    half-set marker pair. If a half-set state were read as "not sovereign" the
    gate would return early and the spawn would proceed unchecked, which is the
    bypass itself.
    """
    _write_config(tmp_path, _COMPLIANT_DENY_ALL)
    assert not attestation_path(tmp_path).exists()  # premise: nothing else can arm the gate
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    monkeypatch.delenv(ENV_PROFILE_MODE, raising=False)
    with pytest.raises(PostureDriftRefusal) as excinfo:
        _preflight(tmp_path)
    assert any("markers are inconsistent" in v for v in excinfo.value.record["violations"])


@pytest.mark.usefixtures("clean_env")
def test_absent_markers_and_no_attestation_leave_the_gate_closed(tmp_path: Path) -> None:
    """The counterpart: with neither signal the gate must stay a no-op."""
    _write_config(tmp_path, _COMPLIANT_DENY_ALL)
    _preflight(tmp_path)  # must not raise


# ---------------------------------------------------------------------------
# The auditor-facing surface must agree with the enforcement surface
# ---------------------------------------------------------------------------


def _doctor_attestation_row(workdir: Path) -> Any:
    from bernstein.core.distribution.doctor_sovereign import run_sovereign_checks

    report = run_sovereign_checks(workdir)
    row = next(c for c in report.checks if c.name == "posture attested (no drift)")
    return report, row


def test_doctor_fails_when_the_attestation_is_present_but_untrusted(tmp_path: Path) -> None:
    """A tampered record must not read as "you have not activated yet".

    Rejecting an untrusted record leaves ``attested_hash`` empty, which is also
    what a genuinely never-activated workspace looks like. Reporting the two the
    same way hands an auditor a clean bill of health at the exact moment the
    spawn gate is refusing every spawn.
    """
    from bernstein.core.distribution.doctor_sovereign import CheckStatus

    _seal(tmp_path)
    _rewrite_attestation(tmp_path, lambda raw: raw["effective_policy"].update({"storage_backend": "postgres"}))

    _, row = _doctor_attestation_row(tmp_path)
    assert row.status is CheckStatus.FAIL, f"expected FAIL, got {row.status}: {row.detail}"
    assert "not trusted" in row.detail
    # And the gate agrees, which is the point: one posture, one verdict.
    assert (
        evaluate_posture_drift(workdir=tmp_path, config_snapshot=load_config_snapshot(tmp_path)).should_refuse is True
    )


def test_doctor_still_warns_when_genuinely_never_activated(tmp_path: Path) -> None:
    """The never-activated case stays a WARN, distinct from a tampered record."""
    from bernstein.core.distribution.doctor_sovereign import CheckStatus

    _write_config(tmp_path, _COMPLIANT_DENY_ALL)
    assert not attestation_path(tmp_path).exists()
    _, row = _doctor_attestation_row(tmp_path)
    assert row.status is CheckStatus.WARN
    assert "no attestation yet" in row.detail


def _attest(workdir: Path, body: str) -> None:
    """Write *body* and seal a matching signed posture attestation on disk."""
    _write_config(workdir, body)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, load_config_snapshot(workdir))
    build_posture_attestation(
        workdir=workdir, policy=policy, timestamp=1, chain=AuditChainStore(workdir / ".sdd" / "audit")
    )


def test_doctor_fails_when_the_enforced_egress_diverges_from_the_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The posture-attested row must FAIL when the runtime egress diverges.

    The config attests an allow-list egress, and the marker pair is present and
    consistent, but the enforced ``BERNSTEIN_NETWORK_POLICY`` points at a
    different destination. ``evaluate_posture_drift`` records that mismatch as a
    live violation (``should_refuse`` is True and the spawn gate refuses), yet
    the doctor row keyed only off drift / attested-hash / rejection and reported
    a clean match. The verifier must not hand an auditor a green row at the
    moment the gate is refusing every spawn.
    """
    from bernstein.core.distribution.doctor_sovereign import CheckStatus

    _attest(tmp_path, _COMPLIANT_ALLOW_LIST)  # attests allow-list 10.0.0.5:11434
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    monkeypatch.setenv(ENV_NETWORK_POLICY, "10.0.0.6:11434")

    _, row = _doctor_attestation_row(tmp_path)
    assert row.status is CheckStatus.FAIL, f"expected FAIL, got {row.status}: {row.detail}"
    assert "does not equal the enforced" in row.detail
    # One posture, one verdict: the gate refuses the identical posture.
    with pytest.raises(PostureDriftRefusal):
        _preflight(tmp_path)


def test_doctor_fails_when_markers_stripped_leave_the_runtime_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_sovereign_checks`` must compare the attested egress against the real
    runtime, not skip the invariant when the airgap marker is absent.

    A workspace carrying a signed deny-all attestation, run in a process whose
    markers were stripped, enforces allow-all. The spawn gate refuses that
    (it passes ``runtime_policy=policy_from_env()`` explicitly). The doctor
    derived its runtime from ``_live_runtime_policy()``, which returns ``None``
    without the airgap marker, so it skipped the egress invariant and reported a
    clean match. It must instead mirror the gate.
    """
    from bernstein.core.distribution.doctor_sovereign import CheckStatus

    _attest(tmp_path, _COMPLIANT_DENY_ALL)  # deny-all attestation on disk
    for var in _SOVEREIGN_ENV:
        monkeypatch.delenv(var, raising=False)
    assert policy_from_env().allow_any is True  # premise: runtime is wide open

    _, row = _doctor_attestation_row(tmp_path)
    assert row.status is CheckStatus.FAIL, f"expected FAIL, got {row.status}: {row.detail}"
    assert "does not equal the enforced" in row.detail
    with pytest.raises(PostureDriftRefusal):
        _preflight(tmp_path)


def test_cli_doctor_simulation_installs_the_configured_egress_for_an_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The standalone doctor must simulate the egress a real activation installs.

    Run from a bare shell, ``bernstein doctor sovereign`` simulates
    ``--profile sovereign`` for the duration of the checks. A real activation
    installs the ``sovereign.allowed_egress`` allow-list from config; the
    simulation must install the same policy, or an honest allow-list workspace
    is falsely reported as an egress mismatch now that the verifier surfaces
    live violations.
    """
    from bernstein.cli.commands.doctor_sovereign_cmd import run_doctor_sovereign

    _attest(tmp_path, _COMPLIANT_ALLOW_LIST)  # honest, compliant allow-list posture
    for var in _SOVEREIGN_ENV:
        monkeypatch.delenv(var, raising=False)  # bare shell -> simulation active

    # rc 0 means no FAIL row: the posture-attested row does not report a false
    # egress mismatch against a fabricated deny-all runtime.
    rc = run_doctor_sovereign(workdir=tmp_path, as_json=False)
    assert rc == 0


def test_verify_rejects_a_chain_record_signed_by_a_foreign_key(tmp_path: Path) -> None:
    """Isolates the chain-side signer anchor.

    The rewritten body is fully self-consistent - non-compliant posture, hash
    recomputed to match, signed with a fresh keypair whose public key is
    embedded. Every structural check passes; only the anchor can reject it.
    Without the anchor this is the shape that makes the Ed25519 layer useless
    under `--merkle-only` or a compromised HMAC key.
    """
    from bernstein.core.security.deployment_profile import (
        _canonical_bytes,
        _sha256_of,
        verify_sovereign_attestations,
    )
    from bernstein.core.skills.catalog.signature import generate_signer_keypair, sign_payload, verify_payload

    _seal(tmp_path)
    audit_dir = tmp_path / ".sdd" / "audit"
    assert verify_sovereign_attestations(audit_dir).ok is True

    foreign_private, foreign_public = generate_signer_keypair()
    target = sorted(audit_dir.glob("*.jsonl"))[0]
    mutated = False
    patched: list[str] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        details = row.get("details", {})
        body = details.get("signed_body")
        if isinstance(body, dict) and "effective_policy" in body:
            body["effective_policy"]["storage_backend"] = "postgres"
            body["posture_hash"] = _sha256_of(body["effective_policy"])
            details["signature"] = sign_payload(_canonical_bytes(body), foreign_private)
            details["signer_public_key_pem"] = foreign_public
            # Premise: the forged record is internally consistent.
            assert verify_payload(
                _canonical_bytes(body), details["signature"], foreign_public, allow_unverified=True
            ).verified
            mutated = True
        patched.append(json.dumps(row))
    assert mutated, "no sovereign record found to mutate"
    target.write_text("\n".join(patched) + "\n", encoding="utf-8")

    result = verify_sovereign_attestations(audit_dir)
    assert result.ok is False
    assert any("not this install's sovereign identity" in err for err in result.errors), result.errors


def test_verify_fails_closed_when_the_trust_anchor_is_missing(tmp_path: Path) -> None:
    """Sovereign records with no identity to anchor them against cannot pass.

    Deleting the public key is the cheap way to defeat an anchor that treats
    "no anchor available" as "nothing to check", so the absence has to be an
    error rather than a skip. A workspace with zero sovereign records is
    unaffected (that stays a silent pass), so this only bites where it should.
    """
    from bernstein.core.security.deployment_profile import verify_sovereign_attestations

    _seal(tmp_path)
    audit_dir = tmp_path / ".sdd" / "audit"
    assert verify_sovereign_attestations(audit_dir).ok is True

    (tmp_path / ".sdd" / "sovereign" / "sovereign-identity-public.pem").unlink()
    result = verify_sovereign_attestations(audit_dir)
    assert result.ok is False
    assert any("no sovereign identity public key" in err for err in result.errors), result.errors


def test_verify_is_a_silent_pass_with_no_sovereign_records(tmp_path: Path) -> None:
    """A workspace that never activated sovereign must not be dragged into this."""
    from bernstein.core.security.deployment_profile import verify_sovereign_attestations

    audit_dir = tmp_path / ".sdd" / "audit"
    audit_dir.mkdir(parents=True)
    result = verify_sovereign_attestations(audit_dir)
    assert result.ok is True
    assert result.errors == []
    assert (result.attestation_count, result.drift_count) == (0, 0)


# ---------------------------------------------------------------------------
# Honest configs must not be refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    ["[::1]:8080", "[::1]", "[fd00::1]:443", "127.0.0.1", "10.0.0.5:11434", "10.0.0.0/8", "192.168.1.7"],
)
def test_local_egress_tokens_are_accepted(token: str) -> None:
    """IPv6 loopback and unique-local are self-hosted destinations like any other.

    ``[::1]`` is what ``localhost`` resolves to on a dual-stack host, so a local
    model server reached over IPv6 is an ordinary sovereign deployment. Bracket
    stripping used to hand a bare IPv6 literal to a URL parser that cannot
    represent one, which classified it as non-local - previously a spurious
    warning, and now, with enforcement before attestation, a hard refusal of an
    honest config.
    """
    from bernstein.core.security.deployment_profile import _egress_token_is_local

    assert _egress_token_is_local(token) is True


@pytest.mark.parametrize("token", ["api.example.com:443", "8.8.8.8", "1.1.1.1:443", "0.0.0.0/0", "::/0", "any"])
def test_non_local_egress_tokens_are_still_refused(token: str) -> None:
    """The counterpart: globally routable destinations must stay refused."""
    from bernstein.core.security.deployment_profile import _egress_token_is_local

    assert _egress_token_is_local(token) is False


@pytest.mark.parametrize("token", ["[2001:db8::1]:443", "192.0.2.1", "198.51.100.1:80"])
def test_documentation_prefixes_follow_the_module_locality_definition(token: str) -> None:
    """Documentation prefixes count as local, matching the endpoint path exactly.

    This module defines local as ``ip.is_loopback or ip.is_private``, and
    Python's ``is_private`` covers the RFC 5737 / RFC 3849 documentation ranges
    for both families - so ``is_local_or_eu_host`` already classifies
    ``192.0.2.1`` as local on the endpoint path. Pinning the same answer for
    egress tokens keeps the two paths from disagreeing about the same address.
    These prefixes are unroutable by definition, so they are not an egress path.
    """
    from bernstein.core.security.deployment_profile import _egress_token_is_local, is_local_or_eu_host

    assert _egress_token_is_local(token) is True
    bare = (
        token[1 : token.index("]")]
        if token.startswith("[")
        else token.rsplit(":", 1)[0]
        if (token.count(":") == 1)
        else token
    )
    url = f"http://[{bare}]/" if ":" in bare else f"http://{bare}/"
    assert is_local_or_eu_host(url) is True, "egress and endpoint paths must agree on the same address"


@pytest.mark.usefixtures("clean_env")
def test_ipv6_loopback_egress_activates_cleanly(tmp_path: Path) -> None:
    """End-to-end: an IPv6-loopback allow-list activates and stays truthful."""
    _write_config(
        tmp_path,
        "goal: x\nstorage:\n  backend: memory\nsovereign:\n  allowed_egress: ['[::1]:8080']\n",
    )
    _activate(tmp_path)
    assert attestation_path(tmp_path).is_file()
    assert _attested_egress(tmp_path) == enforced_egress_posture(policy_from_env())
    assert policy_from_env().is_allowed("::1", 8080) is True


@pytest.mark.usefixtures("clean_env")
def test_dropping_the_sovereign_marker_does_not_bypass_an_attested_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace carrying a signed attestation stays gated even with no markers."""
    _seal(tmp_path)
    (tmp_path / "bernstein.yaml").write_text("goal: x\nstorage:\n  backend: postgres\n", encoding="utf-8")
    for var in _SOVEREIGN_ENV:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(PostureDriftRefusal):
        _preflight(tmp_path)


@pytest.mark.usefixtures("clean_env")
def test_policy_from_env_fails_closed_under_a_network_locked_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stripped policy variable must not reopen egress under a locked profile."""
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    monkeypatch.delenv(ENV_NETWORK_POLICY, raising=False)
    assert policy_from_env().allow_any is False
    monkeypatch.setenv(ENV_NETWORK_POLICY, "")
    assert policy_from_env().allow_any is False


@pytest.mark.usefixtures("clean_env")
def test_policy_from_env_keeps_back_compat_outside_locked_profiles() -> None:
    assert policy_from_env().allow_any is True


@pytest.mark.usefixtures("clean_env")
def test_install_policy_clears_markers_it_does_not_assert() -> None:
    """A second install must not inherit the first one's markers.

    Leaving a stale sovereign or airgap marker behind is how a later
    non-sovereign run in the same process ends up in the half-set state.
    """
    import os

    from bernstein.core.security.network_policy import install_policy

    install_policy(NetworkPolicy.deny_all(), profile=PROFILE_AIRGAP, sovereign=True)
    assert is_sovereign_profile() is True

    install_policy(NetworkPolicy.allow_all())
    assert os.environ.get(ENV_SOVEREIGN_MODE) is None
    assert os.environ.get(ENV_PROFILE_MODE) is None
    assert is_sovereign_profile() is False


@pytest.mark.usefixtures("clean_env")
def test_install_policy_refuses_sovereign_without_the_airgap_profile() -> None:
    from bernstein.core.security.network_policy import install_policy

    with pytest.raises(SovereignMarkerError):
        install_policy(NetworkPolicy.deny_all(), sovereign=True)


def test_attested_workspace_refuses_an_open_runtime_with_markers_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripping the markers must not skip the egress invariant.

    With no markers the process policy is allow-all, while the workspace still
    carries a signed deny-all attestation. That is the mismatch the gate exists
    to catch, so it must refuse rather than pass on an unchanged config hash.
    """
    _seal(tmp_path)
    for var in _SOVEREIGN_ENV:
        monkeypatch.delenv(var, raising=False)
    assert policy_from_env().allow_any is True  # premise: runtime is wide open
    with pytest.raises(PostureDriftRefusal) as excinfo:
        _preflight(tmp_path)
    assert any("does not equal the enforced runtime policy" in v for v in excinfo.value.record["violations"])


# ---------------------------------------------------------------------------
# Resume-path drift gating
# ---------------------------------------------------------------------------


def test_resume_spawn_is_drift_gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Behavioural proof (not source inspection) that resume applies the gate."""
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    _seal(tmp_path)
    (tmp_path / "bernstein.yaml").write_text("goal: x\nstorage:\n  backend: postgres\n", encoding="utf-8")
    tasks = [SimpleNamespace(id="t1", role="developer")]
    with pytest.raises(PostureDriftRefusal):
        AgentSpawner.spawn_for_resume(  # type: ignore[arg-type]
            _spawner_shim(tmp_path),
            tasks,  # type: ignore[arg-type]
            worktree_path=tmp_path / "wt",
            changed_files=[],
        )


def test_resume_gate_runs_before_any_worktree_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal must precede adapter/worktree side effects, so a shim suffices."""
    monkeypatch.setenv(ENV_SOVEREIGN_MODE, "1")
    monkeypatch.setenv(ENV_PROFILE_MODE, PROFILE_AIRGAP)
    _write_config(tmp_path, _COMPLIANT_DENY_ALL)  # attested never -> refusal
    tasks = [SimpleNamespace(id="t1", role="developer")]
    with pytest.raises(PostureDriftRefusal):
        AgentSpawner.spawn_for_resume(  # type: ignore[arg-type]
            _spawner_shim(tmp_path),
            tasks,  # type: ignore[arg-type]
            worktree_path=tmp_path / "wt",
            changed_files=[],
        )
    assert not (tmp_path / "wt").exists()


def test_evaluate_posture_drift_folds_in_extra_violations(tmp_path: Path) -> None:
    _write_config(tmp_path, _COMPLIANT_DENY_ALL)
    snapshot = load_config_snapshot(tmp_path)
    policy = resolve_effective_policy(SOVEREIGN_PROFILE, snapshot)
    build_posture_attestation(
        workdir=tmp_path, policy=policy, timestamp=1, chain=AuditChainStore(tmp_path / ".sdd" / "audit")
    )
    clean = evaluate_posture_drift(workdir=tmp_path, config_snapshot=snapshot)
    assert clean.should_refuse is False
    gated = evaluate_posture_drift(
        workdir=tmp_path, config_snapshot=snapshot, extra_violations=("markers are half-set",)
    )
    assert gated.should_refuse is True
    assert "markers are half-set" in gated.violations
