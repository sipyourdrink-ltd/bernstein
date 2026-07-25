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
    """The default: observable first, blocking only when opted into.

    The exact refusal reason depends on whether the host happens to have the
    upstream binary installed (``no_receipt`` when it does, ``conformance_skip``
    when it does not), so the assertion is on the verdict and the record - the
    two things the warn policy is defined by - rather than on the reason.
    """
    monkeypatch.delenv(_POLICY_ENV, raising=False)
    spawner = _spawner(tmp_path, mock_adapter_factory())

    spawner._preflight_adapter_admission("opencode")

    events = _admission_events(tmp_path)
    assert len(events) == 1
    assert events[0].details["verdict"] == VERDICT_REFUSE  # type: ignore[attr-defined]
    assert events[0].details["reason"]  # type: ignore[attr-defined]


def test_off_policy_skips_the_gate_entirely(
    tmp_path: Path,
    mock_adapter_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_POLICY_ENV, "off")
    spawner = _spawner(tmp_path, mock_adapter_factory())

    spawner._preflight_adapter_admission("opencode")

    assert _admission_events(tmp_path) == []


def test_unregistered_adapter_name_is_refused_not_crashed(
    tmp_path: Path,
    mock_adapter_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawner can hold an adapter that was injected, not registry-resolved.

    Test doubles and third-party adapters are both constructed directly and
    handed to the spawner, so their name need not resolve in ``_ADAPTERS``.
    The gate must record a refusal for such a name rather than raising the
    registry's "unknown adapter" ``ValueError`` out of an unrelated preflight.
    """
    monkeypatch.delenv(_POLICY_ENV, raising=False)
    spawner = _spawner(tmp_path, mock_adapter_factory())

    spawner._preflight_adapter_admission("mockcli")

    events = _admission_events(tmp_path)
    assert len(events) == 1
    assert events[0].details["adapter"] == "mockcli"  # type: ignore[attr-defined]
    assert events[0].details["verdict"] == VERDICT_REFUSE  # type: ignore[attr-defined]


def test_non_string_adapter_key_is_skipped_not_serialised(
    tmp_path: Path,
    mock_adapter_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stubbed adapter key must not reach the chain as an unserialisable value.

    Spawner tests construct the spawner around a ``MagicMock``, so the name
    that flows into the preflight is not always a string. Every receipt field
    is JSON-serialised into the audit chain, so passing one through would
    raise ``TypeError`` from the anchor rather than producing a decision.
    """
    from unittest.mock import MagicMock

    monkeypatch.delenv(_POLICY_ENV, raising=False)
    spawner = _spawner(tmp_path, mock_adapter_factory())

    spawner._preflight_adapter_admission(MagicMock())  # type: ignore[arg-type]

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
