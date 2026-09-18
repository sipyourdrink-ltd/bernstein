import pytest
from unittest.mock import MagicMock, patch

from bernstein.core.tasks.task_lifecycle import claim_and_spawn_batches
from bernstein.core.tasks.models import Task


def test_quarantined_task_skip_transitions_terminal():
    # Setup orchestrator mock
    orch = MagicMock()
    orch._quarantine = MagicMock()
    orch._client = MagicMock()
    orch._config = MagicMock()
    orch._config.max_agents = 10
    orch._config.auto_decompose = False
    orch._config.force_parallel = False
    orch._config.server_url = "http://example.com"
    orch._agents = {}
    orch._idle_shutdown_ts = {}
    orch._spawn_failures = {}
    orch._decomposed_task_ids = set()
    orch._workdir = MagicMock()

    # Create a task that is quarantined
    task_title = "test task"
    task = MagicMock(spec=Task)
    task.title = task_title
    task.id = "task123"
    task.role = "backend"
    batch = [task]

    # Configure the quarantine to return that the task is quarantined with action='skip'
    orch._quarantine.is_quarantined.return_value = True
    entry = MagicMock()
    entry.fail_count = 5
    entry.action = "skip"
    orch._quarantine.get_entry.return_value = entry

    # We need to mock _pre_spawn_checks_pass to return True so we don't return early
    with patch('bernstein.core.tasks.task_lifecycle._pre_spawn_checks_pass', return_value=True):
        batches = [batch]
        alive_count = 0
        assigned_task_ids = set()
        done_ids = set()
        result = MagicMock()

        # Call the function
        claim_and_spawn_batches(
            orch, batches, alive_count, assigned_task_ids, done_ids, result
        )

    # Verify that fail_task was called with the correct arguments
    from bernstein.core.tick_pipeline import fail_task
    expected_reason = f"Quarantined after {entry.fail_count} failures: {entry.action}"
    fail_task.assert_called_once_with(
        orch._client,
        orch._config.server_url,
        task.id,
        expected_reason
    )
