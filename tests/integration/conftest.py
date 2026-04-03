"""Shared fixtures for integration tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult
from bernstein.core.models import ModelConfig, OrchestratorConfig
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.server import create_app
from bernstein.core.spawner import AgentSpawner

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

            markers_lines = "\n".join(f"(Path('{marker_dir}') / 'DONE_{tid}').write_text('done')" for tid in task_ids)

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
