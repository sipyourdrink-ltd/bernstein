"""Tests for the fix to GitHub issue #4918: null downgrade_reason should project as empty string."""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from bernstein.core.security.audit_chain import AuditEvent
from bernstein.core.tasks.suspension import verify_suspension_continuity, _project_recorded_str


def test_project_recorded_str_handles_none_and_empty():
    """Test that None and empty string both project to empty string."""
    assert _project_recorded_str(None, event_id="abc123", field_name="test") == ""
    assert _project_recorded_str("", event_id="abc123", field_name="test") == ""


def test_project_recorded_str_handles_string():
    """Test that string values are returned unchanged."""
    assert _project_recorded_str("hello", event_id="abc123", field_name="test") == "hello"
    assert _project_recorded_str("  ", event_id="abc123", field_name="test") == "  "


def test_project_recorded_str_raises_on_non_string():
    """Test that non-string, non-null values raise ValueError."""
    with pytest.raises(ValueError) as exc_info:
        _project_recorded_str(42, event_id="abc123def", field_name="downgrade_reason")
    assert "malformed audit event abc123def..." in str(exc_info.value)
    assert "field 'downgrade_reason'" in str(exc_info.value)
    assert "is int" in str(exc_info.value)
    assert "expected string, null, or empty" in str(exc_info.value)


def test_verify_suspension_continuity_null_downgrade_reason():
    """
    Test that a resume event with JSON null downgrade_reason projects to empty string.
    
    This reproduces the issue from #4918 where str(resume_event.details.get("downgrade_reason", ""))
    would convert None to "None" (4-char string) instead of treating it as empty.
    """
    # Create mock paths and journal setup
    task_path = Path("/fake/path/.sdd/runs/task-job001/journal.jsonl")
    mock_journal = Mock()
    mock_journal.head.return_value = "event_hash_xyz"
    mock_journal.chain_consistent = True
    mock_journal.discarded_line_indices = []
    mock_journal.event_count.return_value = 1
    
    # Mock the journal path and load_events
    with patch("bernstein.core.tasks.suspension._journal_path", return_value=task_path), \
         patch("bernstein.core.tasks.suspension.verify_journal", return_value=mock_journal), \
         patch("bernstein.core.tasks.suspension.load_events", return_value=type('obj', (object,), {'events': []})()):
        
        # Create a mock suspend event
        suspend_event = Mock()
        suspend_event.hmac = "suspend_hmac_123"
        suspend_event.details = {
            "task_id": "test-task",
            "suspend_event_hash": "suspend_event_hash_456",
            "journal_index": 0
        }
        
        # Create a mock resume event with JSON null downgrade_reason (None)
        resume_event = Mock()
        resume_event.hmac = "resume_hmac_789"
        resume_event.details = {
            "task_id": "test-task",
            "suspend_event_hash": "suspend_event_hash_456",  # matches suspend event
            "suspend_receipt_hash": "suspend_hmac_123",     # matches suspend receipt
            "downgrade_reason": None,                       # This is the JSON null case
            "effective_mode": "cold",
            "workspace_match": False,
            "resume_event_hash": "resume_event_hash_xyz"
        }
        
        # Create mock chain that returns our events
        mock_chain = Mock()
        mock_chain.verify.return_value = (True, [])
        mock_chain.query.side_effect = lambda event_type: (
            [suspend_event] if event_type == "task.suspend_receipt" else
            [resume_event] if event_type == "task.resume_receipt" else
            []
        )
        mock_chain.scan_verified.return_value = Mock(
            ok=True,
            errors=[],
            events=[suspend_event, resume_event]  # Simplified for test
        )
        
        # Call the function under test
        result = verify_suspension_continuity(
            sdd_dir="/fake/path",
            task_id="test-task",
            chain=mock_chain
        )
        
        # The downgrade_reason should be empty string, not "None"
        assert result.downgrade_reason == "", f"Expected empty string, got {result.downgrade_reason!r}"
        
        # The verification should succeed (no errors)
        assert result.ok, f"Expected verification to succeed, but got errors: {result.errors}"


