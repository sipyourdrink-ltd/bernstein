"""Dispatch-path tests for receipt-gated adapter admission (issue #2610).

The receipt primitives - fingerprint determinism, the symmetric admit/refuse
receipt shape, staleness and drift detection - are proven in isolation in
``tests/unit/test_adapter_admission.py``. These tests prove the other half:
that the *spawn dispatch path* invokes the gate, so an adapter that cannot
present a fresh admission receipt leaves a chain-anchored refusal instead of
spawning on the strength of being importable.

The hook is exercised on a real :class:`AgentSpawner` so the assertions cover
the chain the production dispatch path writes. The audit key is isolated to
``tmp_path`` by the autouse ``_isolate_audit_key`` conftest fixture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from bernstein.core.spawner import AgentSpawner

from bernstein.adapters.admission import (
    GATE_RECEIPT_KIND,
    REASON_NO_RECEIPT,
    VERDICT_REFUSE,
    AdapterAdmissionRefusal,
)
from bernstein.core.security.audit_chain import (
    EVENT_ADAPTER_ADMISSION_RECEIPT,
    AuditChainStore,
)

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock

_POLICY_ENV = "BERNSTEIN_ADAPTER_ADMISSION_POLICY"


def _spawner(tmp_path: Path, adapter: MagicMock) -> AgentSpawner:
    templates_dir = tmp_path / "templates" / "roles"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return AgentSpawner(adapter, templates_dir, tmp_path, use_worktrees=False, default_model="mock-model")


def _admission_events(tmp_path: Path) -> list[object]:
    chain = AuditChainStore(tmp_path / ".sdd" / "audit")
    return list(chain.query(event_type=EVENT_ADAPTER_ADMISSION_RECEIPT))


def test_dispatch_refuses_an_adapter_with_no_sealed_receipt(
    tmp_path: Path,
    mock_adapter_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under enforce, an unproven adapter never receives task context."""
    monkeypatch.setenv(_POLICY_ENV, "enforce")
    spawner = _spawner(tmp_path, mock_adapter_factory())

    with pytest.raises(AdapterAdmissionRefusal) as excinfo:
        spawner._preflight_adapter_admission("opencode")

    assert excinfo.value.receipt["verdict"] == VERDICT_REFUSE
    assert "spawn" in excinfo.value.receipt["forbidden_capabilities"]
    assert excinfo.value.receipt["remediation"]


def test_dispatch_anchors_the_refusal_in_the_audit_chain(
    tmp_path: Path,
    mock_adapter_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative path is a record, not a silence."""
    monkeypatch.setenv(_POLICY_ENV, "enforce")
    spawner = _spawner(tmp_path, mock_adapter_factory())

    with pytest.raises(AdapterAdmissionRefusal):
        spawner._preflight_adapter_admission("opencode")

    events = _admission_events(tmp_path)
    assert len(events) == 1
    details = events[0].details  # type: ignore[attr-defined]
    assert details["adapter"] == "opencode"
    assert details["verdict"] == VERDICT_REFUSE
    assert details["kind"] == GATE_RECEIPT_KIND
    assert details["replay_fingerprint"].startswith("sha256:")


def test_dispatch_writes_a_forensic_decision_receipt(
    tmp_path: Path,
    mock_adapter_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_POLICY_ENV, "enforce")
    spawner = _spawner(tmp_path, mock_adapter_factory())

    with pytest.raises(AdapterAdmissionRefusal):
        spawner._preflight_adapter_admission("opencode")

    written = sorted((tmp_path / ".sdd" / "adapters" / "admission" / "decisions").glob("opencode-*.json"))
    assert len(written) == 1


def test_warn_policy_records_but_does_not_block(
    tmp_path: Path,
    mock_adapter_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default: observable first, blocking only when opted into."""
    monkeypatch.delenv(_POLICY_ENV, raising=False)
    spawner = _spawner(tmp_path, mock_adapter_factory())

    spawner._preflight_adapter_admission("opencode")

    events = _admission_events(tmp_path)
    assert len(events) == 1
    assert events[0].details["reason"] == REASON_NO_RECEIPT  # type: ignore[attr-defined]


def test_off_policy_skips_the_gate_entirely(
    tmp_path: Path,
    mock_adapter_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_POLICY_ENV, "off")
    spawner = _spawner(tmp_path, mock_adapter_factory())

    spawner._preflight_adapter_admission("opencode")

    assert _admission_events(tmp_path) == []


def test_mock_adapter_is_never_gated(
    tmp_path: Path,
    mock_adapter_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The offline escape hatch holds even under the enforce policy."""
    monkeypatch.setenv(_POLICY_ENV, "enforce")
    spawner = _spawner(tmp_path, mock_adapter_factory())

    spawner._preflight_adapter_admission("mock")

    assert _admission_events(tmp_path) == []
