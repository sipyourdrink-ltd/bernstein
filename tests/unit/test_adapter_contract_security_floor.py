"""Contract-advisories consistency for the security floor (issue #2515).

The adapter conformance contract carries a ``security_floor`` sourced from the
advisory map (the single source of truth). Every adapter with a curated floor
must load a contract that reports that exact floor, and vice versa, so the two
surfaces can never silently diverge.
"""

from __future__ import annotations

from bernstein.adapters._contract import CONTRACTS_DIR, ContractSpec
from bernstein.adapters.advisories import ADAPTER_MIN_SAFE_VERSIONS


def test_every_advisory_adapter_has_a_contract_row_carrying_its_floor() -> None:
    for name, advisory in ADAPTER_MIN_SAFE_VERSIONS.items():
        spec = ContractSpec.load(name)
        assert spec.security_floor == advisory.min_safe_version, name
        assert spec.security_advisory_id == advisory.advisory_id, name


def test_contract_floor_matches_advisory_and_none_when_untracked() -> None:
    # A contract without an advisory reports no floor.
    spec = ContractSpec.load("claude")
    assert spec.security_floor is None
    assert spec.security_advisory_id is None


def test_every_contract_reporting_a_floor_is_backed_by_an_advisory() -> None:
    # Vice versa: no contract may fabricate a floor the advisory map does not
    # carry (the loader sources it from the map, so this holds by construction,
    # but the test pins the invariant against future refactors).
    for path in sorted(CONTRACTS_DIR.glob("*.yaml")):
        spec = ContractSpec.load(path.stem)
        if spec.security_floor is not None:
            assert spec.adapter in ADAPTER_MIN_SAFE_VERSIONS
            assert spec.security_floor == ADAPTER_MIN_SAFE_VERSIONS[spec.adapter].min_safe_version
