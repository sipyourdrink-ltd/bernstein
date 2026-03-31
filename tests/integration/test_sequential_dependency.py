"""Integration test: sequential task dependencies."""

from __future__ import annotations

import time
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
async def test_sequential_dependency(test_client: TestClient, orchestrator_factory, integration_sdd: Path, monkeypatch):
    # Monkeypatch merge_with_conflict_detection in both locations
    import bernstein.core.git_pr
    import bernstein.core.git_ops
    
    def debug_merge(cwd, branch, **kwargs):
        print(f"DEBUG: Merging {branch}")
        # Call original from git_pr to avoid recursion if we patched git_ops
        res = bernstein.core.git_pr.merge_with_conflict_detection(cwd, branch, **kwargs)
        print(f"DEBUG: Merge result for {branch}: {res.success} diff_len={len(res.merge_diff)}")
        return res
        
    monkeypatch.setattr(bernstein.core.git_ops, "merge_with_conflict_detection", debug_merge)

    # 1. Create a backend task
    desc_backend = (
        "```python\n"
        "# INTEGRATION-MOCK\n"
        "import os, subprocess, time\n"
        "from pathlib import Path\n"
        "Path('api.py').write_text('API v1')\n"
        "subprocess.run(['git', 'add', 'api.py'], check=True)\n"
        "subprocess.run(['git', 'commit', '-m', 'add api'], check=True)\n"
        "runtime_dir = Path(__file__).parent\n"
        "(runtime_dir / 'DONE_backend').write_text('done')\n"
        "time.sleep(2)\n"
        "```"
    )
    resp = test_client.post("/tasks", json={"title": "Backend", "description": desc_backend, "role": "backend"})
    backend_id = resp.json()["id"]

    # 2. Create a frontend task depending on backend
    desc_frontend = f"""
```python
# INTEGRATION-MOCK
import os, subprocess, time
from pathlib import Path
if not Path('api.py').exists():
    with open(r'{integration_sdd}/runtime/MISSING_API', 'w') as f:
        f.write('api.py missing')
    raise RuntimeError('api.py missing - dependency failed')
Path('ui.js').write_text('UI v1')
subprocess.run(['git', 'add', 'ui.js'], check=True)
subprocess.run(['git', 'commit', '-m', 'add ui'], check=True)
runtime_dir = Path(__file__).parent
(runtime_dir / 'DONE_frontend').write_text('done')
time.sleep(2)
```"""
    test_client.post("/tasks", json={
        "title": "Frontend", 
        "description": desc_frontend, 
        "role": "frontend",
        "depends_on": [backend_id]
    })

    # 3. Run orchestrator
    orch: Orchestrator = orchestrator_factory(max_agents=2, use_worktrees=True)
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
                    slug = t["title"].lower()
                    marker = integration_sdd / "runtime" / f"DONE_{slug}"
                    if marker.exists():
                        test_client.post(f"/tasks/{t['id']}/complete", json={"result_summary": "done"})
                        marker.unlink()
                resp = test_client.get("/tasks")
                return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

            content = request.read()
            headers = dict(request.headers)
            resp = test_client.request(method, api_path, content=content, headers=headers)
            return Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

        respx_mock.route().mock(side_effect=handler)

        # Run ticks
        for tick_idx in range(40):
            orch.tick()
            
            # Check if all tasks are done
            resp = test_client.get("/tasks")
            tasks = resp.json()
            all_done = all(t["status"] == "done" for t in tasks)
            
            print(f"Tick {tick_idx}: tasks={[ (t['title'], t['status']) for t in tasks]} agents={list(orch._agents.keys())}")
            
            if all_done:
                break
            time.sleep(0.5)
        
        # 4. Verify
        resp = test_client.get("/tasks")
        for t in resp.json():
            assert t["status"] == "done", f"Task {t['title']} failed: {t['status']}"

        assert (integration_sdd.parent / "api.py").exists()
        assert (integration_sdd.parent / "ui.js").exists()
