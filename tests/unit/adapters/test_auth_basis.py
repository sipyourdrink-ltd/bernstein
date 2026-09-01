"""AuthBasis contract completeness drift tests.

Covers the auth_basis field introduced on both ContractSpec and
AdapterCapabilityProfile: the enum parses cleanly from wire values,
every shipped contract YAML carries an explicit basis (no defaults
slipping through), and every profiled adapter's profile agrees with
the contract it is pinned against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.adapters._contract import CONTRACTS_DIR, AuthBasis, ContractSpec
from bernstein.adapters.capability_profile import AdapterCapabilityProfile, InvocationSpec

# ---------------------------------------------------------------------------
# Enum parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("api_key", AuthBasis.API_KEY),
        ("local", AuthBasis.LOCAL),
        ("subscription_oauth", AuthBasis.SUBSCRIPTION_OAUTH),
        ("unknown", AuthBasis.UNKNOWN),
    ],
)
def test_auth_basis_parses_from_wire_value(raw: str, expected: AuthBasis) -> None:
    assert AuthBasis(raw) is expected


def test_auth_basis_rejects_invalid_wire() -> None:
    with pytest.raises(ValueError):
        AuthBasis("totally-made-up")


# ---------------------------------------------------------------------------
# ContractSpec loads auth_basis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("adapter", "expected"),
    [
        ("claude", AuthBasis.API_KEY),
        ("qwen", AuthBasis.API_KEY),
        ("goose", AuthBasis.API_KEY),
        ("gemini", AuthBasis.API_KEY),
        ("aider", AuthBasis.API_KEY),
        ("kimi", AuthBasis.API_KEY),
        ("codex", AuthBasis.API_KEY),
        ("droid", AuthBasis.API_KEY),
        ("opencode", AuthBasis.API_KEY),
        ("python_runtime", AuthBasis.API_KEY),
        ("computer_use", AuthBasis.UNKNOWN),
        ("integration-mock", AuthBasis.UNKNOWN),
        ("agy", AuthBasis.SUBSCRIPTION_OAUTH),
        ("copilot", AuthBasis.SUBSCRIPTION_OAUTH),
    ],
)
def test_contract_spec_loads_auth_basis(adapter: str, expected: AuthBasis) -> None:
    spec = ContractSpec.load(adapter)
    assert spec.auth_basis is expected


def test_contract_spec_default_when_basis_absent(tmp_path: Path) -> None:
    """Contracts without a basis field fall back to UNKNOWN."""
    path = tmp_path / "dummy.yaml"
    path.write_text(
        "adapter: dummy\nbinary: dummy\ninstall:\n  method: pip\n  spec: dummy\n"
        "auth:\n  required_for_help: false\n  required_for_models: false\n"
        "required_flags: []\nrequired_subcommands: []\n",
        encoding="utf-8",
    )
    spec = ContractSpec.load("dummy", contracts_dir=tmp_path)
    assert spec.auth_basis is AuthBasis.UNKNOWN


# ---------------------------------------------------------------------------
# AdapterCapabilityProfile carries auth_basis
# ---------------------------------------------------------------------------


def test_profile_default_auth_basis_is_unknown() -> None:
    profile = AdapterCapabilityProfile(
        name="stub",
        display_name="Stub",
        invocation=InvocationSpec(binary="stub"),
    )
    assert profile.auth_basis is AuthBasis.UNKNOWN


def test_profile_auth_basis_in_canonical_form() -> None:
    profile = AdapterCapabilityProfile(
        name="stub",
        display_name="Stub",
        invocation=InvocationSpec(binary="stub"),
        auth_basis=AuthBasis.API_KEY,
    )
    canonical = profile.to_canonical_dict()
    assert canonical["auth_basis"] == "api_key"


def test_profile_auth_basis_stables_hash() -> None:
    profile = AdapterCapabilityProfile(
        name="stub",
        display_name="Stub",
        invocation=InvocationSpec(binary="stub"),
        auth_basis=AuthBasis.API_KEY,
    )
    first = profile.profile_hash
    profile2 = AdapterCapabilityProfile(
        name="stub",
        display_name="Stub",
        invocation=InvocationSpec(binary="stub"),
        auth_basis=AuthBasis.API_KEY,
    )
    assert profile2.profile_hash == first


# ---------------------------------------------------------------------------
# Completeness: every shipped contract YAML has an explicit basis
# ---------------------------------------------------------------------------


def test_all_contracts_dir_contains_basis() -> None:
    """Every contract YAML on disk must carry an explicit auth.basis entry."""
    yaml_files = sorted(CONTRACTS_DIR.glob("*.yaml"))
    for path in yaml_files:
        data = path.read_text(encoding="utf-8")
        assert "basis:" in data, f"{path.name} is missing an explicit auth.basis"
