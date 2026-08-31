"""Tests for the fix to GitHub issue #4918: null downgrade_reason projects as empty string.

The fix changes the read shape for downgrade_reason from:
  str(resume_event.details.get("downgrade_reason", ""))
to a helper that treats None, "", and absent as the same outcome (empty string),
rejects non-string values with a ValueError, and returns string values unchanged.
"""

import pytest

from bernstein.core.tasks.suspension import _project_recorded_str


def test_project_recorded_str_handles_none():
    """None projects to empty string."""
    assert _project_recorded_str(None, event_id="abc123", field_name="downgrade_reason") == ""


def test_project_recorded_str_handles_empty_string():
    """Empty string projects to empty string."""
    assert _project_recorded_str("", event_id="abc123", field_name="downgrade_reason") == ""


def test_project_recorded_str_handles_string_unchanged():
    """String values are returned as-is."""
    assert _project_recorded_str("hello", event_id="abc123", field_name="downgrade_reason") == "hello"
    assert (
        _project_recorded_str("insufficient quota", event_id="abc123", field_name="downgrade_reason")
        == "insufficient quota"
    )


def test_project_recorded_str_handles_whitespace_string():
    """Whitespace-only strings are preserved (they are valid strings)."""
    assert _project_recorded_str("  ", event_id="abc123", field_name="downgrade_reason") == "  "


def test_project_recorded_str_raises_on_integer():
    """Integer values raise ValueError with a clear message."""
    with pytest.raises(ValueError) as exc_info:
        _project_recorded_str(42, event_id="abc123def456", field_name="downgrade_reason")
    assert "malformed audit event abc123def456..." in str(exc_info.value)
    assert "field 'downgrade_reason'" in str(exc_info.value)
    assert "is int" in str(exc_info.value)
    assert "expected string, null, or empty" in str(exc_info.value)


def test_project_recorded_str_raises_on_float():
    """Float values raise ValueError with a clear message."""
    with pytest.raises(ValueError) as exc_info:
        _project_recorded_str(3.14, event_id="abc123def456", field_name="downgrade_reason")
    assert "malformed audit event" in str(exc_info.value)
    assert "field 'downgrade_reason'" in str(exc_info.value)
    assert "is float" in str(exc_info.value)


def test_project_recorded_str_raises_on_list():
    """List values raise ValueError."""
    with pytest.raises(ValueError) as exc_info:
        _project_recorded_str(["a", "b"], event_id="abc123def456", field_name="downgrade_reason")
    assert "malformed audit event" in str(exc_info.value)
    assert "field 'downgrade_reason'" in str(exc_info.value)
    assert "is list" in str(exc_info.value)


def test_project_recorded_str_error_includes_event_id():
    """Error message includes the event HMAC (truncated to 16 chars) for traceability."""
    with pytest.raises(ValueError) as exc_info:
        _project_recorded_str(123, event_id="suspicious_event_hmac_value", field_name="downgrade_reason")
    assert "suspicious_event..." in str(exc_info.value)


def test_project_recorded_str_error_includes_field_name():
    """Error message includes the field name for context."""
    with pytest.raises(ValueError) as exc_info:
        _project_recorded_str(42, event_id="abc123", field_name="effective_mode")
    assert "field 'effective_mode'" in str(exc_info.value)
    assert "is int" in str(exc_info.value)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
