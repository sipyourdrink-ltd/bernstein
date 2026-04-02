"""Tests for role classification."""

from __future__ import annotations

from bernstein.core.role_classifier import classify_role


def test_classify_role_security() -> None:
    """Test security role detection."""
    assert classify_role("Fix SQL injection in auth module") == "security"
    assert classify_role("Rotate leaked secrets") == "security"


def test_classify_role_qa() -> None:
    """Test QA role detection."""
    assert classify_role("Add unit tests for the router") == "qa"
    assert classify_role("Improve test coverage") == "qa"


def test_classify_role_frontend() -> None:
    """Test frontend role detection."""
    assert classify_role("Fix button style in the UI") == "frontend"
    assert classify_role("Update React component") == "frontend"


def test_classify_role_devops() -> None:
    """Test devops role detection."""
    assert classify_role("Update Dockerfile") == "devops"
    assert classify_role("Fix CI pipeline") == "devops"


def test_classify_role_backend() -> None:
    """Test backend role detection."""
    assert classify_role("Create database migration") == "backend"
    assert classify_role("Add new API endpoint") == "backend"


def test_classify_role_default() -> None:
    """Test default role when no keywords match."""
    assert classify_role("Do something random") == "backend"


def test_classify_role_ambiguous() -> None:
    """Test ambiguous description (backend vs security)."""
    # 'auth' keyword matches both security and backend keywords,
    # but special rule returns backend for simple auth tasks.
    assert classify_role("Implement auth login") == "backend"

    # Adding 'encryption' makes it clearly security
    assert classify_role("Implement auth with strong encryption") == "security"
