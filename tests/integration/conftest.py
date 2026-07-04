"""Shared fixtures for integration tests."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from bernstein.core.models import ModelConfig, OrchestratorConfig
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.spawner import AgentSpawner
from fastapi.testclient import TestClient
from httpx import Response as HttpxResponse

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult
from bernstein.core.server import create_app

# Re-export the fake-CLI harness fixture so adapter integration tests can
# request ``fake_cli_fixture`` without an explicit module-level import.
from .fake_cli.conftest_adapters import (  # noqa: F401
    FakeCLIHandle,
    fake_cli_fixture,
)

_TASKS_PATH = "/tasks"

# Terminal success states for an integration task. A task first transitions to
# "done" on completion, then the orchestrator's verify-and-close pass advances
# a verified/merged task to "closed" (the terminal success state). Tests that
# poll for completion must treat BOTH as success; asserting strictly on "done"
# races the verify-close transition and flakes/fails once a task closes.
TERMINAL_SUCCESS_STATUSES = frozenset({"done", "closed"})

if TYPE_CHECKING:
    from fastapi import FastAPI


class IntegrationMockAdapter(CLIAdapter):
    """A flexible mock adapter that executes python commands from task descriptions."""

    def __init__(self, sdd_path: Path):
        self.sdd_path = sdd_path

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        log_path = workdir / ".sdd" / "runtime" / f"agent-{session_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Extract python script if present
        script_body = ""
        if "```python" in prompt and "# INTEGRATION-MOCK" in prompt:
            parts = prompt.split("```python")
            for part in parts[1:]:
                code = part.split("```")[0]
                if "# INTEGRATION-MOCK" in code:
                    script_body = code
                    break

        if not script_body:
            # Default: just commit and write a marker file for conftest to pick up
            import re

            task_ids = re.findall(r"id=([A-Za-z0-9\-_]+)", prompt)

            # Use absolute path for marker IN THE PROJECT ROOT SDD
            marker_dir = self.sdd_path.resolve() / "runtime"
            marker_dir.mkdir(parents=True, exist_ok=True)

            # Each marker line is substituted into the ``try:`` block below, so
            # it MUST carry the block's 4-space indentation. Without it the
            # generated script is a SyntaxError ("expected 'except' or 'finally'
            # block"), the mock never writes its DONE_ markers, and every task
            # that relies on the default mock stays "claimed" forever.
            markers_lines = "\n".join(
                f"    (Path('{marker_dir}') / 'DONE_{tid}').write_text('done')" for tid in task_ids
            )

            script_body = f"""import os
import subprocess
import sys
import time
from pathlib import Path

print(f"Mock agent starting (PID {{os.getpid()}})...")
print(f"Workdir: {{os.getcwd()}}")
# Give orchestrator plenty of time to see us alive
time.sleep(2.0)

# Mock work
try:
    with open("mock_output.txt", "w") as f:
        f.write("completed {session_id}")
    print("Wrote mock_output.txt")

    # Git ops
    res = subprocess.run(["git", "add", "."], cwd="{workdir}", check=False, capture_output=True, text=True)
    print(f"git add: {{res.returncode}} {{res.stdout}} {{res.stderr}}")

    res = subprocess.run(["git", "commit", "-m", "mock work"], cwd="{workdir}", check=False, capture_output=True, text=True)
    print(f"git commit: {{res.returncode}} {{res.stdout}} {{res.stderr}}")

    # Completion markers
    print(f"Writing markers to {marker_dir}...")
{markers_lines}
    print("Wrote markers successfully")
    # Hold alive a bit longer
    time.sleep(1.0)
except Exception as e:
    print(f"Error in mock script: {{e}}", file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)