def test_verify_suspension_continuity_absent_key():
    """
    Test that an absent downgrade_reason key also projects to empty string.
    """
    # Create mock paths and journal setup
    task_path = Path("/fake/path/.sdd/runs/task-job001/journal.jsonl")
    mock_journal = Mock()
    mock_journal.head.return_value = "event_hash_xyz"
    mock_journal.chain_consistent = True
    mock_journal.discarded_line_indices = []
    mock_journal.event_count.return_value = 1
    
    # Mock the journal path and load_events
    with patch("bernstein.core.tasks.suspension._journal_path", return_value=task_path), \
         patch("bernstein.core.tasks.suspension.verify_journal", return_value=mock_journal), \
         patch("bernstein.core.tasks.suspension.load_events", return_value=type('obj', (object,), {'events': []})()):
        
        # Create a mock suspend event
        suspend_event = Mock()
        suspend_event.hmac = "suspend_hmac_123"
        suspend_event.details = {
            "task_id": "test-task",
            "suspend_event_hash": "suspend_event_hash_456",
            "journal_index": 0
        }
        
        # Create a mock resume event with NO downgrade_reason key
        resume_event = Mock()
        resume_event.hmac = "resume_hmac_789"
        resume_event.details = {
            "task_id": "test-task",
            "suspend_event_hash": "suspend_event_hash_456",  # matches suspend event
            "suspend_receipt_hash": "suspend_hmac_123",     # matches suspend receipt
            # Note: no "downgrade_reason" key at all
            "effective_mode": "cold",
            "workspace_match": False,
            "resume_event_hash": "resume_event_hash_xyz"
        }
        
        # Create mock chain
        mock_chain = Mock()
        mock_chain.verify.return_value = (True, [])
        mock_chain.query.side_effect = lambda event_type: (
            [suspend_event] if event_type == "task.suspend_receipt" else
            [resume_event] if event_type == "task.resume_receipt" else
            []
        )
        mock_chain.scan_verified.return_value = Mock(
            ok=True,
            errors=[],
            events=[suspend_event, resume_event]
        )
        
        # Call the function under test
        result = verify_suspension_continuity(
            sdd_dir="/fake/path",
            task_id="test-task",
            chain=mock_chain
        )
        
        # The downgrade_reason should be empty string
        assert result.downgrade_reason == "", f"Expected empty string for absent key, got {result.downgrade_reason!r}"
        
        # The verification should succeed
        assert result.ok, f"Expected verification to succeed, but got errors: {result.errors}"


def test_verify_suspension_continuity_real_value():
    """
    Test that a real recorded reason still renders unchanged.
    """
    # Create mock paths and journal setup
    task_path = Path("/fake/path/.sdd/runs/task-job001/journal.jsonl")
    mock_journal = Mock()
    mock_journal.head.return_value = "event_hash_xyz"
    mock_journal.chain_consistent = True
    mock_journal.discarded_line_indices = []
    mock_journal.event_count.return_value = 1
    
    # Mock the journal path and load_events
    with patch("bernstein.core.tasks.suspension._journal_path", return_value=task_path), \
         patch("bernstein.core.tasks.suspension.verify_journal", return_value=mock_journal), \
         patch("bernstein.core.tasks.suspension.load_events", return_value=type('obj', (object,), {'events': []})()):
        
        # Create a mock suspend event
        suspend_event = Mock()
        suspend_event.hmac = "suspend_hmac_123"
        suspend_event.details = {
            "task_id": "test-task",
            "suspend_event_hash": "suspend_event_hash_456",
            "journal_index": 0
        }
        
        # Create a mock resume event with a real downgrade reason
        resume_event = Mock()
        resume_event.hmac = "resume_hmac_789"
        resume_event.details = {
            "task_id": "test-task",
            "suspend_event_hash": "suspend_event_hash_456",  # matches suspend event
            "suspend_receipt_hash": "suspend_hmac_123",     # matches suspend receipt
            "downgrade_reason": "insufficient quota",       # A real string value
            "effective_mode": "cold",
            "workspace_match": False,
            "resume_event_hash": "resume_event_hash_xyz"
        }
        
        # Create mock chain
        mock_chain = Mock()
        mock_chain.verify.return_value = (True, [])
        mock_chain.query.side_effect = lambda event_type: (
            [suspend_event] if event_type == "task.suspend_receipt" else
            [resume_event] if event_type == "task.resume_receipt" else
            []
        )
        mock_chain.scan_verified.return_value = Mock(
            ok=True,
            errors=[],
            events=[suspend_event, resume_event]
        )
        
        # Call the function under test
        result = verify_suspension_continuity(
            sdd_dir="/fake/path",
            task_id="test-task",
            chain=mock_chain
        )
        
        # The downgrade_reason should be the exact string value
        assert result.downgrade_reason == "insufficient quota", \
            f"Expected 'insufficient quota', got {result.downgrade_reason!r}"
        
        # The verification should succeed
        assert result.ok, f"Expected verification to succeed, but got errors: {result.errors}"


