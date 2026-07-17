"""Feed projection: determinism and verifiability beyond integrity (#2548).

Covers acceptance criteria:
  * Determinism - same chain bytes project byte-identical canonical output.
  * Verifiability beyond integrity - a window verifies offline for completeness
    and order; deleting or reordering any single event fails verification.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from bernstein.core.events.feed import project_window, verify_window
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.audit_slice import slice_audit_log


def _seed_chain(audit_dir: Path) -> AuditChainStore:
    """Write a handful of chained events with lineage edges in details."""
    chain = AuditChainStore(audit_dir, key=b"k" * 32)
    chain.log_with_prev_digest(
        event_type="run.started",
        actor="orchestrator",
        resource_type="run",
        resource_id="run_1",
        details={"run_id": "run_1"},
    )
    chain.log_with_prev_digest(
        event_type="task.created",
        actor="orchestrator",
        resource_type="task",
        resource_id="task_a",
        details={"run_id": "run_1", "lineage_parents": ["run_1"]},
    )
    chain.log_with_prev_digest(
        event_type="gate.result",
        actor="gate",
        resource_type="gate",
        resource_id="gate_1",
        details={"task_id": "task_a", "passed": False, "lineage_parents": ["task_a"]},
    )
    chain.log_with_prev_digest(
        event_type="task.completed",
        actor="worker",
        resource_type="task",
        resource_id="task_a",
        details={"run_id": "run_1", "lineage_parents": ["task_a"]},
    )
    return chain


def test_projection_is_byte_identical_across_hosts(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    _seed_chain(audit_dir)

    # Two independent "hosts" read the same on-disk bytes and project.
    window_a = project_window(slice_audit_log(audit_dir))
    window_b = project_window(slice_audit_log(audit_dir))

    assert window_a.to_envelope_json() == window_b.to_envelope_json()
    assert window_a.to_jsonl() == window_b.to_jsonl()
    assert window_a.count == 4


def test_window_embeds_fence_posts(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    _seed_chain(audit_dir)
    window = project_window(slice_audit_log(audit_dir))

    assert window.from_hmac == window.events[0].hmac
    assert window.to_hmac == window.events[-1].hmac


def test_window_verifies_offline_for_completeness_and_order(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    _seed_chain(audit_dir)
    window = project_window(slice_audit_log(audit_dir))

    ok, errors = verify_window(window)
    assert ok, errors


def test_deleting_an_event_fails_verification(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    _seed_chain(audit_dir)
    window = project_window(slice_audit_log(audit_dir))

    # Drop the middle event but keep the fence-posts: linkage must break.
    tampered = dataclasses.replace(
        window,
        events=[window.events[0], window.events[1], window.events[3]],
    )
    ok, errors = verify_window(tampered, expect_from=window.from_hmac, expect_to=window.to_hmac)
    assert not ok
    assert any("order break" in e for e in errors)


def test_reordering_an_event_fails_verification(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    _seed_chain(audit_dir)
    window = project_window(slice_audit_log(audit_dir))

    reordered = dataclasses.replace(
        window,
        events=[window.events[0], window.events[2], window.events[1], window.events[3]],
    )
    ok, _errors = verify_window(reordered, expect_from=window.from_hmac, expect_to=window.to_hmac)
    assert not ok


def test_truncating_upper_fence_post_fails(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    _seed_chain(audit_dir)
    window = project_window(slice_audit_log(audit_dir))

    truncated = dataclasses.replace(window, events=window.events[:-1])
    ok, errors = verify_window(truncated, expect_from=window.from_hmac, expect_to=window.to_hmac)
    assert not ok
    assert any("upper fence-post" in e for e in errors)


def test_bounded_slice_projects_and_verifies(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    _seed_chain(audit_dir)
    full = project_window(slice_audit_log(audit_dir))
    mid_from = full.events[1].hmac
    mid_to = full.events[2].hmac

    window = project_window(slice_audit_log(audit_dir, from_hmac=mid_from, to_hmac=mid_to))
    assert window.count == 2
    assert window.from_hmac == mid_from
    assert window.to_hmac == mid_to
    ok, errors = verify_window(window)
    assert ok, errors