"""

        script_path = self.sdd_path / "runtime" / f"script-{session_id}.py"
        script_path.write_text(script_body)

        with open(log_path, "w") as f:
            proc = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=f,
                stderr=subprocess.STDOUT,
                cwd=str(workdir),
            )

        return SpawnResult(pid=proc.pid, log_path=log_path, proc=proc)

    def name(self) -> str:
        return "integration-mock"


@pytest.fixture
def integration_sdd(tmp_path: Path) -> Path:
    # Clear env vars that might affect the run
    for key in list(os.environ.keys()):
        if key.startswith("BERNSTEIN_"):
            del os.environ[key]

    sdd = tmp_path / ".sdd"
    (sdd / "runtime").mkdir(parents=True)
    (sdd / "backlog" / "open").mkdir(parents=True)
    (sdd / "backlog" / "done").mkdir(parents=True)
    (sdd / "metrics").mkdir(parents=True)
    (sdd / "config").mkdir(parents=True)
    (sdd / "incidents").mkdir(parents=True)

    # Add dummy role templates. The integration tests spawn tasks for the
    # "backend", "frontend", and "manager" roles; every role that any test
    # spawns must have a template here or the spawned task never reaches
    # "done" (the spawner cannot resolve a system prompt and the task stays
    # "claimed").
    for role in ["backend", "frontend", "manager"]:
        templates = tmp_path / "templates" / "roles" / role
        templates.mkdir(parents=True, exist_ok=True)
        (templates / "system_prompt.md").write_text(f"You are a {role} specialist.")

    # Init git repo in tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)
    (tmp_path / "README.md").write_text("# Test Project")
    # Real bernstein projects gitignore ``.sdd/`` (workspace runtime state).
    # Without it, the mock adapter's per-agent log under ``.sdd/runtime/`` is
    # swept into the merge's staged set and the merge-preflight forbidden-path
    # guard (defect 28 decoy-commit guard) refuses the merge, so the task never
    # reaches "done". Mirror production and exclude ``.sdd/``.
    (tmp_path / ".gitignore").write_text(".sdd/\n")
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(tmp_path), check=True)

    # Give the repo a real (local, bare) "origin" remote. Salvage of an
    # abandoned worktree runs a best-effort ``git push origin <branch>``; with
    # no origin configured that push fails and logs noisy
    # ``'origin' does not appear to be a git repository`` warnings. A bare repo
    # on disk lets the push succeed cleanly.
    origin_path = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin_path)], check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin_path)],
        cwd=str(tmp_path),
        check=True,
    )
    subprocess.run(
        ["git", "push", "--set-upstream", "origin", "main"],
        cwd=str(tmp_path),
        check=True,
    )

    return sdd


@pytest.fixture
def test_app(integration_sdd: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    # Disable server auth before the app is built. When any auth secret is
    # present in the environment, ``create_app`` wires up the auth middleware
    # and every POST /tasks returns 401, so the tests that expect a created
    # task see ``KeyError: 'id'``. The supported opt-out is the
    # ``BERNSTEIN_AUTH_DISABLED`` env var, which must be set *before*
    # ``create_app`` reads it.
    monkeypatch.setenv("BERNSTEIN_AUTH_DISABLED", "1")
    jsonl_path = integration_sdd / "runtime" / "tasks.jsonl"
    return create_app(jsonl_path=jsonl_path)


@pytest.fixture
def test_client(test_app: FastAPI) -> TestClient:
    return TestClient(test_app)


@pytest.fixture
def orchestrator_factory(integration_sdd: Path):
    def _create(max_agents: int = 1, use_worktrees: bool = False):
        os.environ["BERNSTEIN_CLI"] = "integration-mock"
        os.environ["BERNSTEIN_MAX_TASK_RETRIES"] = "0"
        os.environ["BERNSTEIN_HEARTBEAT_TIMEOUT"] = "60"
        # The ephemeral test repo lives on its default branch ("main"), so the
        # spawner's protected-trunk guard refuses to merge worktree output back
        # and the task never reaches "done" (it stays "claimed" and is
        # salvaged). These integration tests deliberately merge onto that
        # throwaway branch, so opt into the documented override.
        os.environ["BERNSTEIN_ALLOW_MERGE_TO_DEFAULT_BRANCH"] = "1"

        config = OrchestratorConfig(
            server_url="http://127.0.0.1:8052",
            max_agents=max_agents,
            poll_interval_s=1,
            max_task_retries=0,
            # One task per agent. The default (2) batches same-role tasks so a
            # single agent is handed a multi-task batch, but the mock adapter
            # only executes the first task's embedded script -- the remaining
            # task in the batch never gets its marker and stays "claimed"
            # forever. These tests assert one agent per task, so pin the batch
            # size to 1.
            max_tasks_per_agent=1,
        )

        from bernstein.adapters.registry import register_adapter

        adapter = IntegrationMockAdapter(integration_sdd)
        register_adapter("integration-mock", adapter)

        # The spawn rate limiter defaults to 2 spawns / 10 s / provider to
        # avoid throttling real cloud CLIs. Every integration task uses the
        # single local "integration-mock" provider, so a test that spawns 3
        # agents at once trips the limit: the 3rd spawn (and its retries within
        # the 10 s window) fails with "Spawn rate limit exceeded", the task
        # exhausts its retries and ends "failed". Give the harness a permissive
        # limiter so all requested agents spawn immediately.
        from bernstein.core.agents.spawn_rate_limiter import (
            SpawnRateLimitConfig,
            SpawnRateLimiter,
        )

        permissive_rate_limiter = SpawnRateLimiter(SpawnRateLimitConfig(max_spawns=1000, window_seconds=1.0))

        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=integration_sdd.parent / "templates" / "roles",
            workdir=integration_sdd.parent,
            use_worktrees=use_worktrees,
            spawn_rate_limiter=permissive_rate_limiter,
        )
        orchestrator = Orchestrator(config, spawner, workdir=integration_sdd.parent)

        # Pin the effective agent count to the configured max. Adaptive
        # parallelism throttles ``effective_max_agents`` based on the host's
        # 5-minute load average: on a busy machine (loaded CI, or a laptop
        # running the rest of the suite in parallel) the CPU-overload rule
        # halves the agent count with no minimum floor, so an orchestrator
        # asked for max_agents=N spawns fewer than N. That leaves a task
        # claimed-but-never-spawned, and the tests -- which assert exact,
        # deterministic concurrency -- fail nondeterministically depending on
        # host load. Pinning to the configured max removes the load
        # dependence without touching production behavior.
        orchestrator._adaptive_parallelism.effective_max_agents = (  # type: ignore[method-assign]
            lambda: config.max_agents
        )
        return orchestrator

    return _create


def _auto_complete_done_markers(
    test_client: TestClient,
    integration_sdd: Path,
    tasks_data: list[dict[str, Any]],
    *,
    slug_fn: Callable[[dict[str, Any]], str] | None = None,
    complete_statuses: frozenset[str] = frozenset({"claimed", "working", "in_progress"}),
) -> None:
    """Auto-complete tasks that have a DONE_ marker file in runtime dir."""
    for t in tasks_data:
        if complete_statuses and t.get("status") not in complete_statuses:
            continue
        slug = slug_fn(t) if slug_fn else t["title"].lower().replace(" ", "-")
        marker = integration_sdd / "runtime" / f"DONE_{slug}"
        if marker.exists():
            test_client.post(f"/tasks/{t['id']}/complete", json={"result_summary": "done"})
            marker.unlink()


def make_proxy_handler(
    test_client: TestClient,
    integration_sdd: Path,
    *,
    slug_fn: Callable[[dict[str, Any]], str] | None = None,
    complete_statuses: frozenset[str] = frozenset({"claimed", "working", "in_progress"}),
    on_tasks_fetched: Callable[[list[dict[str, Any]]], None] | None = None,
) -> Callable[..., HttpxResponse]:
    """Build a standard request handler that proxies to test_client with auto-completion.

    Args:
        test_client: FastAPI test client.
        integration_sdd: Path to .sdd directory.
        slug_fn: Optional function to derive marker slug from a task dict.
        complete_statuses: Status values eligible for auto-completion.
        on_tasks_fetched: Optional callback invoked with tasks_data on GET /tasks.

    Returns:
        A handler function suitable for ``respx_mock.route().mock(side_effect=...)``.
    """

    def handler(request: Any) -> HttpxResponse:
        method = request.method
        path = request.url.path
        api_path = path if path.startswith("/") else "/" + path

        if method == "GET" and api_path == _TASKS_PATH:
            resp = test_client.get(_TASKS_PATH)
            tasks_data = resp.json()
            if on_tasks_fetched:
                on_tasks_fetched(tasks_data)
            _auto_complete_done_markers(
                test_client,
                integration_sdd,
                tasks_data,
                slug_fn=slug_fn,
                complete_statuses=complete_statuses,
            )
            resp = test_client.get(_TASKS_PATH)
            return HttpxResponse(resp.status_code, content=resp.content, headers=dict(resp.headers))

        content = request.read()
        headers = dict(request.headers)
        resp = test_client.request(method, api_path, content=content, headers=headers)
        return HttpxResponse(resp.status_code, content=resp.content, headers=dict(resp.headers))

    return handler
