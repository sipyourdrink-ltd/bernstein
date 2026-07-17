"""Sandbox relaxation must not drop the write-scope boundary on backends
that expose the whole repository.

The T466 sandbox relaxation was built on the assumption that container
isolation makes the ``scope_enforcement`` write-scope check redundant. That
assumption only holds when the sandbox mounts *only* the task's owned files.
The Docker backend bind-mounts the entire repository read-only (so ``git
checkout`` retains full history), so the container still sees every file and
the write-scope boundary must stay enforced. Only a backend that proves a
mount scoped to ``task.owned_files`` (declaring
``SandboxCapability.SCOPED_MOUNT``) may relax ``scope_enforcement``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from bernstein.core.guardrails import (
    GuardrailsConfig,
    active_backend_provides_scoped_mount,
    relax_sandboxed,
    run_guardrails,
)
from bernstein.core.models import Complexity, Scope, Task
from bernstein.core.policy_engine import DecisionType, PermissionDecision

from bernstein.core.sandbox.backend import SandboxCapability

if TYPE_CHECKING:
    from pathlib import Path


_SANDBOX_ENV = ("BERNSTEIN_SANDBOX", "BERNSTEIN_SANDBOX_RUNTIME")


@pytest.fixture(autouse=True)
def _clean_sandbox_env() -> object:
    saved = {name: os.environ.get(name) for name in _SANDBOX_ENV}
    for name in _SANDBOX_ENV:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _make_task() -> Task:
    return Task(
        id="T-scope",
        title="Scoped task",
        description="Only allowed to touch src/.",
        role="backend",
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        owned_files=["src/bernstein/core/scoped_module.py"],
    )


# A write that lands outside ``owned_files`` - the exact case the write-scope
# boundary exists to catch.
_OUT_OF_SCOPE_DIFF = "diff --git a/other/secret_exfil.py b/other/secret_exfil.py\n+leak\n"


class _ScopedMountBackend:
    """A fake backend that genuinely scopes its mount to the task files."""

    name = "scopedfs"
    capabilities = frozenset({SandboxCapability.FILE_RW, SandboxCapability.SCOPED_MOUNT})


def _scope_config() -> GuardrailsConfig:
    # Isolate the scope check from the other guardrails.
    return GuardrailsConfig(secrets=False, file_permissions=False, license_scan=False)


def test_docker_whole_repo_mount_keeps_scope_enforcement(tmp_path: Path) -> None:
    """Under the Docker whole-repo mount, an out-of-scope write must NOT be
    relaxed to ALLOW - it stays flagged (ASK) for review."""
    os.environ["BERNSTEIN_SANDBOX"] = "1"
    os.environ["BERNSTEIN_SANDBOX_RUNTIME"] = "docker"

    results = run_guardrails(_OUT_OF_SCOPE_DIFF, _make_task(), _scope_config(), tmp_path)
    scope_res = next(r for r in results if r.check == "scope_enforcement")

    assert scope_res.passed is False, "scope enforcement must survive the Docker whole-repo mount"
    assert "[SANDBOX RELAXED]" not in scope_res.detail


def test_scoped_mount_backend_still_relaxes_scope(tmp_path: Path) -> None:
    """A backend that proves a task-scoped mount may still relax scope."""
    from bernstein.core.sandbox.registry import default_registry

    registry = default_registry()
    registry.register("scopedfs", _ScopedMountBackend())
    try:
        os.environ["BERNSTEIN_SANDBOX"] = "1"
        os.environ["BERNSTEIN_SANDBOX_RUNTIME"] = "scopedfs"

        results = run_guardrails(_OUT_OF_SCOPE_DIFF, _make_task(), _scope_config(), tmp_path)
        scope_res = next(r for r in results if r.check == "scope_enforcement")

        assert scope_res.passed is True
        assert "[SANDBOX RELAXED]" in scope_res.detail
    finally:
        registry.unregister("scopedfs")


def test_relax_sandboxed_scope_requires_scoped_mount() -> None:
    """Direct unit: scope_enforcement relaxation is gated on the scoped mount."""
    os.environ["BERNSTEIN_SANDBOX"] = "1"
    ask = [PermissionDecision(type=DecisionType.ASK, reason="file outside task scope")]

    # No scoped mount -> boundary held.
    held = relax_sandboxed(ask, "scope_enforcement", scoped_mount=False)
    assert held[0].type == DecisionType.ASK

    # Scoped mount proven -> relaxed.
    relaxed = relax_sandboxed(ask, "scope_enforcement", scoped_mount=True)
    assert relaxed[0].type == DecisionType.ALLOW


def test_file_permissions_relaxation_preserved(tmp_path: Path) -> None:
    """file_permissions relaxation is unchanged: still relaxes under any
    sandbox backend, scoped mount or not."""
    os.environ["BERNSTEIN_SANDBOX"] = "1"
    os.environ["BERNSTEIN_SANDBOX_RUNTIME"] = "docker"
    deny = [PermissionDecision(type=DecisionType.DENY, reason="role cannot write path")]

    result = relax_sandboxed(deny, "file_permissions")
    assert result[0].type == DecisionType.ALLOW
    assert "[SANDBOX RELAXED]" in result[0].reason


def test_non_sandboxed_path_unchanged() -> None:
    """Outside a sandbox, nothing is relaxed regardless of check."""
    ask = [PermissionDecision(type=DecisionType.ASK, reason="file outside task scope")]
    assert relax_sandboxed(ask, "scope_enforcement", scoped_mount=True)[0].type == DecisionType.ASK
    assert relax_sandboxed(ask, "file_permissions")[0].type == DecisionType.ASK


def test_active_backend_scoped_mount_detection() -> None:
    """The helper reports False for docker (whole-repo mount) and when unset."""
    assert active_backend_provides_scoped_mount() is False  # unset

    os.environ["BERNSTEIN_SANDBOX_RUNTIME"] = "docker"
    assert active_backend_provides_scoped_mount() is False