def test_verify_suspension_continuity_non_string_value():
    """
    Test that a non-string value (like integer 42) raises a named validation error.
    """
    # Create mock paths and journal setup
    task_path = Path("/fake/path/.sdd/runs/task-job001/journal.jsonl")
    mock_journal = Mock()
    mock_journal.head.return_value = "event_hash_xyz"
    mock_journal.chain_consistent = True
    mock_journal.discarded_line_indices = []
    mock_journal.event_count.return_value = 1
    
    # Mock the journal path and load_events
    with patch("bernstein.core.tasks.suspension._journal_path", return_value=task_path), \
         patch("bernstein.core.tasks.suspension.verify_journal", return_value=mock_journal), \
         patch("bernstein.core.tasks.suspension.load_events", return_value=type('obj', (object,), {'events': []})()):
        
        # Create a mock suspend event
        suspend_event = Mock()
        suspend_event.hmac = "suspend_hmac_123"
        suspend_event.details = {
            "task_id": "test-task",
            "suspend_event_hash": "suspend_event_hash_456",
            "journal_index": 0
        }
        
        # Create a mock resume event with an integer downgrade_reason (non-string)
        resume_event = Mock()
        resume_event.hmac = "resume_hmac_789"
        resume_event.details = {
            "task_id": "test-task",
            "suspend_event_hash": "suspend_event_hash_456",  # matches suspend event
            "suspend_receipt_hash": "suspend_hmac_123",     # matches suspend receipt
            "downgrade_reason": 42,                         # This should cause a validation error
            "effective_mode": "cold",
            "workspace_match": False,
            "resume_event_hash": "resume_event_hash_xyz"
        }
        
        # Create mock chain
        mock_chain = Mock()
        mock_chain.verify.return_value = (True, [])
        mock_chain.query.side_effect = lambda event_type: (
            [suspend_event] if event_type == "task.suspend_receipt" else
            [resume_event] if event_type == "task.resume_receipt" else
            []
        )
        mock_chain.scan_verified.return_value = Mock(
            ok=True,
            errors=[],
            events=[suspend_event, resume_event]
        )
        
        # Call the function under test
        result = verify_suspension_continuity(
            sdd_dir="/fake/path",
            task_id="test-task",
            chain=mock_chain
        )
        
        # The verification should FAIL due to the validation error
        assert not result.ok, "Expected verification to fail due to non-string downgrade_reason"
        
        # Should have exactly one error
        assert len(result.errors) == 1, f"Expected exactly one error, got {len(result.errors)}: {result.errors}"
        
        # Error should mention the malformed field
        error_msg = result.errors[0]
        assert "malformed audit event" in error_msg
        assert "field 'downgrade_reason'" in error_msg
        assert "is int" in error_msg
        assert "expected string, null, or empty" in error_msg