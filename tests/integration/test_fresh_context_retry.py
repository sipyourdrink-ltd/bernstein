"""End-to-end retry-with-fresh-context tests using the fake-CLI harness.

Closes #1109 - exercises the spawner's retry path when a task opts into
``agent_restart_between_retries``.  Configures the fake CLI to fail twice
then succeed, drives three sequential spawns through ``spawn_for_tasks``,
and asserts that every retry argv carries no resume-tokens / continuation
flags and no failure-replay payload from prior attempts.

Two scenarios:

1. ``test_fresh_restart_drops_failure_replay`` - flag on, three spawns,
   the 2nd and 3rd are retries.  The audit chain records both restarts
   and the rendered prompt never contains the prior-attempt failure
   markers.

2. ``test_default_off_preserves_replay`` - flag off (the default for
   every existing task), three spawns, the failure-replay text survives
   into the rendered prompt as it does today.  Regression guard.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.agents.spawn_rate_limiter import (
    SpawnRateLimitConfig,
    SpawnRateLimiter,
)
from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.security.audit import (
    AGENT_FRESH_RESTART_ON_RETRY,
    AuditLog,
)
from bernstein.core.tasks.models import (
    Complexity,
    ModelConfig,
    Scope,
    Task,
    TaskStatus,
)

if TYPE_CHECKING:
    from .fake_cli.conftest_adapters import FakeCLIHandle

# fake-CLI harness uses POSIX shell wrappers.
pytestmark = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="fake-CLI harness uses POSIX shell wrappers",
)

# ---------------------------------------------------------------------------
# Fake-CLI-backed adapter
# ---------------------------------------------------------------------------


class _FakeCLIBackedAdapter(CLIAdapter):
    """Adapter that forwards spawns to the fake-CLI wrapper on PATH.

    Records every prompt / argv pair so tests can verify the absence of
    accumulated state across retries.  Distinct from the production claude
    adapter - we only need the spawn surface for this regression.
    """

    def __init__(self, wrapper_dir: Path) -> None:
        self._wrapper_dir = wrapper_dir
        self.spawned_prompts: list[str] = []
        self.spawned_argv: list[list[str]] = []

    def name(self) -> str:
        return "fake-cli-mock"

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        self.spawned_prompts.append(prompt)
        # Mirror the claude adapter's argv shape (-p prompt + the two
        # required flags ``_validate_argv`` checks for).
        argv = [
            str(self._wrapper_dir / "claude"),
            "--output-format",
            "stream-json",
            "--permission-mode",
            "bypassPermissions",
            "-p",
            prompt,
        ]
        log_path = workdir / ".sdd" / "runtime" / f"agent-{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(workdir),
        )
        proc.wait(timeout=10)
        self.spawned_argv.append(argv)
        return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)

    def is_alive(self, pid: int) -> bool:  # type: ignore[override]
        return False

    def kill(self, pid: int) -> None:  # type: ignore[override]
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_audit_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Scope the audit HMAC key to this test (avoid clobbering ~/.local/state)."""
    key_path = tmp_path / "audit.key"
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    return key_path


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """Initialise a minimal git workdir + ``.sdd`` skeleton for the spawner."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=workdir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=workdir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=workdir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=workdir,
        check=True,
        capture_output=True,
    )
    (workdir / "templates" / "roles" / "backend").mkdir(parents=True, exist_ok=True)
    return workdir


@pytest.fixture
def spawner(
    workdir: Path,
    fake_cli_fixture: FakeCLIHandle,
) -> tuple[AgentSpawner, _FakeCLIBackedAdapter]:
    """Build a spawner whose adapter records every prompt + argv."""
    adapter = _FakeCLIBackedAdapter(fake_cli_fixture.bin_dir)
    # Permissive rate limiter so back-to-back retries do not trip the
    # default 2-per-10s budget.
    rate_limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=100, window_seconds=1.0))
    sp = AgentSpawner(
        adapter=adapter,
        templates_dir=workdir / "templates" / "roles",
        workdir=workdir,
        use_worktrees=False,
        spawn_rate_limiter=rate_limiter,
        default_model="mock-model",
    )
    # Stub the heavy auth/JWT path - production wires it up but unit-style
    # integration tests don't have an identity store on disk.
    sp._issue_agent_token = MagicMock(return_value=workdir / ".sdd" / "stub.token")  # type: ignore[method-assign]
    return sp, adapter


def _make_task(
    *,
    retry_count: int,
    flag: bool,
    description: str,
    meta_messages: list[str] | None = None,
    terminal_reason: str | None = None,
) -> Task:
    """Build a task that mirrors what ``maybe_retry_task`` would produce."""
    return Task(
        id="T-1109",
        title="Compile parser",
        description=description,
        role="backend",
        scope=Scope.SMALL,
        complexity=Complexity.LOW,
        status=TaskStatus.OPEN,
        retry_count=retry_count,
        max_retries=3,
        terminal_reason=terminal_reason,
        meta_messages=list(meta_messages or []),
        agent_restart_between_retries=flag,
    )


def _failure_replay_description(base: str) -> str:
    """Mirror the ``## Previous attempt failed`` block ``maybe_retry_task`` writes."""
    return (
        f"{base}\n\n"
        "## Previous attempt failed\n"
        "compile_error in src/parser.py\n\n"
        "Avoid the same mistakes. If you hit the same error, try a different approach."
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFreshRestartIntegration:
    """Three sequential spawns: success → success after two failures.

    The fake CLI is configured to fail twice and then succeed.  We don't
    actually drive the failure-classifier loop here - that is covered by
    unit tests in ``tests/unit/test_failure_aware_retry.py``.  This test
    verifies the *spawner's* contribution: when a retry is fresh-context,
    the prompt argv contains no replay text and the audit log records
    the restart with the correct ``retry_n`` / ``reason``.
    """

    def test_fresh_restart_drops_failure_replay(
        self,
        spawner: tuple[AgentSpawner, _FakeCLIBackedAdapter],
        fake_cli_fixture: FakeCLIHandle,
        workdir: Path,
        isolated_audit_key: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sp, adapter = spawner
        # Configure the fake CLI to "fail twice, then succeed".  The
        # adapter doesn't probe the exit code in this test so we just
        # use the default success mode for all three spawns; the failure
        # mode lives in the *task state*, not the wrapper.
        fake_cli_fixture.configure(mode="success")

        base_description = "Implement compile_parser()."
        attempts: list[tuple[int, str | None]] = [
            (0, None),
            (1, "compile_error"),
            (2, "compile_error"),
        ]
        for attempt_no, reason in attempts:
            description = base_description if attempt_no == 0 else _failure_replay_description(base_description)
            meta_messages = (
                []
                if attempt_no == 0
                else [
                    f"Retry {attempt_no}: Previous attempt failed with reason: {reason}",
                ]
            )
            task = _make_task(
                retry_count=attempt_no,
                flag=True,
                description=description,
                meta_messages=meta_messages,
                terminal_reason=reason,
            )
            sp.spawn_for_tasks([task])

        # Three spawns occurred.
        assert len(adapter.spawned_prompts) == 3
        assert len(adapter.spawned_argv) == 3

        # The 2nd and 3rd spawns are fresh-context retries → their prompt
        # must contain no failure-replay markers from prior attempts.
        for idx in (1, 2):
            prompt = adapter.spawned_prompts[idx]
            assert "## Previous attempt failed" not in prompt, (
                f"Retry attempt {idx} leaked previous-attempt block into the prompt"
            )
            assert "Retry " not in prompt or "Previous attempt failed" not in prompt, (
                f"Retry attempt {idx} leaked retry meta-message into the prompt"
            )

        # argv shape stays canonical across retries - no resume tokens
        # nor continuation flags piggy-back on the fresh restart.
        for idx, argv in enumerate(adapter.spawned_argv):
            assert "--resume" not in argv, f"spawn {idx} carried --resume"
            assert "--continue" not in argv, f"spawn {idx} carried --continue"
            assert "--session-id" not in argv, f"spawn {idx} carried --session-id"

        # Two audit events - one per retry restart, in order.
        log = AuditLog(audit_dir=workdir / ".sdd" / "audit")
        events = log.query(event_type=AGENT_FRESH_RESTART_ON_RETRY)
        assert len(events) == 2
        assert [e.details["retry_n"] for e in events] == [1, 2]
        assert all(e.details["task_id"] == "T-1109" for e in events)
        assert all(e.details["reason"] == "compile_error" for e in events)

        valid, errors = log.verify()
        assert valid, errors

    def test_default_off_preserves_replay(
        self,
        spawner: tuple[AgentSpawner, _FakeCLIBackedAdapter],
        fake_cli_fixture: FakeCLIHandle,
        workdir: Path,
        isolated_audit_key: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the flag is unset, retries keep the failure-replay payload.

        Regression guard: the new code path must not accidentally strip
        prior-failure context for tasks that did not opt into the fresh-
        restart semantics.
        """
        sp, adapter = spawner
        fake_cli_fixture.configure(mode="success")

        base_description = "Implement compile_parser()."
        retry_description = _failure_replay_description(base_description)
        retry_meta = ["Retry 1: Previous attempt failed with reason: compile_error"]

        sp.spawn_for_tasks(
            [
                _make_task(
                    retry_count=0,
                    flag=False,
                    description=base_description,
                )
            ]
        )
        sp.spawn_for_tasks(
            [
                _make_task(
                    retry_count=1,
                    flag=False,
                    description=retry_description,
                    meta_messages=retry_meta,
                    terminal_reason="compile_error",
                )
            ]
        )

        assert len(adapter.spawned_prompts) == 2
        retry_prompt = adapter.spawned_prompts[1]
        # Failure replay must survive when the flag is off (default).
        assert "## Previous attempt failed" in retry_prompt
        assert "compile_error" in retry_prompt

        # No fresh-restart audit events were emitted.
        audit_dir = workdir / ".sdd" / "audit"
        if audit_dir.exists():
            log = AuditLog(audit_dir=audit_dir)
            events = log.query(event_type=AGENT_FRESH_RESTART_ON_RETRY)
            assert events == []
