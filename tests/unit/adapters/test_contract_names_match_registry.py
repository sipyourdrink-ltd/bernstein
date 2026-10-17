"""Every adapter contract is filed under the name its adapter is registered as.

Contracts are looked up by adapter name: ``ContractSpec.load(name)`` opens
``tests/contract/contracts/<name>.yaml``. The volunteer runner resolves an
adapter's ``auth_basis`` that way, ``CLIAdapter.session_id_flag_args`` reads
its ``session_id_flag`` that way, and ``bernstein adapters contract-check``
takes the same name. Two contracts were filed under the binary or module name
instead -- ``crush.yaml`` for the ``charm`` adapter, ``continue_dev.yaml``
for ``continue`` -- so every one of those lookups answered "no contract" for
adapters that had one, and volunteer mode reported their auth basis as
unknown.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bernstein.adapters._contract import ContractSpec
from bernstein.adapters.registry import iter_adapter_specs

_CONTRACTS_DIR = Path("tests/contract/contracts")

#: Contracts for adapters that only the test suite registers (``tests/conftest.py``).
_TEST_ONLY_ADAPTERS = frozenset({"integration-mock"})

_CONTRACTS = sorted(_CONTRACTS_DIR.glob("*.yaml"))


def _registered_names() -> frozenset[str]:
    return frozenset(name for name, _spec in iter_adapter_specs())


@pytest.mark.parametrize("contract", _CONTRACTS, ids=lambda p: p.stem)
def test_contract_file_is_named_after_a_registered_adapter(contract: Path) -> None:
    assert contract.stem in _registered_names() | _TEST_ONLY_ADAPTERS, (
        f"{contract} is not the name of any registered adapter; ContractSpec.load(<adapter name>) will never find it"
    )


@pytest.mark.parametrize("contract", _CONTRACTS, ids=lambda p: p.stem)
def test_contract_declares_the_adapter_it_is_filed_under(contract: Path) -> None:
    data = yaml.safe_load(contract.read_text(encoding="utf-8")) or {}
    assert data.get("adapter") == contract.stem


@pytest.mark.parametrize(("adapter", "binary"), [("charm", "crush"), ("continue", "cn")])
def test_renamed_contracts_resolve_by_adapter_name(adapter: str, binary: str) -> None:
    spec = ContractSpec.load(adapter, contracts_dir=_CONTRACTS_DIR)
    assert spec.adapter == adapter
    assert spec.binary == binary
