"""Unit tests for the orchestrator's pure helper functions and methods.

Covers, without constructing a full :class:`Orchestrator`:

* :func:`_build_container_config` - runtime/network fallbacks, disabled gate,
  two-phase sandbox wiring.
* :func:`_build_notification_manager` and the ``_collect_*_targets`` family -
  webhook / desktop / smtp target assembly from a seed config.
* :class:`TickResult` - default field shape.
* :meth:`Orchestrator._build_pr_body` and :meth:`Orchestrator._get_pr_diff_stats`
  - PR body formatting and shortstat regex parsing - exercised by binding the
  unbound methods to a stub ``self`` that only carries ``_workdir``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from bernstein.core.models import ContainerIsolationConfig, Task

from bernstein.core.orchestration.orchestrator import (
    Orchestrator,
    TickResult,
    _build_container_config,
    _build_notification_manager,
    _collect_notify_targets,
    _collect_smtp_targets,
    _collect_webhook_targets,
)

# ---------------------------------------------------------------------------
# _build_container_config
# ---------------------------------------------------------------------------


def test_build_container_config_disabled_returns_none() -> None:
    cfg = _build_container_config(ContainerIsolationConfig(enabled=False))
    assert cfg is None


def test_build_container_config_maps_known_runtime_and_network() -> None:
    from bernstein.core.container import ContainerRuntime, NetworkMode

    iso = ContainerIsolationConfig(
        enabled=True,
        runtime="podman",
        network_mode="bridge",
        image="custom:1",
        cpu_cores=1.0,
        memory_mb=512,
        pids_limit=64,
    )
    cfg = _build_container_config(iso)
    assert cfg is not None
    assert cfg.runtime is ContainerRuntime.PODMAN
    assert cfg.network_mode is NetworkMode.BRIDGE
    assert cfg.image == "custom:1"
    assert cfg.resource_limits.cpu_cores == 1.0
    assert cfg.resource_limits.memory_mb == 512


def test_build_container_config_unknown_runtime_falls_back_to_docker() -> None:
    from bernstein.core.container import ContainerRuntime

    iso = ContainerIsolationConfig(enabled=True, runtime="qemu-nonexistent")
    cfg = _build_container_config(iso)
    assert cfg is not None
    assert cfg.runtime is ContainerRuntime.DOCKER


def test_build_container_config_unknown_network_falls_back_to_host() -> None:
    from bernstein.core.container import NetworkMode

    iso = ContainerIsolationConfig(enabled=True, network_mode="weird-net")
    cfg = _build_container_config(iso)
    assert cfg is not None
    assert cfg.network_mode is NetworkMode.HOST


def test_build_container_config_two_phase_sandbox_propagates_setup_commands() -> None:
    iso = ContainerIsolationConfig(
        enabled=True,
        two_phase_sandbox=True,
        sandbox_setup_commands=("apt-get update", "pip install -e ."),
    )
    cfg = _build_container_config(iso)
    assert cfg is not None
    assert cfg.two_phase_sandbox is not None
    assert cfg.two_phase_sandbox.setup_commands == ("apt-get update", "pip install -e .")


def test_build_container_config_no_two_phase_leaves_field_none() -> None:
    iso = ContainerIsolationConfig(enabled=True, two_phase_sandbox=False)
    cfg = _build_container_config(iso)
    assert cfg is not None
    assert cfg.two_phase_sandbox is None


def test_build_container_config_drop_capabilities_become_tuple() -> None:
    iso = ContainerIsolationConfig(enabled=True, drop_capabilities=("NET_RAW", "SYS_ADMIN"))
    cfg = _build_container_config(iso)
    assert cfg is not None
    assert cfg.security.drop_capabilities == ("NET_RAW", "SYS_ADMIN")


# ---------------------------------------------------------------------------
# Notification target collection
# ---------------------------------------------------------------------------


def test_build_notification_manager_none_seed_returns_none() -> None:
    assert _build_notification_manager(None) is None


def test_build_notification_manager_no_targets_returns_none() -> None:
    # A seed with no notify/webhooks/smtp config yields zero targets => None.
    seed = SimpleNamespace(notify=None, webhooks=(), smtp=None)
    assert _build_notification_manager(seed) is None


def test_collect_notify_targets_webhook_with_default_events() -> None:
    notify = SimpleNamespace(webhook_url="https://hook.example/x", on_complete=True, on_failure=True, desktop=False)
    seed = SimpleNamespace(notify=notify)
    targets: list[Any] = []
    _collect_notify_targets(seed, targets)
    assert len(targets) == 1
    tgt = targets[0]
    assert tgt.type == "webhook"
    assert tgt.url == "https://hook.example/x"
    assert "run.completed" in tgt.events
    assert "task.failed" in tgt.events


def test_collect_notify_targets_webhook_only_failure_event() -> None:
    notify = SimpleNamespace(webhook_url="https://hook/y", on_complete=False, on_failure=True, desktop=False)
    seed = SimpleNamespace(notify=notify)
    targets: list[Any] = []
    _collect_notify_targets(seed, targets)
    assert len(targets) == 1
    assert targets[0].events == ["task.failed"]


def test_collect_notify_targets_webhook_no_events_skipped() -> None:
    # Both event flags off => no events => no webhook target appended.
    notify = SimpleNamespace(webhook_url="https://hook/z", on_complete=False, on_failure=False, desktop=False)
    seed = SimpleNamespace(notify=notify)
    targets: list[Any] = []
    _collect_notify_targets(seed, targets)
    assert targets == []


def test_collect_notify_targets_desktop_target() -> None:
    notify = SimpleNamespace(webhook_url=None, on_complete=True, on_failure=True, desktop=True)
    seed = SimpleNamespace(notify=notify)
    targets: list[Any] = []
    _collect_notify_targets(seed, targets)
    desktop_targets = [t for t in targets if t.type == "desktop"]
    assert len(desktop_targets) == 1
    assert "task.completed" in desktop_targets[0].events


def test_collect_notify_targets_none_config_is_noop() -> None:
    seed = SimpleNamespace(notify=None)
    targets: list[Any] = []
    _collect_notify_targets(seed, targets)
    assert targets == []


def test_collect_webhook_targets_appends_valid_entries() -> None:
    webhook = SimpleNamespace(url="https://w/1", events=("task.completed", "task.failed"))
    seed = SimpleNamespace(webhooks=[webhook])
    targets: list[Any] = []
    _collect_webhook_targets(seed, targets)
    assert len(targets) == 1
    assert targets[0].url == "https://w/1"
    assert targets[0].events == ["task.completed", "task.failed"]


def test_collect_webhook_targets_skips_missing_url_or_events() -> None:
    no_url = SimpleNamespace(url="", events=("task.failed",))
    no_events = SimpleNamespace(url="https://w/2", events=())
    seed = SimpleNamespace(webhooks=[no_url, no_events])
    targets: list[Any] = []
    _collect_webhook_targets(seed, targets)
    assert targets == []  # both entries invalid


def test_collect_smtp_targets_appends_email_target() -> None:
    seed = SimpleNamespace(smtp=SimpleNamespace(host="smtp.example", port=587))
    targets: list[Any] = []
    _collect_smtp_targets(seed, targets)
    assert len(targets) == 1
    assert targets[0].type == "email"
    assert "approval.needed" in targets[0].events


def test_collect_smtp_targets_no_smtp_is_noop() -> None:
    seed = SimpleNamespace(smtp=None)
    targets: list[Any] = []
    _collect_smtp_targets(seed, targets)
    assert targets == []


def test_build_notification_manager_assembles_targets_from_all_sources() -> None:
    notify = SimpleNamespace(webhook_url="https://hook/all", on_complete=True, on_failure=True, desktop=True)
    webhook = SimpleNamespace(url="https://w/extra", events=("task.failed",))
    seed = SimpleNamespace(notify=notify, webhooks=[webhook], smtp=SimpleNamespace(host="s"))
    manager = _build_notification_manager(seed)
    assert manager is not None
    types = {t.type for t in manager._targets}
    # webhook (notify) + desktop + webhook (list) + email (smtp)
    assert {"webhook", "desktop", "email"} <= types


# ---------------------------------------------------------------------------
# TickResult
# ---------------------------------------------------------------------------


def test_tick_result_defaults_are_empty() -> None:
    result = TickResult()
    assert result.open_tasks == 0
    assert result.active_agents == 0
    assert result.spawned == []
    assert result.reaped == []
    assert result.verified == []
    assert result.verification_failures == []
    assert result.retried == []
    assert result.errors == []
    assert result.dry_run_planned == []


def test_tick_result_lists_are_independent_per_instance() -> None:
    a = TickResult()
    b = TickResult()
    a.spawned.append("s1")
    # Mutating one instance must not bleed into the other (no shared mutable
    # default).
    assert b.spawned == []


# ---------------------------------------------------------------------------
# _build_pr_body / _get_pr_diff_stats (bound to a stub self)
# ---------------------------------------------------------------------------


def _task(title: str, description: str = "") -> Task:
    return Task(id=title, title=title, description=description, role="backend")


def test_get_pr_diff_stats_parses_shortstat() -> None:
    fake_self = SimpleNamespace(_workdir=Path("/tmp/repo"))
    completed = subprocess.CompletedProcess(
        args=["git"],
        returncode=0,
        stdout=" 3 files changed, 42 insertions(+), 7 deletions(-)\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=completed):
        stats = Orchestrator._get_pr_diff_stats(fake_self, "feature")
    assert stats == {"files": 3, "insertions": 42, "deletions": 7}


def test_get_pr_diff_stats_handles_insertions_only() -> None:
    fake_self = SimpleNamespace(_workdir=Path("/tmp/repo"))
    completed = subprocess.CompletedProcess(
        args=["git"],
        returncode=0,
        stdout=" 1 file changed, 5 insertions(+)\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=completed):
        stats = Orchestrator._get_pr_diff_stats(fake_self, "feature")
    assert stats == {"files": 1, "insertions": 5, "deletions": 0}


def test_get_pr_diff_stats_returns_zero_on_nonzero_returncode() -> None:
    fake_self = SimpleNamespace(_workdir=Path("/tmp/repo"))
    completed = subprocess.CompletedProcess(args=["git"], returncode=128, stdout="", stderr="fatal")
    with patch("subprocess.run", return_value=completed):
        stats = Orchestrator._get_pr_diff_stats(fake_self, "feature")
    assert stats == {"files": 0, "insertions": 0, "deletions": 0}


def test_get_pr_diff_stats_swallows_subprocess_exception() -> None:
    fake_self = SimpleNamespace(_workdir=Path("/tmp/repo"))
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10)):
        stats = Orchestrator._get_pr_diff_stats(fake_self, "feature")
    # contextlib.suppress(Exception) keeps the default zero dict.
    assert stats == {"files": 0, "insertions": 0, "deletions": 0}


def _self_with_diff_stats(stats: dict[str, int]) -> SimpleNamespace:
    """Stub ``self`` carrying the workdir and a canned ``_get_pr_diff_stats``.

    ``_build_pr_body`` resolves ``self._get_pr_diff_stats`` and
    ``self._evidence_projection_for_tasks`` on the instance, so both stubs are
    attached directly to the namespace rather than the class. Evidence linking
    has its own suite (``test_orchestrator_pr_body_evidence``); here it is a
    no-op so these tests stay focused on body formatting.
    """
    return SimpleNamespace(
        _workdir=Path("/tmp/repo"),
        _get_pr_diff_stats=lambda _branch: stats,
        _evidence_projection_for_tasks=lambda _tasks: "",
    )


def test_build_pr_body_single_task_uses_description() -> None:
    fake_self = _self_with_diff_stats({"files": 0, "insertions": 0, "deletions": 0})
    body = Orchestrator._build_pr_body(fake_self, [_task("Add login", "Implements OAuth login")], "feature")
    assert "## Summary" in body
    assert "Implements OAuth login" in body
    # No diff stats section when files == 0.
    assert "## Changes" not in body
    assert body.strip().endswith("*Generated by Bernstein*")


def test_build_pr_body_single_task_falls_back_to_title_when_no_description() -> None:
    fake_self = _self_with_diff_stats({"files": 0, "insertions": 0, "deletions": 0})
    body = Orchestrator._build_pr_body(fake_self, [_task("Just a title", "")], "feature")
    assert "Just a title" in body


def test_build_pr_body_multiple_tasks_lists_titles() -> None:
    fake_self = _self_with_diff_stats({"files": 0, "insertions": 0, "deletions": 0})
    tasks = [_task("First task"), _task("Second task"), _task("Third task")]
    body = Orchestrator._build_pr_body(fake_self, tasks, "feature")
    assert "Completed 3 tasks:" in body
    assert "- First task" in body
    assert "- Second task" in body
    assert "- Third task" in body


def test_build_pr_body_includes_changes_section_when_files_changed() -> None:
    fake_self = _self_with_diff_stats({"files": 4, "insertions": 100, "deletions": 20})
    body = Orchestrator._build_pr_body(fake_self, [_task("Big change")], "feature")
    assert "## Changes" in body
    assert "**4** files changed" in body
    assert "**+100** insertions" in body
    assert "**-20** deletions" in body
