import pytest
from unittest.mock import MagicMock
import httpx

# We can reuse the same test for task_completion since the code is identical
from bernstein.core.task_lifecycle import retry_or_fail_task as retry_lifecycle
from bernstein.core.task_completion import retry_or_fail_task as retry_completion

# Mock Task class just enough to pass the function
class MockScope:
    value = "small"
class MockComplexity:
    value = "low"
class MockTaskType:
    value = "feature"

class MockTask:
    def __init__(self, task_id, description=""):
        self.id = task_id
        self.title = "Test Task"
        self.description = description
        self.role = "backend"
        self.priority = 1
        self.scope = MockScope()
        self.complexity = MockComplexity()
        self.estimated_minutes = 10
        self.depends_on = []
        self.owned_files = []
        self.task_type = MockTaskType()
        self.model = "sonnet"
        self.effort = "high"
        self.completion_signals = []

@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_retry_or_fail_task_transient(retry_func):
    mock_client = MagicMock(spec=httpx.Client)
    
    # Task has 0 retries so far
    task = MockTask("task-123", description="")
    tasks_snapshot = {"active": [task]}
    
    retried_ids = set()
    
    retry_func(
        task_id="task-123",
        reason="API Rate Limit Exceeded",
        client=mock_client,
        server_url="http://test",
        max_task_retries=1, # Default is 1, but transient should override to 3
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
    )
    
    # Should have posted a new task
    call_args = mock_client.post.call_args_list
    assert any(call[0][0].endswith("/tasks") for call in call_args)
    assert "task-123" in retried_ids

@pytest.mark.parametrize("retry_func", [retry_lifecycle, retry_completion])
def test_retry_or_fail_task_permanent(retry_func):
    mock_client = MagicMock(spec=httpx.Client)
    
    # Task has 0 retries
    task = MockTask("task-456", description="")
    tasks_snapshot = {"active": [task]}
    
    retried_ids = set()
    
    retry_func(
        task_id="task-456",
        reason="SyntaxError in main.py",
        client=mock_client,
        server_url="http://test",
        max_task_retries=3, # Default is 3, but permanent should override to 0
        retried_task_ids=retried_ids,
        tasks_snapshot=tasks_snapshot,
    )
    
    # Should NOT have posted a new task (no retry)
    call_args = mock_client.post.call_args_list
    assert not any(call[0][0].endswith("/tasks") for call in call_args)
    # It should call fail_task, which uses client.delete or hit an endpoint.
    # Actually wait, fail_task hits `client.post(f"{base}/tasks/{task_id}/fail")`
    # We just need to ensure `post` wasn't called for the original task resubmission.
    # Actually fail_task does a PUT or POST to fail it. Let's just check that `task_body` wasn't posted to `/tasks`.
    call_args = mock_client.post.call_args_list
    assert not any(call[0][0].endswith("/tasks") for call in call_args)
