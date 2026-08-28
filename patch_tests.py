with open("tests/unit/orchestration/test_orchestrator_tick_methods.py", "r") as f:
    content = f.read()

import re

new_test = """
def test_check_file_overlap_records_wait_with_real_holder_ids() -> None:
    session = AgentSession(id="A-1", role="backend")
    session.status = "working"
    
    # We create an agent A-2 which requests this task.
    session_waiting = AgentSession(id="A-2", role="backend", task_ids=["T-parent"])
    session_waiting.status = "working"
    
    lock = SimpleNamespace(agent_id="ghost", task_id="T-old", locked_at=100.0)
    stub = _overlap_stub(
        file_ownership={"src/a.py": "A-1"},
        agents={"A-1": session, "A-2": session_waiting},
        lock_conflicts=[("src/locked.py", lock)],
    )
    
    stub._loop_detector = MagicMock()
    
    # Task has parent_task_id which belongs to A-2
    task = _task_with_files("T-1", ["src/a.py", "src/locked.py"])
    task.parent_task_id = "T-parent"
    
    assert stub._check_file_overlap([task]) is True
    
    stub._loop_detector.record_lock_wait.assert_called_once_with(
        waiting_agent_id="A-2",
        wanted_files=["src/a.py", "src/locked.py"],
        held_by={"src/a.py": "A-1", "src/locked.py": "ghost"},
        lock_timestamps={"ghost": 100.0},
    )

# ---------------------------------------------------------------------------
# _evaluate_budget_policy"""

content = content.replace("# ---------------------------------------------------------------------------\n# _evaluate_budget_policy", new_test)

with open("tests/unit/orchestration/test_orchestrator_tick_methods.py", "w") as f:
    f.write(content)
