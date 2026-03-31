"""Shared pytest fixtures for the bernstein test suite."""

from __future__ import annotations

import gc
import os
import platform
import resource
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult
from bernstein.core.adaptive_parallelism import AdaptiveParallelism
from bernstein.core.models import (
    Complexity,
    ModelConfig,
    OrchestratorConfig,
    Scope,
    Task,
    TaskStatus,
    TaskType,
)
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.server import create_app
from bernstein.core.spawner import AgentSpawner

if TYPE_CHECKING:
    from fastapi import FastAPI

# ---------------------------------------------------------------------------
# Memory guard: prevent any single pytest run from eating >2 GB RAM.
# ---------------------------------------------------------------------------

_MAX_RSS_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

if platform.system() != "Windows":
    try:
        _soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (_MAX_RSS_BYTES, _hard))
    except (ValueError, AttributeError):
        pass  # RLIMIT_AS not available on all platforms


@pytest.fixture(autouse=True)
def _memory_guard():
    """Force GC before and after every test; abort if RSS exceeds limit."""
    yield
    # Aggressive garbage collection to prevent accumulation
    gc.collect()
    if platform.system() == "Darwin":
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss_bytes = usage.ru_maxrss  # macOS reports bytes
        if rss_bytes > _MAX_RSS_BYTES:
            print(
                f"\n\nFATAL: pytest RSS exceeded {_MAX_RSS_BYTES // (1024**3)} GB "
                f"(actual: {rss_bytes / (1024**3):.1f} GB). Aborting.\n",
                file=sys.stderr,
            )
            sys.exit(137)


@pytest.fixture(autouse=True)
def _stable_adaptive_parallelism(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep adaptive parallelism deterministic across the test suite.

    Integration tests should not depend on ambient machine load. Individual
    adaptive-parallelism tests can still override this with their own patches.
    """

    monkeypatch.setattr(AdaptiveParallelism, "_get_cpu_percent", lambda self: 0.0)


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """Aggressively clear references that pytest holds onto after each test."""
    # Clear funcargs/fixtures that may hold large mock objects or tmp_path data
    if hasattr(item, "funcargs"):
        item.funcargs.clear()
    # Clear report sections (captured stdout/stderr per test)
    if hasattr(item, "_report_sections"):
        item._report_sections.clear()
    gc.collect()


@pytest.fixture
def make_task():
    """Factory fixture for Task objects with sensible defaults.

    Supports all common Task fields; tests override only what they care about.
    """

    def _factory(
        *,
        id: str = "T-001",
        role: str = "backend",
        title: str = "Implement feature",
        description: str = "Write the code.",
        scope: Scope = Scope.MEDIUM,
        complexity: Complexity = Complexity.MEDIUM,
        status: TaskStatus = TaskStatus.OPEN,
        task_type: TaskType = TaskType.STANDARD,
        priority: int = 2,
        owned_files: list[str] | None = None,
        mcp_servers: list[str] | None = None,
    ) -> Task:
        return Task(
            id=id,
            title=title,
            description=description,
            role=role,
            scope=scope,
            complexity=complexity,
            status=status,
            task_type=task_type,
            priority=priority,
            owned_files=owned_files or [],
            mcp_servers=mcp_servers or [],
        )

    return _factory


@pytest.fixture
def mock_adapter_factory():
    """Factory fixture for CLIAdapter mocks with configurable PID."""

    def _factory(pid: int = 42) -> CLIAdapter:
        adapter = MagicMock(spec=CLIAdapter)
        adapter.spawn.return_value = SpawnResult(pid=pid, log_path=Path("/tmp/test.log"))
        adapter.is_alive.return_value = True
        adapter.kill.return_value = None
        adapter.name.return_value = "MockCLI"
        return adapter

    return _factory


@pytest.fixture
def sdd_dir(tmp_path: Path) -> Path:
    """Temporary .sdd directory with standard subdirectories pre-created."""
    sdd = tmp_path / ".sdd"
    (sdd / "backlog" / "open").mkdir(parents=True)
    (sdd / "backlog" / "done").mkdir(parents=True)
    (sdd / "runtime").mkdir(parents=True)
    (sdd / "metrics").mkdir(parents=True)
    (sdd / "upgrades").mkdir(parents=True)
    return sdd


# ---------------------------------------------------------------------------
# Integration & Chaos Engineering Fixtures
# ---------------------------------------------------------------------------


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

            marker_dir = self.sdd_path.resolve() / "runtime"
            marker_dir.mkdir(parents=True, exist_ok=True)

            markers = "\n".join(f"(Path('{marker_dir}') / 'DONE_{tid}').write_text('done')" for tid in task_ids)

            script_body = f"""
import os
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
    {{markers}}
    print("Wrote markers successfully")
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

    # Add dummy templates
    for role in ["backend", "manager"]:
        templates = tmp_path / "templates" / "roles" / role
        templates.mkdir(parents=True, exist_ok=True)
        (templates / "system_prompt.md").write_text(f"You are a {role} specialist.")

    # Init git repo in tmp_path
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(tmp_path), check=True)
    (tmp_path / "README.md").write_text("# Test Project")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(tmp_path), check=True)

    return sdd


@pytest.fixture
def test_app(integration_sdd: Path) -> FastAPI:
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

        config = OrchestratorConfig(
            server_url="http://127.0.0.1:8052",
            max_agents=max_agents,
            poll_interval_s=1,
            max_task_retries=0,
        )

        from bernstein.adapters.registry import register_adapter

        adapter = IntegrationMockAdapter(integration_sdd)
        register_adapter("integration-mock", adapter)

        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=integration_sdd.parent / "templates" / "roles",
            workdir=integration_sdd.parent,
            use_worktrees=use_worktrees,
        )
        return Orchestrator(config, spawner, workdir=integration_sdd.parent)

    return _create
