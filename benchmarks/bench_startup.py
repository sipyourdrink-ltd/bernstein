"""Benchmark: End-to-end startup latency."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient


def run_benchmark():
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

    print("Measuring startup latency...")

    # We measure from the moment we start importing until the first tick completes
    # Since imports are cached, we'll do it in a loop but with fresh Orchestrator

    latencies = []
    for i in range(5):
        # We need fresh app/client each time because tasks.jsonl might be locked or something
        from bernstein.core.orchestrator import Orchestrator, OrchestratorConfig
        from bernstein.core.spawner import AgentSpawner

        from bernstein.adapters.mock import MockAgentAdapter
        from bernstein.core.server import create_app

        start = time.perf_counter()

        app = create_app(jsonl_path=tmp_dir / ".sdd" / f"tasks_{i}.jsonl")
        client = TestClient(app)

        config = OrchestratorConfig(server_url="http://benchmark", max_agents=1, poll_interval_s=1)
        adapter = MockAgentAdapter()
        spawner = AgentSpawner(
            adapter=adapter,
            templates_dir=tmp_dir / "templates" / "roles",
            workdir=tmp_dir,
            use_worktrees=False,
        )
        orch = Orchestrator(config, spawner, workdir=tmp_dir)
        orch._client = client
        orch._approval_gate = None
        orch._incident_manager.auto_pause = False

        orch.tick()

        end = time.perf_counter()
        latencies.append((end - start) * 1000)

    avg_lat = sum(latencies) / len(latencies)
    print(f"  Avg Startup Latency: {avg_lat:.2f} ms")

    shutil.rmtree(tmp_dir)


if __name__ == "__main__":
    run_benchmark()
