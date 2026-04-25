"""Unit tests for hook + audit emission during archival."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from bernstein.core.lifecycle.hooks import HookRegistry, LifecycleContext, LifecycleEvent
from bernstein.core.planning.lifecycle import PlanLifecycle, PlanState
from bernstein.core.planning.run_summary import FailureSummary, RunSummary


def _write_active(lifecycle: PlanLifecycle, name: str = "demo") -> Path:
    p = lifecycle.bucket(PlanState.ACTIVE) / f"{name}.yaml"
    p.write_text(yaml.dump({"name": name, "stages": []}))
    return p


class _RecordingAuditLog:
    """Minimal stand-in for :class:`bernstein.core.security.audit.AuditLog`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def log(
        self,
        event_type: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append(
            (
                event_type,
                {
                    "actor": actor,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "details": details or {},
                },
            )
        )


def test_pre_and_post_archive_hooks_fire_in_order(tmp_path: Path) -> None:
    registry = HookRegistry()
    seen: list[str] = []

    def pre(_ctx: LifecycleContext) -> None:
        seen.append("pre")

    def post(_ctx: LifecycleContext) -> None:
        seen.append("post")

    registry.register_callable(LifecycleEvent.PRE_ARCHIVE, pre)
    registry.register_callable(LifecycleEvent.POST_ARCHIVE, post)

    lifecycle = PlanLifecycle(tmp_path / "plans", hook_registry=registry)
    plan = _write_active(lifecycle)
    lifecycle.archive_completed(plan, RunSummary())

    assert seen == ["pre", "post"]


def test_audit_log_records_archive_transition(tmp_path: Path) -> None:
    audit = _RecordingAuditLog()
    lifecycle = PlanLifecycle(tmp_path / "plans", audit_log=audit)  # type: ignore[arg-type]
    plan = _write_active(lifecycle, "auditable")
    archived = lifecycle.archive_completed(plan, RunSummary())

    assert any(call[0] == "plan.archive.success" for call in audit.calls)
    success = next(call for call in audit.calls if call[0] == "plan.archive.success")
    assert success[1]["resource_type"] == "plan"
    assert success[1]["resource_id"] == archived.stem
    assert success[1]["details"]["destination"] == str(archived)


def test_failure_audit_event_records_blocked(tmp_path: Path) -> None:
    audit = _RecordingAuditLog()
    lifecycle = PlanLifecycle(tmp_path / "plans", audit_log=audit)  # type: ignore[arg-type]
    plan = _write_active(lifecycle, "blocked")
    lifecycle.archive_blocked(plan, FailureSummary(failing_stage="lint"))
    assert any(call[0] == "plan.archive.failure" for call in audit.calls)
