"""Unit tests verifying adapter admission uses registry IDs for contract lookup (#5348).

When a model's provider prefix is not a registered adapter (e.g. `--model myproxy/coder`
with `--cli qwen`), the spawner's preflight must use the registry ID (`qwen`) rather
than the display name (`Qwen CLI`) so contract lookup (`qwen.yaml`) succeeds and does
not issue a `no_contract` refusal.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bernstein.adapters._contract import ContractSpec
from bernstein.adapters.admission import (
    AdmissionGate,
    preflight_admission,
)
from bernstein.adapters.opencode import OpenCodeAdapter
from bernstein.adapters.qwen import QwenAdapter
from bernstein.adapters.registry import (
    canonical_adapter_name,
)
from bernstein.adapters.report import _contract_hash, check_adapter_in_process
from bernstein.core.agents.spawner_core import AgentSpawner


def _spawner(tmp_path: Path, adapter: MagicMock | None = None) -> AgentSpawner:
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    active_adapter = adapter or QwenAdapter()
    return AgentSpawner(
        active_adapter,
        templates_dir,
        tmp_path,
        use_worktrees=False,
        default_model="mock-model",
    )


def test_canonical_adapter_name_resolutions() -> None:
    """Canonical adapter name resolves display names, aliases, and registry keys."""
    assert canonical_adapter_name("qwen") == "qwen"
    assert canonical_adapter_name("Qwen CLI") == "qwen"
    assert canonical_adapter_name("qwen cli") == "qwen"
    assert canonical_adapter_name("opencode") == "opencode"
    assert canonical_adapter_name("OpenCode") == "opencode"
    assert canonical_adapter_name("claude") == "claude"
    assert canonical_adapter_name("Claude Code") == "claude"
    assert canonical_adapter_name("codex") == "codex"
    assert canonical_adapter_name("Codex CLI") == "codex"
    assert canonical_adapter_name("gemini") == "gemini"
    assert canonical_adapter_name("Gemini CLI") == "gemini"
    assert canonical_adapter_name("") is None
    assert canonical_adapter_name("unknown-adapter-xyz") is None


def test_infer_adapter_name_for_provider_uses_registry_id_for_unmatched_provider(tmp_path: Path) -> None:
    """Spawner fallback with an unmatched provider prefix returns registry ID, not display name."""
    spawner_qwen = _spawner(tmp_path, QwenAdapter())
    inferred = spawner_qwen._infer_adapter_name_for_provider("myproxy", "myproxy/coder")
    assert inferred == "qwen"
    assert inferred != "Qwen CLI"

    spawner_opencode = _spawner(tmp_path, OpenCodeAdapter())
    inferred_oc = spawner_opencode._infer_adapter_name_for_provider("customgateway", "customgateway/deepseek")
    assert inferred_oc == "opencode"


def test_contract_lookup_finds_qwen_for_display_name() -> None:
    """_contract_hash and ContractSpec.load find qwen.yaml for both display name and registry ID."""
    hash_reg = _contract_hash("qwen")
    hash_disp = _contract_hash("Qwen CLI")
    assert hash_reg
    assert hash_reg == hash_disp

    spec_reg = ContractSpec.load("qwen")
    spec_disp = ContractSpec.load("Qwen CLI")
    assert spec_reg.adapter == "qwen"
    assert spec_disp.adapter == "qwen"

    # Also test check_adapter_in_process with display name
    payload_reg = check_adapter_in_process("qwen", binary_resolved=None)
    payload_disp = check_adapter_in_process("Qwen CLI", binary_resolved=None)
    assert payload_disp.detail != "no contract"
    assert payload_disp.capabilities == payload_reg.capabilities


def test_contract_lookup_finds_opencode_for_display_name() -> None:
    """_contract_hash and ContractSpec.load find opencode.yaml for OpenCode."""
    hash_reg = _contract_hash("opencode")
    hash_disp = _contract_hash("OpenCode")
    assert hash_reg
    assert hash_reg == hash_disp

    spec_reg = ContractSpec.load("opencode")
    spec_disp = ContractSpec.load("OpenCode")
    assert spec_reg.adapter == "opencode"
    assert spec_disp.adapter == "opencode"


def test_admission_gate_admit_display_name_resolves_to_contract_and_receipt_stem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AdmissionGate.admit('Qwen CLI') evaluates against qwen contract and writes qwen receipts."""
    monkeypatch.setenv("BERNSTEIN_ADAPTER_ADMISSION_POLICY", "warn")
    receipts_dir = tmp_path / "receipts"
    decisions_dir = tmp_path / "decisions"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    decisions_dir.mkdir(parents=True, exist_ok=True)

    gate = AdmissionGate(
        receipts_dir=receipts_dir,
        decisions_dir=decisions_dir,
        policy="warn",
    )
    decision = gate.admit("Qwen CLI")
    assert decision is not None
    assert decision.evidence.adapter == "qwen"
    # Verify contract was found (reason is not NO_CONTRACT)
    assert decision.reason != "no_contract"

    # Decisions file is written with qwen stem
    written = list(decisions_dir.glob("qwen-*.json"))
    assert len(written) == 1
    assert not list(decisions_dir.glob("Qwen CLI-*.json"))


def test_refusal_message_names_registry_id_contract_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a genuinely missing contract is refused, remediation path uses registry ID without space."""
    decision = preflight_admission(
        adapter="Qwen CLI",
        receipts_dir=tmp_path / "receipts",
        policy="warn",
        contracts_dir=tmp_path / "empty_contracts",
    )
    assert decision is not None
    assert decision.evidence.adapter == "qwen"
    assert decision.reason == "no_contract"
    from bernstein.adapters.admission import REMEDIATION

    remediation = REMEDIATION["no_contract"].format(adapter=decision.evidence.adapter)
    assert "tests/contract/contracts/qwen.yaml" in remediation
    assert "Qwen CLI" not in remediation
