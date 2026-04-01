"""Integration test: Risk-based Approval Workflow Engine.

Tests that a high-risk task triggers 'review' mode and a low-risk task
triggers 'auto' mode via the dynamic approval mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from bernstein.core.models import AgentSession, ModelConfig, OrchestratorConfig
from bernstein.core.orchestrator import Orchestrator
from bernstein.core.server import create_app
from bernstein.core.spawner import AgentSpawner


def _make_mock_spawner(session_id: str = "agent-123", role: str = "backend") -> MagicMock:
    spawner = MagicMock(spec=AgentSpawner)
    session = AgentSession(
        id=session_id,
        role=role,
        pid=9999,
        model_config=ModelConfig("sonnet", "high"),
        status="working",
    )
    spawner.spawn_for_tasks.return_value = session
    spawner.check_alive.return_value = True
    spawner.get_worktree_path.return_value = None
    return spawner


def test_approval_workflow_e2e(tmp_path: Path) -> None:
    # 1. Start Server
    app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
    client = TestClient(app)

    # 2. Setup orchestrator with workflow config
    config = OrchestratorConfig(
        server_url="http://testserver",
        max_agents=2,
        max_tasks_per_agent=1,
        poll_interval_s=1,
        evolution_enabled=False,
        approval={
            "low_risk": "auto",
            "medium_risk": "review",
            "high_risk": "pr",
            "timeout_hours": 24,
            "notify_channels": ["slack"],
        },
    )
    mock_spawner = _make_mock_spawner()

    # We mock _notify to verify high-risk tasks fire it
    notifications: list[dict[str, Any]] = []

    def _mock_notify(event: str, title: str, body: str = "", **kwargs: Any) -> None:
        notifications.append({"event": event, "title": title, "body": body, **kwargs})

    orchestrator = Orchestrator(
        config=config,
        spawner=mock_spawner,
        workdir=tmp_path,
        client=client,
    )
    # Monkeypatch the notification method for testing
    orchestrator._notify = _mock_notify

    # 3. Create a low-risk task (acts normal) and a high-risk task
    r_low = client.post(
        "/tasks",
        json={
            "title": "Low risk typo fix",
            "description": "Fix a typo",
            "role": "backend",
            "risk_level": "low",
        },
    )
    r_high = client.post(
        "/tasks",
        json={
            "title": "Critical DB Drop",
            "description": "Drop users table",
            "role": "backend",
            "risk_level": "high",
        },
    )

    task_low = r_low.json()["id"]
    task_high = r_high.json()["id"]

    # 4. First tick spawns agents
    result1 = orchestrator.tick()
    # At least one spawned
    assert len(result1.spawned) > 0

    # 5. Claim both
    client.post(f"/tasks/{task_low}/claim")
    client.post(f"/tasks/{task_high}/claim")

    # 6. Complete both
    client.post(f"/tasks/{task_low}/complete", json={"result_summary": "Done"})
    client.post(f"/tasks/{task_high}/complete", json={"result_summary": "Dropped"})

    # 7. Second tick evaluates approvals and merges
    # We stop the agents so orchestrator sees them complete
    mock_spawner.check_alive.return_value = False

    # Let's just track call args to evaluate
    evaluations: list[tuple[str, object, object]] = []
    original_eval = orchestrator._approval_gate.evaluate

    def tracked_eval(
        task: Any, *, session_id: str, override_mode: Any = None, timeout_s: Any = None, **kwargs: Any
    ) -> Any:
        evaluations.append((task.id, override_mode, timeout_s))
        return original_eval(task, session_id=session_id, override_mode=override_mode, timeout_s=timeout_s, **kwargs)

    orchestrator._approval_gate.evaluate = tracked_eval

    # We prevent it from actually creating a PR:
    orchestrator._approval_gate.create_pr = MagicMock(return_value="http://pr")

    orchestrator.tick()

    # 8. Verifications
    # The evaluation calls should explicitly encode the overrides
    assert len(evaluations) == 2

    eval_by_task = {t_id: (m, t) for t_id, m, t in evaluations}

    # low_risk should have ApprovalMode.AUTO (from dict: "low_risk": "auto")
    from bernstein.core.approval import ApprovalMode

    assert eval_by_task[task_low][0] == ApprovalMode.AUTO

    # high_risk should have ApprovalMode.PR (from dict: "high_risk": "pr")
    assert eval_by_task[task_high][0] == ApprovalMode.PR

    # Verify timeout was passed correctly
    assert eval_by_task[task_low][1] == 24 * 3600
    assert eval_by_task[task_high][1] == 24 * 3600

    # Verify approval-needed notification fired for the high risk task
    approval_notifications = [n for n in notifications if n["event"] == "task.approval_needed"]
    assert len(approval_notifications) == 1
    assert approval_notifications[0]["task_id"] == task_high
    assert "Approval required (HIGH risk)" in approval_notifications[0]["title"]
