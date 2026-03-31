"""Chaos test: malformed roadmap YAML."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from bernstein.core.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_malformed_roadmap(test_client: TestClient, orchestrator_factory, integration_sdd: Path):
    # 1. Create a malformed roadmap file
    roadmaps_dir = integration_sdd / "roadmaps" / "open"
    roadmaps_dir.mkdir(parents=True, exist_ok=True)

    malformed_file = roadmaps_dir / "bad_roadmap.yaml"
    malformed_file.write_text(
        "id: bad\ntitle: Bad\nscenarios:\n  - scenario1\n  - : missing colon value"
    )  # INVALID YAML

    # 2. Run orchestrator
    orch: Orchestrator = orchestrator_factory(max_agents=1, use_worktrees=True)
    orch._approval_gate = None
    orch._incident_manager.auto_pause = False

    # 3. Tick and verify no crash
    # Even if it logs a warning (it doesn't currently), it should not crash.
    orch.tick()

    # 4. Verify no tickets were emitted from the bad roadmap
    backlog_open = integration_sdd / "backlog" / "open"
    emitted = list(backlog_open.glob("bad-*.yaml"))
    assert len(emitted) == 0
