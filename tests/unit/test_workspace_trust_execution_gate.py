"""Workspace trust gate on manifest-driven command execution.

Workflow command nodes, workflow predicates (``when`` / ``loop.until``)
and lifecycle script hooks all hand repository-authored text to a
subprocess.  None of them may run until the workspace is trusted - the
same rule the plugin manager applies to committed hook scripts.
"""

from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.lifecycle.hooks import (
    HookFailure,
    HookRegistry,
    LifecycleContext,
    LifecycleEvent,
    UntrustedWorkspaceError,
)
from bernstein.core.persistence.workspace import grant_workspace_trust
from bernstein.core.workflows import (
    NodeStatus,
    WorkflowRunError,
    WorkflowRunner,
    load_workflow_spec_from_text,
)

AuditLog = list[tuple[str, str, dict[str, Any]]]


def _audit_sink() -> tuple[AuditLog, Callable[[str, str, dict[str, Any]], None]]:
    log: AuditLog = []

    def _emit(event_type: str, resource_id: str, details: dict[str, Any]) -> None:
        log.append((event_type, resource_id, dict(details)))

    return log, _emit


def _write_script(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


_COMMAND_SPEC = """
name: touch-marker
description: "writes a marker"
version: "1.0.0"
nodes:
  - id: write
    command: "touch marker.txt"
"""

_WHEN_SPEC = """
name: gated
description: "when predicate writes a marker"
version: "1.0.0"
nodes:
  - id: gated
    command: "true"
    when: "touch when-marker.txt"
"""


# ---------------------------------------------------------------------------
# WorkflowRunner
# ---------------------------------------------------------------------------


def test_command_node_refused_in_untrusted_workspace(tmp_path: Path) -> None:
    log, emit = _audit_sink()
    runner = WorkflowRunner(workdir=tmp_path, audit_emitter=emit)

    execution = runner.run(load_workflow_spec_from_text(_COMMAND_SPEC))

    assert execution.succeeded is False
    node = execution.nodes[0]
    assert node.status == NodeStatus.FAILED
    assert "not trusted" in node.error
    assert not (tmp_path / "marker.txt").exists()
    assert any(event == "workflow.untrusted_workspace" and rid == "write" for event, rid, _ in log)


def test_command_node_runs_in_trusted_workspace(tmp_path: Path) -> None:
    grant_workspace_trust(tmp_path)
    runner = WorkflowRunner(workdir=tmp_path)

    execution = runner.run(load_workflow_spec_from_text(_COMMAND_SPEC))

    assert execution.succeeded is True
    assert (tmp_path / "marker.txt").exists()


def test_when_predicate_refused_in_untrusted_workspace(tmp_path: Path) -> None:
    runner = WorkflowRunner(workdir=tmp_path)

    with pytest.raises(WorkflowRunError, match="not trusted"):
        runner.run(load_workflow_spec_from_text(_WHEN_SPEC))

    assert not (tmp_path / "when-marker.txt").exists()


def test_trust_file_with_false_flag_is_untrusted(tmp_path: Path) -> None:
    trust = tmp_path / ".sdd" / "runtime" / "workspace_trust.json"
    trust.parent.mkdir(parents=True)
    trust.write_text('{"trusted": false}', encoding="utf-8")
    runner = WorkflowRunner(workdir=tmp_path)

    execution = runner.run(load_workflow_spec_from_text(_COMMAND_SPEC))

    assert execution.nodes[0].status == NodeStatus.FAILED
    assert not (tmp_path / "marker.txt").exists()


# ---------------------------------------------------------------------------
# HookRegistry script hooks
# ---------------------------------------------------------------------------


def test_script_hook_refused_in_untrusted_workspace(tmp_path: Path) -> None:
    marker = tmp_path / "hook-ran.txt"
    script = _write_script(tmp_path / "hook.sh", f"touch {marker}\n")
    registry = HookRegistry()
    registry.register_script(LifecycleEvent.PRE_TASK, script)
    ctx = LifecycleContext(event=LifecycleEvent.PRE_TASK, workdir=tmp_path)

    with pytest.raises(UntrustedWorkspaceError, match="not trusted") as excinfo:
        registry.run(LifecycleEvent.PRE_TASK, ctx)

    assert isinstance(excinfo.value, HookFailure)
    assert not marker.exists()


def test_script_hook_runs_in_trusted_workspace(tmp_path: Path) -> None:
    grant_workspace_trust(tmp_path)
    marker = tmp_path / "hook-ran.txt"
    script = _write_script(tmp_path / "hook.sh", f"touch {marker}\n")
    registry = HookRegistry()
    registry.register_script(LifecycleEvent.PRE_TASK, script)

    registry.run(LifecycleEvent.PRE_TASK, LifecycleContext(event=LifecycleEvent.PRE_TASK, workdir=tmp_path))

    assert marker.exists()


def test_callable_hooks_are_not_gated(tmp_path: Path) -> None:
    """In-process callables are registered by bernstein itself, not the repo."""
    seen: list[str] = []
    registry = HookRegistry()
    registry.register_callable(LifecycleEvent.PRE_TASK, lambda ctx: seen.append(ctx.event.value))

    registry.run(LifecycleEvent.PRE_TASK, LifecycleContext(event=LifecycleEvent.PRE_TASK, workdir=tmp_path))

    assert seen == ["pre_task"]
