"""Benchmark: Orchestrator.tick() latency with large backlog."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient

from bernstein.adapters.mock import MockAgentAdapter
from bernstein.core.orchestrator import Orchestrator, OrchestratorConfig
from bernstein.core.server import create_app
from bernstein.core.spawner import AgentSpawner


def setup_benchmark_env():
    tmp_dir = Path(tempfile.mkdtemp())
    sdd_dir = tmp_dir / ".sdd"
    sdd_dir.mkdir()
    (sdd_dir / "backlog").mkdir()
    (sdd_dir / "backlog" / "open").mkdir()
    (sdd_dir / "runtime").mkdir()
    (sdd_dir / "metrics").mkdir()

    # Init git
    subprocess.run(["git", "init", "-b", "main"], cwd=str(tmp_dir), check=True, capture_output=True)
    (tmp_dir / "README.md").write_text("bench")
    subprocess.run(["git", "add", "."], cwd=str(tmp_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(tmp_dir), check=True, capture_output=True)

    # Templates
    templates_dir = tmp_dir / "templates"
    roles_dir = templates_dir / "roles"
    roles_dir.mkdir(parents=True)
    (roles_dir / "backend").mkdir()
    (roles_dir / "backend" / "config.yaml").write_text("model: haiku\neffort: low")

    return tmp_dir


def run_benchmark():
    workdir = setup_benchmark_env()
    app = create_app(jsonl_path=workdir / ".sdd" / "tasks.jsonl")
    client = TestClient(app)

    print("Adding 100 tasks to server...")
    for i in range(100):
        client.post(
            "/tasks", json={"title": f"Task {i}", "description": "benchmark task", "role": "backend", "priority": 2}
        )

    config = OrchestratorConfig(server_url="http://benchmark", max_agents=10, poll_interval_s=1)
    adapter = MockAgentAdapter()
    spawner = AgentSpawner(
        adapter=adapter,
        templates_dir=workdir / "templates" / "roles",
        workdir=workdir,
        use_worktrees=False,
    )
    orch = Orchestrator(config, spawner, workdir=workdir)
    orch._client = client  # Direct injection
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    print("Measuring tick() latency (10 iterations)...")
    latencies = []
    for _ in range(10):
        start = time.perf_counter()
        orch.tick()
        end = time.perf_counter()
        latencies.append((end - start) * 1000)
        # Reset agents after each tick to keep it consistent
        orch._agents.clear()
        orch._task_to_session.clear()

    avg_lat = sum(latencies) / len(latencies)
    # Sort and pick max for small samples
    p95_lat = sorted(latencies)[-1]

    print("\nResults (100 tasks in backlog):")
    print(f"  Average Tick Latency: {avg_lat:.2f} ms")
    print(f"  Max Tick Latency:     {p95_lat:.2f} ms")

    shutil.rmtree(workdir)


if __name__ == "__main__":
    run_benchmark()
