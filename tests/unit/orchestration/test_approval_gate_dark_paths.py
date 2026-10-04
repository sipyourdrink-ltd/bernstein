"""Coverage for the rarely-hit branches of the pre-spawn approval gate.

The integration suite (``tests/integration/test_approval_gate.py``) covers the
happy paths and timeouts. This module targets the dark branches:

* :func:`_resolve_default_action` for the ``"fail"`` action.
* :func:`_emit_audit` when no audit log is resolvable, and when ``log.log``
  raises.
* :func:`_publish_actor_event` - the actor bridge import/publish error paths
  and the no-session-id short circuit.
* :func:`wait_for_approval` with a ``session_id`` so the actor-bridge mirror
  runs end-to-end (requested + granted/denied events).
* :func:`list_pending_approvals` skipping unreadable / non-dict sentinels.
* :func:`write_pending_sentinel` atomic-write cleanup on serialization failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from bernstein.core.models import ApprovalSpec

from bernstein.core.orchestration import approval_gate as ag
from bernstein.core.orchestration.approval_gate import (
    _emit_audit,
    _publish_actor_event,
    _resolve_default_action,
    list_pending_approvals,
    wait_for_approval,
    write_pending_sentinel,
)

# ---------------------------------------------------------------------------
# _resolve_default_action
# ---------------------------------------------------------------------------


def test_resolve_default_action_approve() -> None:
    assert _resolve_default_action("approve") == "approved"


def test_resolve_default_action_reject() -> None:
    assert _resolve_default_action("reject") == "rejected"


def test_resolve_default_action_fail_collapses_to_rejected() -> None:
    # "fail" is distinct in the audit chain but collapses to "rejected" for
    # the gate's runtime contract.
    assert _resolve_default_action("fail") == "rejected"


# ---------------------------------------------------------------------------
# _emit_audit
# ---------------------------------------------------------------------------


def test_emit_audit_none_log_and_no_lifecycle_log_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    # When no explicit log is passed and the lifecycle lookup yields None,
    # the function must return without raising.
    monkeypatch.setattr(
        "bernstein.core.tasks.lifecycle.get_audit_log",
        lambda: None,
    )
    # Should not raise.
    _emit_audit(None, event_type="approval_pending", task_id="T-1", details={})


def test_emit_audit_lifecycle_import_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Any:
        raise RuntimeError("lifecycle unavailable")

    monkeypatch.setattr("bernstein.core.tasks.lifecycle.get_audit_log", _boom)
    # Exception inside the lookup is swallowed (debug-logged) and we return.
    _emit_audit(None, event_type="approval_pending", task_id="T-1", details={})


def test_emit_audit_log_write_failure_is_swallowed() -> None:
    class _BrokenLog:
        def log(self, **_kwargs: Any) -> None:
            raise OSError("disk full")

    # A failing audit write must not propagate - audit is best-effort.
    _emit_audit(_BrokenLog(), event_type="approval_resolved", task_id="T-1", details={"outcome": "approved"})


def test_emit_audit_forwards_fields_to_log() -> None:
    captured: dict[str, Any] = {}

    class _RecordingLog:
        def log(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    _emit_audit(
        _RecordingLog(),
        event_type="approval_resolved",
        task_id="T-42",
        details={"outcome": "rejected", "decision_source": "cli"},
    )
    assert captured["event_type"] == "approval_resolved"
    assert captured["resource_id"] == "T-42"
    assert captured["actor"] == "approval_gate"
    assert captured["details"]["outcome"] == "rejected"


# ---------------------------------------------------------------------------
# _publish_actor_event
# ---------------------------------------------------------------------------


def test_publish_actor_event_no_session_id_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        "bernstein.core.orchestration.run_actor_registry.publish_event_sync",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    _publish_actor_event(session_id=None, kind="approval_requested", task_id="T-1")
    # No session id => never reaches the registry.
    assert calls == []


def test_publish_actor_event_publishes_to_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[tuple[str, Any]] = []

    def _fake_publish(session_id: str, event: Any) -> bool:
        published.append((session_id, event))
        return True

    monkeypatch.setattr(
        "bernstein.core.orchestration.run_actor_registry.publish_event_sync",
        _fake_publish,
    )
    _publish_actor_event(
        session_id="sess-1",
        kind="approval_granted",
        task_id="T-7",
        extras={"decision_source": "cli"},
    )
    assert len(published) == 1
    session_id, event = published[0]
    assert session_id == "sess-1"
    assert event.kind == "approval_granted"
    assert event.payload["task_id"] == "T-7"
    assert event.payload["decision_source"] == "cli"
    assert event.source == "approval_gate"


def test_publish_actor_event_swallows_publish_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_session_id: str, _event: Any) -> bool:
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(
        "bernstein.core.orchestration.run_actor_registry.publish_event_sync",
        _boom,
    )
    # A failing publish must not propagate out of the best-effort bridge.
    _publish_actor_event(session_id="sess-1", kind="approval_denied", task_id="T-1")


# ---------------------------------------------------------------------------
# wait_for_approval with session_id (actor bridge end-to-end)
# ---------------------------------------------------------------------------


def _operator_decides(workdir: Path, task_id: str, outcome: Any) -> None:
    """Land a signed CLI decision record for the open request (once)."""
    from bernstein.core.approval.models import local_shell_principal
    from bernstein.core.orchestration.approval_gate import record_decision

    if not (workdir / ".sdd" / "runtime" / "approvals" / f"{task_id}.{outcome}").exists():
        record_decision(workdir, task_id, outcome, source="cli", principal=local_shell_principal().to_dict())


def test_wait_for_approval_mirrors_actor_events_on_approve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []
    monkeypatch.setattr(
        "bernstein.core.orchestration.run_actor_registry.publish_event_sync",
        lambda _sid, event: events.append(event) or True,
    )
    spec = ApprovalSpec(prompt="ship?", timeout_seconds=5)

    outcome = wait_for_approval(
        "T-actor",
        spec,
        workdir=tmp_path,
        audit_log=None,
        session_id="sess-actor",
        sleep=lambda _s: _operator_decides(tmp_path, "T-actor", "approved"),
    )
    assert outcome == "approved"
    kinds = [e.kind for e in events]
    # Requested first (sentinel emitted), then granted on resolution.
    assert kinds == ["approval_requested", "approval_granted"]


def test_wait_for_approval_mirrors_actor_events_on_reject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[Any] = []
    monkeypatch.setattr(
        "bernstein.core.orchestration.run_actor_registry.publish_event_sync",
        lambda _sid, event: events.append(event) or True,
    )
    spec = ApprovalSpec(prompt="ship?", timeout_seconds=5)

    outcome = wait_for_approval(
        "T-actor-rej",
        spec,
        workdir=tmp_path,
        audit_log=None,
        session_id="sess-actor",
        sleep=lambda _s: _operator_decides(tmp_path, "T-actor-rej", "rejected"),
    )
    assert outcome == "rejected"
    kinds = [e.kind for e in events]
    assert kinds == ["approval_requested", "approval_denied"]


def test_wait_for_approval_timeout_mirrors_actor_denied_for_reject_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[Any] = []
    monkeypatch.setattr(
        "bernstein.core.orchestration.run_actor_registry.publish_event_sync",
        lambda _sid, event: events.append(event) or True,
    )
    spec = ApprovalSpec(prompt="?", timeout_seconds=1, default_action="reject")
    clock = iter([0.0, 100.0, 100.0, 100.0])
    outcome = wait_for_approval(
        "T-actor-timeout",
        spec,
        workdir=tmp_path,
        audit_log=None,
        session_id="sess-actor",
        monotonic=lambda: next(clock),
        sleep=lambda _s: None,
    )
    assert outcome == "timeout"
    kinds = [e.kind for e in events]
    # Requested then denied (timeout-default reject maps to a denied mirror).
    assert kinds == ["approval_requested", "approval_denied"]


# ---------------------------------------------------------------------------
# list_pending_approvals - skip unreadable / non-dict sentinels
# ---------------------------------------------------------------------------


def test_list_pending_approvals_empty_dir_returns_empty(tmp_path: Path) -> None:
    # No approvals dir at all.
    assert list_pending_approvals(tmp_path) == []


def test_list_pending_approvals_skips_non_dict_payload(tmp_path: Path) -> None:
    approvals_dir = tmp_path / ".sdd" / "runtime" / "approvals"
    approvals_dir.mkdir(parents=True)
    # A JSON array is valid JSON but not a dict; it must be skipped.
    (approvals_dir / "T-bad.pending").write_text("[1, 2, 3]")
    write_pending_sentinel(tmp_path, "T-good", ApprovalSpec(prompt="ok"))

    rows = list_pending_approvals(tmp_path)
    ids = {r.get("task_id") for r in rows}
    assert ids == {"T-good"}  # only the well-formed sentinel survives


def test_list_pending_approvals_skips_unreadable_json(tmp_path: Path) -> None:
    approvals_dir = tmp_path / ".sdd" / "runtime" / "approvals"
    approvals_dir.mkdir(parents=True)
    (approvals_dir / "T-corrupt.pending").write_text("{not valid json")
    write_pending_sentinel(tmp_path, "T-fine", ApprovalSpec(prompt="ok"))

    rows = list_pending_approvals(tmp_path)
    ids = {r.get("task_id") for r in rows}
    assert ids == {"T-fine"}


def test_list_pending_approvals_defaults_task_id_from_filename(tmp_path: Path) -> None:
    approvals_dir = tmp_path / ".sdd" / "runtime" / "approvals"
    approvals_dir.mkdir(parents=True)
    # Sentinel dict without an explicit task_id key.
    (approvals_dir / "T-named.pending").write_text('{"prompt": "x"}')
    rows = list_pending_approvals(tmp_path)
    assert rows[0]["task_id"] == "T-named"


# ---------------------------------------------------------------------------
# write_pending_sentinel atomic-write failure cleanup
# ---------------------------------------------------------------------------


def test_write_pending_sentinel_cleans_temp_on_serialization_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = ApprovalSpec(prompt="ship?", timeout_seconds=5)

    # Force json.dump to blow up so the atomic-write except branch (temp-file
    # unlink + re-raise) executes.
    def _boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("serialize failed")

    monkeypatch.setattr(ag.json, "dump", _boom)

    with pytest.raises(RuntimeError, match="serialize failed"):
        write_pending_sentinel(tmp_path, "T-fail", spec)

    # No stray temp file left behind in the approvals directory.
    approvals_dir = tmp_path / ".sdd" / "runtime" / "approvals"
    leftover = list(approvals_dir.glob(".T-fail.pending.*.tmp"))
    assert leftover == []
