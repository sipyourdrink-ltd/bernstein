"""Integration test: multi-agent simultaneous merge."""

from __future__ import annotations

import time
import re
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import respx
from httpx import Response

if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from bernstein.core.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_multi_agent_merge(test_client: TestClient, orchestrator_factory, integration_sdd: Path, monkeypatch):
    # Monkeypatch merge_with_conflict_detection to add debug logging
    import bernstein.core.git_pr
    original_merge = bernstein.core.git_pr.merge_with_conflict_detection
    
    def debug_merge(cwd, branch, **kwargs):
        print(f"DEBUG: Merging {branch}")
        res = original_merge(cwd, branch, **kwargs)
        print(f"DEBUG: Merge result for {branch}: {res.success} diff_len={len(res.merge_diff)}")
        return res
        
    monkeypatch.setattr(bernstein.core.git_pr, "merge_with_conflict_detection", debug_merge)

    # 1. Create 3 tasks each modifying a different file
    task_ids = []
    for i in range(1, 4):
        title = f"Task {i}"
        slug = title.lower().replace(" ", "-")
        desc = (
            "```python\n"
            "# INTEGRATION-MOCK\n"
            "import os, subprocess, time\n"
            "from pathlib import Path\n"
            "try:\n"
            f"    Path('file_{i}.txt').write_text('content {i}')\n"
            f"    r1 = subprocess.run(['git', 'add', 'file_{i}.txt'], capture_output=True, text=True)\n"
            f"    r2 = subprocess.run(['git', 'commit', '-m', 'mock work {i}'], capture_output=True, text=True)\n"
            "except Exception as e:\n"
            "    pass\n"
            "\n"
            "runtime_dir = Path(__file__).parent\n"
            f"(runtime_dir / 'DONE_{slug}').write_text('done')\n"
            "# Keep process alive so orchestrator can reap it properly\n"
            "time.sleep(2)\n"
            "```"
        )
        payload = {"title": title, "description": desc, "role": "backend"}
        resp = test_client.post("/tasks", json=payload)
        assert resp.status_code == 201
        task_ids.append(resp.json()["id"])

    # 2. Run orchestrator with max_agents=3
    orch: Orchestrator = orchestrator_factory(max_agents=3, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    with respx.mock(base_url="http://127.0.0.1:8052") as respx_mock:
        def handler(request):
            method = request.method
            path = request.url.path
            api_path = path if path.startswith("/") else "/" + path

            if method == "GET" and api_path == "/tasks":
                resp = test_client.get("/tasks")
                tasks = resp.json()
                for t in tasks:
                    slug = t["title"].lower().replace(" ", "-")
                    marker_slug = integration_sdd / "runtime" / f"DONE_{slug}"
                    if marker_slug.exists():
                        test_client.post(f"/tasks/{t['id']}/complete", json={"result_summary": "done"})
                        marker_slug.unlink()
                resp = test_client.get("/tasks")
                return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

            content = request.read()
            headers = dict(request.headers)
            resp = test_client.request(method, api_path, content=content, headers=headers)
            return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

        respx_mock.route().mock(side_effect=handler)

        for tick_idx in range(40):
            # NO MANUAL COMPLETION HERE to avoid races with orchestrator
            
            orch.tick()
            
            print(f"Tick {tick_idx}: agents={list(orch._agents.keys())} task_to_session={orch._task_to_session}")
            
            time.sleep(0.5)
            
            done_count = 0
            for tid in task_ids:
                resp = test_client.get(f"/tasks/{tid}")
                if resp.json()["status"] == "done":
                    done_count += 1
            if done_count == 3:
                # Run one more tick to ensure orchestrator processes the completions
                orch.tick()
                break
        
        # 3. Verify
        for tid in task_ids:
            resp = test_client.get(f"/tasks/{tid}")
            assert resp.json()["status"] == "done", f"Task {tid} failed to complete"

        for i in range(1, 4):
            fpath = integration_sdd.parent / f"file_{i}.txt"
            assert fpath.exists(), f"File {fpath} was not created or merged"
            assert fpath.read_text() == f"content {i}"

        worktrees = list(integration_sdd.parent.glob("bernstein-task-*"))
        assert len(worktrees) == 0, f"Stale worktrees found: {worktrees}"
